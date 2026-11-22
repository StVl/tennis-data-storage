"""Защита от выдуманных агентом результатов.

  python3 -m unittest tests.test_agent_results
  TEST_DATABASE_URL=postgresql:///tennis_pipeline_test python3 -m unittest tests.test_agent_results

2026-11-22 агент матчей в 19:01 записал финал ATP Finals Alcaraz-Zverev (начало 18:00, матч
ещё шёл) завершённым, 6-4 6-1 -- счётом матча Alcaraz-Cobolli из группы. PDF сетки результата
ещё не показывал, и migrate_draws завершил строку, которую бэкенд держал live. Три слоя:
  1. матч на корте по live_flags бэкенда результатом шарда не завершается (нужна БД);
  2. свежий результат, который не мог успеть сложиться, возвращается в upcoming;
  3. PDF, опровергающий результат шарда, переписывает matches_past.json.

Без TEST_DATABASE_URL тесты БД пропускаются; база пересоздаётся (drop schema public), поэтому
её имя обязано оканчиваться на `_test`."""

import copy
import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import build_config  # noqa: E402
from plausibility import FRESH_WINDOW, implausible, quarantine  # noqa: E402
from score import format_score, parse_score  # noqa: E402

DB_URL = os.environ.get("TEST_DATABASE_URL")
try:
    import psycopg
    from migrate_draws import apply_corrections, plan_edition, shard_record
except ImportError:  # pragma: no cover
    psycopg = None

CET = datetime.timezone(datetime.timedelta(hours=1))


def at(hhmm, day=22):
    h, m = map(int, hhmm.split(":"))
    return datetime.datetime(2026, 11, day, h, m, tzinfo=CET)


def record(pid, opp, result, score, start="2026-11-22T18:00:00+01:00", stage="F"):
    return {
        "id": f"{pid}_atpfinals_{stage.lower()}_2026", "playerId": pid, "opponentId": opp,
        "tournament": "Nitto ATP Finals 2026", "stage": stage, "startAt": start,
        "surface": "indoor_hard", "status": "completed", "result": result, "score": score,
    }


def incident_final():
    """Ровно то, что агент записал в matches_past.json в 19:01 (коммит b26ad97)."""
    return [record("alcaraz", "zverev", "won", "6-4, 6-1"),
            record("zverev", "alcaraz", "lost", "4-6, 1-6")]


class IncidentReplayTest(unittest.TestCase):

    def test_final_written_at_1901_goes_back_to_upcoming(self):
        up, past = [], incident_final()
        moved = quarantine(up, past, at("19:01"))
        self.assertEqual(past, [])
        self.assertEqual({mid for mid, _ in moved}, {"alcaraz_atpfinals_f_2026", "zverev_atpfinals_f_2026"})
        self.assertIn("17 геймов", dict(moved)["alcaraz_atpfinals_f_2026"])
        # запись -- ровно в той форме, в какой агент держит upcoming: следующий прогон
        # посмотрит матч заново
        self.assertEqual(up[0], {
            "id": "alcaraz_atpfinals_f_2026", "playerId": "alcaraz", "opponentId": "zverev",
            "tournament": "Nitto ATP Finals 2026", "stage": "F",
            "startAt": "2026-11-22T18:00:00+01:00", "surface": "indoor_hard",
            "isLive": False, "liveScore": None, "status": "upcoming",
        })
        self.assertEqual(list(up[0]), ["id", "playerId", "opponentId", "tournament", "stage",
                                       "startAt", "surface", "isLive", "liveScore", "status"])

    def test_same_result_an_hour_later_passes(self):
        # Предел слоя 2: время -- грубая проверка. 6-4 6-1 к 20:05 сыграть успели бы, и
        # повторённую в 20:05 выдумку ловит уже только слой 1 (live_flags).
        up, past = [], incident_final()
        self.assertEqual(quarantine(up, past, at("20:05")), [])
        self.assertEqual(len(past), 2)


