"""Тесты пуша конвейера: build_config.git_push на временных репозиториях.

  python3 -m unittest tests.test_git_push

Регрессия 2026-10-26: в origin/main влили PR, живой чекаут его не подтянул, и каждый
`git push` отклонялся как non-fast-forward две недели подряд."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from build_config import git_push  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def clone(remote, path):
    subprocess.run(["git", "clone", "-q", str(remote), str(path)], check=True)
    git(path, "config", "user.name", "test")
    git(path, "config", "user.email", "test@example.com")


class GitPushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.remote = base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        seed = base / "seed"
        clone(self.remote, seed)
        (seed / "data.json").write_text("{}\n")
        (seed / "script.py").write_text("x = 1\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "seed")
        git(seed, "push", "-q", "origin", "HEAD:main")
        self.pipeline = base / "pipeline"
        self.other = base / "other"
        clone(self.remote, self.pipeline)
        clone(self.remote, self.other)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pushes_when_remote_moved_ahead(self):
        (self.other / "script.py").write_text("x = 2\n")
        git(self.other, "commit", "-qam", "code fix merged on github")
        git(self.other, "push", "-q")
        (self.pipeline / "data.json").write_text('{"a": 1}\n')

        git_push(self.pipeline)

        log = git(self.remote, "log", "--format=%s", "main")
        self.assertIn("code fix merged on github", log)
        self.assertIn("data update", log)
        self.assertEqual(git(self.pipeline, "status", "--porcelain"), "")

    def test_conflict_aborts_merge_and_raises(self):
        (self.other / "data.json").write_text('{"b": 2}\n')
        git(self.other, "commit", "-qam", "conflicting edit")
        git(self.other, "push", "-q")
        (self.pipeline / "data.json").write_text('{"a": 1}\n')

        with self.assertRaises(subprocess.CalledProcessError):
            git_push(self.pipeline)

        # Чекаут не остаётся посреди слияния: следующий прогон увидит чистое дерево.
        self.assertFalse((self.pipeline / ".git" / "MERGE_HEAD").exists())
        self.assertEqual(git(self.pipeline, "status", "--porcelain"), "")

    def test_no_changes_no_commit(self):
        before = git(self.pipeline, "rev-parse", "HEAD")
        git_push(self.pipeline)
        self.assertEqual(git(self.pipeline, "rev-parse", "HEAD"), before)


if __name__ == "__main__":
    unittest.main()
