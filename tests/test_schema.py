"""Тесты схемы: идемпотентность и два тура (ATP/WTA) у одного бренда.

  TEST_DATABASE_URL=postgresql:///tennis_wta_test python3 -m unittest tests.test_schema

Без TEST_DATABASE_URL тесты пропускаются. База пересоздаётся с нуля (drop schema public), поэтому
имя базы обязано оканчиваться на `_test` -- защита от прогона по рабочей базе."""

import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
DB_URL = os.environ.get("TEST_DATABASE_URL")

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


@unittest.skipUnless(DB_URL and psycopg, "нужны TEST_DATABASE_URL и psycopg")
class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(DB_URL, autocommit=True)
        dbname = self.conn.execute("select current_database()").fetchone()[0]
        if not dbname.endswith("_test"):
            self.conn.close()
            self.fail(f"база {dbname!r} не тестовая (нет суффикса _test)")
        self.conn.execute("drop schema public cascade; create schema public;")

    def tearDown(self):
        self.conn.close()

    def apply(self):
        self.conn.execute(SCHEMA)

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]

    def add_edition(self, slug, gender):
        self.conn.execute(
            """insert into tournament_editions
                 (tournament_id, year, slug, start_date, end_date, surface, gender)
               values ((select id from tournaments where slug = 'wimbledon'), 2026, %s,
                       '2026-06-29', '2026-07-12', 'grass', %s)""",
            (slug, gender),
        )

    def test_schema_is_idempotent_and_allows_two_genders(self):
        self.apply()
        self.apply()

        self.assertEqual(self.one("select name from tours where code = 'wta'"), "WTA Tour")
        self.assertEqual(self.one("select tour_code_for('m')"), "atp")
        self.assertEqual(self.one("select tour_code_for('f')"), "wta")

        self.conn.execute("insert into tournaments (slug, name) values ('wimbledon', 'Wimbledon')")
        self.add_edition("wimbledon_2026", "m")
        self.add_edition("wimbledon_wta_2026", "f")
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.add_edition("wimbledon_wta_2026_dup", "f")

        self.assertEqual(
            self.one("select tour_code from v_tournament_editions where slug = 'wimbledon_wta_2026'"), "wta")
        self.assertEqual(
            self.one("select tour_code from v_tournament_editions where slug = 'wimbledon_2026'"), "atp")

    def test_existing_database_gets_gender_column(self):
        # База, созданная до WTA: уникальность без gender, колонки нет.
        self.apply()
        self.conn.execute("drop view v_tournament_editions")
        self.conn.execute("alter table tournament_editions drop column gender")
        self.conn.execute(
            """alter table tournament_editions
                 add constraint tournament_editions_tournament_id_year_discipline_key
                 unique (tournament_id, year, discipline)""")
        self.conn.execute("insert into tournaments (slug, name) values ('wimbledon', 'Wimbledon')")
        self.conn.execute(
            """insert into tournament_editions (tournament_id, year, slug, start_date, end_date, surface)
               values ((select id from tournaments where slug = 'wimbledon'), 2026, 'wimbledon_2026',
                       '2026-06-29', '2026-07-12', 'grass')""")

        self.apply()

        self.assertEqual(self.one("select gender::text from tournament_editions where slug = 'wimbledon_2026'"), "m")
        self.add_edition("wimbledon_wta_2026", "f")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(DB_URL and psycopg, "нужны TEST_DATABASE_URL и psycopg")
class DrawStatusTest(unittest.TestCase):
    """A tournament that has started is drawn, whether or not we hold its draw or any match."""

    def setUp(self):
        self.conn = psycopg.connect(DB_URL, autocommit=True)
        if not self.conn.execute("select current_database()").fetchone()[0].endswith("_test"):
            self.conn.close()
            self.fail("база не тестовая")
        self.conn.execute("drop schema public cascade; create schema public;")
        self.conn.execute(SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_ongoing_edition_without_matches_is_drawn(self):
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        from seed_tournament_card_meta import mark_drawn
        for brand, start, end in [("osaka", "current_date - 2", "current_date + 3"),
                                  ("later", "current_date + 5", "current_date + 10")]:
            self.conn.execute(f"insert into tournaments (slug, name) values ('{brand}', '{brand}')")
            self.conn.execute(f"""insert into tournament_editions
                (tournament_id, year, slug, start_date, end_date, surface, gender)
                values ((select id from tournaments where slug = '{brand}'), 2026, '{brand}_wta_2026',
                        {start}, {end}, 'hard', 'f')""")
        with self.conn.cursor() as cur:
            mark_drawn(cur)
        status = dict(self.conn.execute("select slug, draw_status::text from tournament_editions").fetchall())
        self.assertEqual(status["osaka_wta_2026"], "drawn")
        self.assertEqual(status["later_wta_2026"], "awaiting_draw")