class ImplausibleTest(unittest.TestCase):
    now = at("23:00")

    def check(self, score, result="won", start="2026-11-22T18:00:00+01:00"):
        return implausible(record("alcaraz", "zverev", result, score, start=start), self.now)

    def test_finished_match_with_real_score_passes(self):
        self.assertIsNone(self.check("6-4, 6-1"))
        self.assertIsNone(self.check("7-6(5), 3-6, 7-6(10)"))
        self.assertIsNone(self.check("6-4, 3-6, 6-7(2), 7-5, 6-4"))  # пять сетов
        self.assertIsNone(self.check("4-6, 1-6", result="lost"))

    def test_start_in_future(self):
        self.assertIn("не начался", self.check("6-4, 6-1", start="2026-11-23T12:00:00+01:00"))

    def test_impossible_set_scores(self):
        for score in ("6-5, 6-4", "8-6, 6-4", "6-6, 6-4", "7-3, 6-4", "0-0, 6-4"):
            self.assertIn("невозможный счёт сета", self.check(score), score)

    def test_unfinished_match(self):
        self.assertIn("не доигран", self.check("7-6, 4-6"))  # 1:1 по сетам
        self.assertIn("не доигран", self.check("6-4"))
        self.assertIn("не доигран", self.check("6-4, 6-4, 6-4, 6-4"))
        # после двух сетов из двух матч в best-of-3 уже окончен
        self.assertIn("последний сет", self.check("6-4, 6-4, 3-6"))

    def test_missing_result_or_score(self):
        self.assertIn("нет счёта", self.check(""))
        self.assertIn("нет result", self.check("6-4, 6-1", result=None))
        self.assertIn("не разобран", self.check("6-4, six-one"))

    def test_walkover_and_retirement(self):
        self.assertIsNone(self.check("w/o"))
        self.assertIn("нет result", self.check("w/o", result=None))
        # недоигранный последний сет при отказе -- норма, форма предыдущих -- нет
        self.assertIsNone(self.check("6-2, 2-0 ret."))
        self.assertIn("невозможный", self.check("6-5, 2-0 ret."))

    def test_old_results_are_not_rechecked(self):
        # история: «1-5», «7-6, 4-6» лежат в matches_past месяцами, вернуть их в upcoming нельзя
        old = (self.now - FRESH_WINDOW - datetime.timedelta(minutes=1)).isoformat()
        self.assertIsNone(self.check("1-5", start=old))
        self.assertIsNone(self.check("", result=None, start=old))

    def test_record_without_start_is_left_to_validation(self):
        m = record("alcaraz", "zverev", "won", "6-5")
        m.pop("startAt")
        self.assertIsNone(implausible(m, self.now))


class QuarantineTest(unittest.TestCase):

    def test_mirror_goes_back_with_the_rejected_record(self):
        # у одной из зеркальных записей нет result -- одна оставшаяся в past всё равно завершила бы
        # матч при заливке
        alcaraz, zverev = incident_final()
        zverev.pop("result")
        up, past = [], [alcaraz, zverev]
        moved = dict(quarantine(up, past, at("23:00")))
        self.assertEqual(past, [])
        self.assertIn("зеркальная", moved["alcaraz_atpfinals_f_2026"])
        self.assertIn("нет result", moved["zverev_atpfinals_f_2026"])

    def test_other_matches_stay(self):
        other = record("sinner", "fritz", "won", "6-3, 6-4", start="2026-11-22T12:00:00+01:00", stage="SF")
        up, past = [], incident_final() + [other]
        quarantine(up, past, at("19:01"))
        self.assertEqual(past, [other])
        self.assertEqual(len(up), 2)

    def test_real_shards_never_crash_the_check(self):
        # каждую реальную запись прогоняем через проверку так, будто она свежая
        past = json.loads((ROOT / "data" / "matches_past.json").read_text(encoding="utf-8"))["matches"]
        checked = 0
        for m in past:
            start = datetime.datetime.fromisoformat(m["startAt"].replace("Z", "+00:00")) if m.get("startAt") else None
            if start:
                implausible(m, start + datetime.timedelta(hours=3))
                checked += 1
        self.assertGreater(checked, 1000)


class BuildQuarantineTest(unittest.TestCase):
    """build_config.py пишет решение в шарды: их, а не config.json, читает migrate_data.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.shards = {
            "matches_upcoming": {"matches": []},
            "matches_past": {"matches": incident_final()},
        }
        for name, shard in self.shards.items():
            (self.data / f"{name}.json").write_text(json.dumps(shard), encoding="utf-8")
        patcher = mock.patch.object(build_config, "DATA", self.data)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def on_disk(self, name):
        return json.loads((self.data / f"{name}.json").read_text(encoding="utf-8"))["matches"]

    def test_build_rewrites_shards(self):
        moved = build_config.quarantine_shards(self.shards, at("19:01"), write=True)
        self.assertEqual(len(moved), 2)
        self.assertEqual(self.on_disk("matches_past"), [])
        self.assertEqual([m["status"] for m in self.on_disk("matches_upcoming")], ["upcoming"] * 2)

    def test_check_only_reports(self):
        before = (self.data / "matches_past.json").read_text(encoding="utf-8")
        moved = build_config.quarantine_shards(self.shards, at("19:01"), write=False)
        self.assertEqual(len(moved), 2)
        self.assertEqual((self.data / "matches_past.json").read_text(encoding="utf-8"), before)

    def test_quarantined_shards_pass_validation(self):
        shards = {
            "reference": {"playStyles": [{"id": "baseliner"}]},
            "players": {"players": [{"id": p, "name": p, "playStyleId": "baseliner"}
                                    for p in ("alcaraz", "zverev")]},
            "rankings": {"rankings": [{"playerId": p, "current": i, "trend": "stable"}
                                      for i, p in enumerate(("alcaraz", "zverev"), start=1)]},
            "tournaments_upcoming": {"tournaments": []},
            "tournaments_past": {"tournaments": []},
            **self.shards,
        }
        build_config.quarantine_shards(shards, at("19:01"), write=False)
        self.assertEqual(build_config.validate(shards), [])


class ScoreFormatTest(unittest.TestCase):
    def test_format_is_inverse_of_parse(self):
        for score in ("6-4, 7-6(5)", "6-2, 2-0 ret.", "w/o", "3-6, 7-6(10), 6-0"):
            sets, outcome = parse_score(score, [])
            self.assertEqual(format_score(sets, outcome), score)


@unittest.skipUnless(psycopg, "нужен psycopg")
class PlanGuardTest(unittest.TestCase):
    """Слои 1 и 3 в плане заливки сетки: чистая функция, без БД."""

    ALCARAZ, ZVEREV = 1, 2

    def draw(self, winner=None, score=None):
        return {"edition": "atp_finals_2026", "slots": 2,
                "entries": [{"pos": 1, "last": "Alcaraz"}, {"pos": 2, "last": "Zverev"}],
                "rounds": [{"code": "F", "matches": [
                    {"pos": 1, "top": 1, "bottom": 2, "winner": winner, "score": score,
                     "outcome": "normal" if winner else None}]}]}

    def row(self, live_flag):
        return [{"id": 50, "round": "F", "bracket_pos": 1, "status": "live" if live_flag else "scheduled",
                 "import_key": "alcaraz_atpfinals_f_2026", "players": {1: self.ALCARAZ, 2: self.ZVEREV},
                 "live_flag": live_flag}]

    def recs(self, final=None):
        ids = {"alcaraz": self.ALCARAZ, "zverev": self.ZVEREV}
        return [shard_record(m, ids, parse_score, []) for m in (final or incident_final())]

    def plan(self, live_flag, **draw):
        plan = plan_edition(self.draw(**draw), self.row(live_flag), self.recs(), {1: self.ALCARAZ, 2: self.ZVEREV})
        return plan, plan["upserts"][0]

    def test_shard_result_does_not_complete_a_match_on_court(self):
        plan, final = self.plan(live_flag=True)
        self.assertEqual((final["row_id"], final["status"], final["winner_side"], final["sets"]),
                         (50, None, None, None))
        self.assertTrue(any("на корте" in w for w in plan["warnings"]), plan["warnings"])

    def test_shard_result_still_completes_a_match_not_on_court(self):
        _, final = self.plan(live_flag=False)
        self.assertEqual((final["status"], final["winner_side"], final["sets"]),
                         ("completed", 1, [(6, 4, None), (6, 1, None)]))

    def test_pdf_completes_a_match_on_court(self):
        _, final = self.plan(live_flag=True, winner="top", score="6-4, 6-1")
        self.assertEqual((final["status"], final["winner_side"]), ("completed", 1))

    def test_pdf_that_disagrees_corrects_both_mirror_records(self):
        # PDF: Zverev (bottom) выиграл 7-6(4), 6-3 -- счёт глазами победителя
        plan, _ = self.plan(live_flag=False, winner="bottom", score="7-6(4), 6-3")
        fixes = {c["id"]: (c["result"], c["score"], c["winner_changed"]) for c in plan["corrections"]}
        self.assertEqual(fixes, {
            "alcaraz_atpfinals_f_2026": ("lost", "6-7(4), 3-6", True),
            "zverev_atpfinals_f_2026": ("won", "7-6(4), 6-3", True),
        })
        mismatch = [w for w in plan["warnings"] if w.startswith("[mismatch]")]
        self.assertEqual(len(mismatch), 2)
        self.assertIn("ПОБЕДИТЕЛЯ", mismatch[0])

    def test_pdf_winner_without_score_keeps_shard_score(self):
        # Так перевыпущен PDF ATP Finals 2026-11-22: у SF Zverev-Medvedev победитель есть, счёта нет.
        # Первая версия поправок стёрла бы «7-6(0), 6-4» в пустую строку.
        plan, _ = self.plan(live_flag=False, winner="top", score=None)
        self.assertEqual(plan["corrections"], [])
        self.assertFalse(any(w.startswith("[mismatch]") for w in plan["warnings"]))
        # опровергнутый победитель всё равно виден -- но шард без счёта не переписываем
        plan, _ = self.plan(live_flag=False, winner="bottom", score=None)
        self.assertEqual(plan["corrections"], [])
        self.assertIn("ПОБЕДИТЕЛЯ", plan["warnings"][0])

    def test_tiebreak_points_are_filled_in_quietly(self):
        # агент часто пишет «7-6» без очков тай-брейка: уточняем, но это не ошибка агента
        final = [record("alcaraz", "zverev", "won", "7-6, 6-1"),
                 record("zverev", "alcaraz", "lost", "6-7, 1-6")]
        plan = plan_edition(self.draw(winner="top", score="7-6(5), 6-1"), self.row(False),
                            self.recs(final), {1: self.ALCARAZ, 2: self.ZVEREV})
        self.assertEqual({c["id"]: c["score"] for c in plan["corrections"]}, {
            "alcaraz_atpfinals_f_2026": "7-6(5), 6-1", "zverev_atpfinals_f_2026": "6-7(5), 1-6"})
        self.assertFalse(any(w.startswith("[mismatch]") for w in plan["warnings"]))

    def test_pdf_without_tiebreak_points_keeps_the_shards(self):
        # обратный случай: PDF без очков, шард с ними -- «7-6(5)» -> «7-6» было бы потерей данных
        final = [record("alcaraz", "zverev", "won", "7-6(5), 6-1"),
                 record("zverev", "alcaraz", "lost", "6-7(5), 1-6")]
        plan = plan_edition(self.draw(winner="top", score="7-6, 6-1"), self.row(False),
                            self.recs(final), {1: self.ALCARAZ, 2: self.ZVEREV})
        self.assertEqual(plan["corrections"], [])
        # другие геймы -- очки шарда к ним не относятся и не переносятся
        plan = plan_edition(self.draw(winner="top", score="7-6, 6-2"), self.row(False),
                            self.recs(final), {1: self.ALCARAZ, 2: self.ZVEREV})
        self.assertEqual({c["id"]: c["score"] for c in plan["corrections"]}, {
            "alcaraz_atpfinals_f_2026": "7-6, 6-2", "zverev_atpfinals_f_2026": "6-7, 2-6"})

    def test_score_written_from_the_winners_side_is_a_mismatch(self):
        # LLM-шум: проигравший записан со счётом победителя (darderi_almaty_r16_2026)
        final = [record("alcaraz", "zverev", "won", "6-4, 6-1"),
                 record("zverev", "alcaraz", "lost", "6-4, 6-1")]
        plan = plan_edition(self.draw(winner="top", score="6-4, 6-1"), self.row(False),
                            self.recs(final), {1: self.ALCARAZ, 2: self.ZVEREV})
        self.assertEqual([(c["id"], c["score"]) for c in plan["corrections"]],
                         [("zverev_atpfinals_f_2026", "4-6, 1-6")])
        self.assertEqual(len([w for w in plan["warnings"] if w.startswith("[mismatch]")]), 1)

    def test_pdf_that_agrees_corrects_nothing(self):
        plan, _ = self.plan(live_flag=False, winner="top", score="6-4, 6-1")
        self.assertEqual(plan["corrections"], [])
        self.assertFalse(any(w.startswith("[mismatch]") for w in plan["warnings"]))

    def test_score_only_difference_is_a_score_mismatch(self):
        plan, _ = self.plan(live_flag=False, winner="top", score="6-4, 7-5")
        self.assertEqual({c["id"]: c["score"] for c in plan["corrections"]}, {
            "alcaraz_atpfinals_f_2026": "6-4, 7-5", "zverev_atpfinals_f_2026": "4-6, 5-7"})
        self.assertIn("счёт агента", plan["warnings"][0])

    def test_unfinished_shard_record_is_not_corrected(self):
        final = incident_final()
        for m in final:
            m.update(status="upcoming", result=None, score=None)
        plan = plan_edition(self.draw(winner="top", score="6-4, 6-1"), self.row(False),
                            self.recs(final), {1: self.ALCARAZ, 2: self.ZVEREV})
        self.assertEqual(plan["corrections"], [])

    def test_apply_corrections_touches_only_completed_records(self):
        past = incident_final() + [record("sinner", "fritz", "won", "6-3, 6-4", stage="SF")]
        upcoming_twin = dict(record("alcaraz", "zverev", None, None), status="upcoming")
        fixes = [{"id": "alcaraz_atpfinals_f_2026", "result": "lost", "score": "6-7(4), 3-6"},
                 {"id": "zverev_atpfinals_f_2026", "result": "won", "score": "7-6(4), 6-3"}]
        self.assertEqual(apply_corrections(past, fixes),
                         ["alcaraz_atpfinals_f_2026", "zverev_atpfinals_f_2026"])
        self.assertEqual((past[0]["result"], past[0]["score"]), ("lost", "6-7(4), 3-6"))
        self.assertEqual(past[2]["score"], "6-3, 6-4")
        self.assertEqual(apply_corrections(past, fixes), [])  # повторно -- ничего
        self.assertEqual(apply_corrections([upcoming_twin], fixes), [])


@unittest.skipUnless(psycopg, "нужен psycopg")
class WriteCorrectionsTest(unittest.TestCase):
    def test_write_corrections_rewrites_matches_past(self):
        import migrate_data
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(migrate_data, "DATA", Path(tmp)):
            past = {"matches": incident_final()}
            (Path(tmp) / "matches_past.json").write_text(json.dumps(past), encoding="utf-8")
            migrate_data.write_corrections([{"id": "alcaraz_atpfinals_f_2026", "result": "lost",
                                        "score": "6-7(4), 3-6"}])
            got = json.loads((Path(tmp) / "matches_past.json").read_text(encoding="utf-8"))["matches"]
            expected = copy.deepcopy(past["matches"])
            expected[0].update(result="lost", score="6-7(4), 3-6")
            self.assertEqual(got, expected)


@unittest.skipUnless(DB_URL and psycopg, "нужны TEST_DATABASE_URL и psycopg")
class LiveHoldDbTest(unittest.TestCase):
    """Слой 1 на настоящей схеме: строку, которую бэкенд держит live, заливка не завершает."""

    def setUp(self):
        import migrate_data
        self.md = migrate_data
        self.conn = psycopg.connect(DB_URL, autocommit=True)
        if not self.conn.execute("select current_database()").fetchone()[0].endswith("_test"):
            self.conn.close()
            self.fail("база не тестовая (нет суффикса _test)")
        self.conn.execute("drop schema public cascade; create schema public;")
        self.conn.execute((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
        self.cur = self.conn.cursor()
        self.md.upsert_play_styles(self.cur, {"playStyles": [
            {"id": "baseliner", "name": "Baseliner", "description": "-"}]})
        roster = {"players": [{"id": "alcaraz", "name": "Carlos Alcaraz", "playStyleId": "baseliner"},
                              {"id": "zverev", "name": "Alexander Zverev", "playStyleId": "baseliner"}]}
        self.tournaments = [
            {"tournament": "atp_finals", "name": "Nitto ATP Finals", "courtType": "indoor_hard",
             "dates": {"start": "2026-11-15", "end": "2026-11-22"}},
            {"tournament": "paris_masters", "name": "Rolex Paris Masters", "courtType": "indoor_hard",
             "dates": {"start": "2026-11-02", "end": "2026-11-08"}},
        ]
        self.player_ids = self.md.upsert_players(self.cur, roster, incident_final())
        self.md.upsert_tournaments(self.cur, self.tournaments, incident_final(), self.player_ids)
        self.genders = {"alcaraz": "m", "zverev": "m"}

    def tearDown(self):
        self.conn.close()

    def backend_ddl(self):
        # Минимум таблицы tennis-backend (db/live_ingest.sql): заливке нужны match_id и flipped_at.
        self.conn.execute("""create table live_flags (
            match_id bigint primary key references matches(id) on delete cascade,
            source text not null default 'livetennisapi',
            flipped_at timestamptz not null default now())""")

    def backend_flips_live(self, match_id, ago="0 minutes"):
        self.conn.execute("update matches set status = 'live' where id = %s", (match_id,))
        self.conn.execute("insert into live_flags (match_id, flipped_at) values (%s, now() - %s::interval)",
                          (match_id, ago))

    def backend_flips_out(self, match_id):
        self.conn.execute("update matches set status = 'scheduled' where id = %s and status = 'live'", (match_id,))
        self.conn.execute("delete from live_flags where match_id = %s", (match_id,))

    def match(self, import_key=None, match_id=None):
        where, arg = ("import_key = %s", import_key) if import_key else ("id = %s", match_id)
        status, winner = self.conn.execute(
            f"select status::text, winner_side from matches where {where}", (arg,)).fetchone()
        sets = self.conn.execute(
            f"""select count(*) from match_sets where match_id = (select id from matches where {where})""",
            (arg,)).fetchone()[0]
        return status, winner, sets

    # --- без сетки: migrate_matches -------------------------------------------------------

    def paris(self, final):
        for m in final:
            m["tournament"] = "Rolex Paris Masters 2026"
        return final

    def sync_matches(self, up, past):
        warn = []
        held = self.md.live_held_matches(self.cur)
        self.md.migrate_matches(self.cur, up, past, self.player_ids, self.genders, {}, warn,
                                held_keys=frozenset(k for k in held.values() if k))
        return warn

    def test_no_backend_tables_is_not_an_error(self):
        self.assertEqual(self.md.live_held_matches(self.cur), {})

    def test_matches_path_holds_a_flagged_row(self):
        self.backend_ddl()
        upcoming = [dict(m, status="upcoming", isLive=False, liveScore=None) for m in self.paris(incident_final())]
        for m in upcoming:
            m.pop("result"), m.pop("score")
        self.sync_matches(upcoming, [])
        key = "alcaraz_atpfinals_f_2026"
        mid = self.conn.execute("select id from matches where import_key = %s", (key,)).fetchone()[0]
        self.backend_flips_live(mid)

        # агент выдумал результат, пока бэкенд держит матч на корте
        warn = self.sync_matches([], self.paris(incident_final()))
        self.assertEqual(self.match(key), ("live", None, 0))
        self.assertTrue(any("на корте" in w for w in warn), warn)
        # и не затёр live статусом 'scheduled' из шарда, не заметившего начала
        self.sync_matches(upcoming, [])
        self.assertEqual(self.match(key), ("live", None, 0))

        # источник показал конец матча, бэкенд снял флаг -- результат шарда проходит
        self.backend_flips_out(mid)
        self.sync_matches([], self.paris(incident_final()))
        self.assertEqual(self.match(key), ("completed", 1, 2))

    # --- с сеткой: migrate_draws ----------------------------------------------------------

    def final_draw(self, winner=None, score=None):
        return {"atp_finals_2026": {
            "edition": "atp_finals_2026", "tour": "atp", "drawSize": 2, "slots": 2,
            "entries": [{"pos": 1, "first": "Carlos", "last": "Alcaraz"},
                        {"pos": 2, "first": "Alexander", "last": "Zverev"}],
            "rounds": [{"code": "F", "matches": [
                {"pos": 1, "top": 1, "bottom": 2, "winner": winner, "score": score,
                 "outcome": "normal" if winner else None}]}]}}

    def sync_draws(self, shard, **draw):
        from migrate_draws import migrate_draws
        warn = []
        enrich = {"atp_finals_2026": [shard_record(m, self.player_ids, parse_score, warn) for m in shard]}
        fixes = migrate_draws(self.cur, self.final_draw(**draw), enrich, warn,
                              frozenset(self.md.live_held_matches(self.cur)))
        return warn, fixes

    def test_draws_path_holds_a_flagged_row_until_flip_out(self):
        self.backend_ddl()
        self.sync_draws([])
        mid = self.conn.execute("select id from matches where round_code = 'F'").fetchone()[0]
        self.backend_flips_live(mid)

        warn, _ = self.sync_draws(incident_final())
        self.assertEqual(self.match(match_id=mid), ("live", None, 0))
        self.assertTrue(any("на корте" in w for w in warn), warn)

        self.backend_flips_out(mid)
        self.sync_draws(incident_final())
        self.assertEqual(self.match(match_id=mid), ("completed", 1, 2))

    def test_flag_left_by_a_stopped_backend_stops_holding(self):
        # бэкенд остановлен и флаг не погасил: старше LIVE_HOLD_MAX_AGE результат шарда проходит
        self.backend_ddl()
        self.sync_draws([])
        mid = self.conn.execute("select id from matches where round_code = 'F'").fetchone()[0]
        self.backend_flips_live(mid, ago="7 hours")
        self.assertEqual(self.md.live_held_matches(self.cur), {})
        self.sync_draws(incident_final())
        self.assertEqual(self.match(match_id=mid), ("completed", 1, 2))

    def test_draws_path_pdf_result_wins_over_flag_and_corrects_shard(self):
        self.backend_ddl()
        self.sync_draws([])
        mid = self.conn.execute("select id from matches where round_code = 'F'").fetchone()[0]
        self.backend_flips_live(mid)

        warn, fixes = self.sync_draws(incident_final(), winner="bottom", score="7-6(4), 6-3")
        self.assertEqual(self.match(match_id=mid), ("completed", 2, 2))
        self.assertEqual({f["id"]: f["score"] for f in fixes}, {
            "alcaraz_atpfinals_f_2026": "6-7(4), 3-6", "zverev_atpfinals_f_2026": "7-6(4), 6-3"})
        self.assertTrue(any(w.startswith("[mismatch]") for w in warn), warn)


if __name__ == "__main__":
    unittest.main()
