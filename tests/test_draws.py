"""Тесты импорта сеток: разбор PDF ATP и план заливки в БД.

  python3 -m unittest discover -s tests

Разбор PDF требует pdfplumber; без него эти тесты пропускаются (планировщик -- нет).
Фикстуры -- настоящие PDF 2026 года: Beijing (32, идёт), Chengdu (28 с bye, завершён),
Cincinnati (96, две страницы, бокс чемпиона)."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
FIXTURES = Path(__file__).resolve().parent / "fixtures"

try:
    import pdfplumber  # noqa: F401
    HAVE_PDF = True
except ImportError:
    HAVE_PDF = False

from migrate_draws import depth, plan_edition, resolve_player, sets_for_sides  # noqa: E402


def parse(name):
    from draw_pdf import parse_draw_pdf
    return parse_draw_pdf((FIXTURES / name).read_bytes())


def by_pos(draw):
    return {e["pos"]: e for e in draw["entries"]}


def match(draw, code, pos):
    rnd = next(r for r in draw["rounds"] if r["code"] == code)
    return next(m for m in rnd["matches"] if m["pos"] == pos)


@unittest.skipUnless(HAVE_PDF, "нужен pdfplumber")
class DrawPdfTests(unittest.TestCase):

    def test_header(self):
        d = parse("beijing_2026_mds.pdf")
        self.assertEqual(d["title"], "China Open")
        self.assertEqual((d["startDate"], d["endDate"]), ("2026-09-30", "2026-10-06"))
        self.assertEqual(d["category"], "ATP 500")
        self.assertEqual(d["released"], "2026-10-02T14:25:46")
        self.assertEqual((d["drawSize"], d["slots"]), (32, 32))

    def test_entries(self):
        e = by_pos(parse("beijing_2026_mds.pdf"))
        self.assertEqual((e[1]["last"], e[1]["seed"]), ("Zverev", 1))
        self.assertEqual(e[3]["entry"], "WC")
        # «WC7» -- уайлд-кард и 7-й посев одновременно
        self.assertEqual((e[25]["last"], e[25]["entry"], e[25]["seed"]), ("Tien", "WC", 7))
        self.assertEqual(e[30]["entry"], "LL")
        # фамилия в нормальном регистре -- из колонки результатов, а не из «DE MINAUR»
        self.assertEqual(e[17]["last"], "de Minaur")
        # обрезанное имя в списке
        self.assertTrue(e[32]["truncated"])
        self.assertEqual(e[32]["last"], "Auger-Aliassime")

    def test_results_and_tiebreaks(self):
        d = parse("beijing_2026_mds.pdf")
        m = match(d, "R32", 1)
        self.assertEqual((m["top"], m["bottom"], m["winner"], m["score"]), (1, 2, "top", "7-6(1), 6-4"))
        # Medvedev–Carreno Busta ещё идёт: пара есть, победителя нет
        m = match(d, "R32", 5)
        self.assertEqual((m["top"], m["bottom"], m["winner"]), (9, 10, None))
        # R16 Khachanov–Molcan сыгран, и дальше Khachanov стоит в QF
        self.assertEqual(match(d, "R16", 8)["winner"], "bottom")
        self.assertEqual(match(d, "QF", 4)["bottom"], 31)

    def test_byes_and_full_run_to_champion(self):
        d = parse("chengdu_2026_mds.pdf")
        e = by_pos(d)
        self.assertTrue(e[2]["bye"])
        self.assertEqual(d["drawSize"], 28)
        # у посеянного с bye матча первого круга нет
        self.assertFalse(any(m["pos"] == 1 for m in d["rounds"][0]["matches"]))
        final = match(d, "F", 1)
        self.assertEqual(e[final["bottom"]]["last"], "Davidovich Fokina")
        self.assertEqual((final["winner"], final["score"]), ("bottom", "6-4, 7-6(7)"))
        self.assertEqual(d["category"], "ATP 250")

    def test_two_page_96_draw(self):
        d = parse("cincinnati_2026_mds.pdf")
        self.assertEqual((d["drawSize"], d["slots"], d["category"]), (96, 128, "ATP 1000"))
        self.assertEqual([r["code"] for r in d["rounds"]], ["R128", "R64", "R32", "R16", "QF", "SF", "F"])
        played = sum(1 for r in d["rounds"] for m in r["matches"] if m["winner"])
        self.assertEqual(played, 95)
        final = match(d, "F", 1)
        self.assertEqual((by_pos(d)[final["top"]]["last"], final["score"]), ("Fils", "6-3, 1-6, 6-0"))
        # надстрочный тай-брейк в 96-сетке (имена 6.0pt, счёт 6.7, тай-брейк 5.6)
        self.assertIn("7-6(4)", match(d, "R32", 1)["score"])
        # отказ по ходу матча: Fucsovics–Atmane, позиции 5–6 (у 1–2 bye)
        self.assertEqual(match(d, "R128", 3)["outcome"], "retirement")
        self.assertEqual(d["warnings"], [])


class PlanTests(unittest.TestCase):

    def draw(self):
        # сетка на 4: SF#1 = позиции 1-2, SF#2 = 3-4
        return {
            "edition": "x_2026", "slots": 4,
            "entries": [{"pos": i, "last": n} for i, n in enumerate(["A", "B", "C", "D"], start=1)],
            "rounds": [
                {"code": "SF", "matches": [
                    {"pos": 1, "top": 1, "bottom": 2, "winner": "bottom", "score": "6-4, 7-6(3)", "outcome": "normal"},
                    {"pos": 2, "top": 3, "bottom": 4, "winner": None, "score": None, "outcome": None},
                ]},
                {"code": "F", "matches": [
                    {"pos": 1, "top": 2, "bottom": None, "winner": None, "score": None, "outcome": None},
                ]},
            ],
        }

    pos_player = {1: 101, 2: 102, 3: 103, 4: 104}

    def test_depth_unifies_round_dictionaries(self):
        self.assertEqual(depth("R1", 7), depth("R128", 7))
        self.assertEqual(depth("R2", 5), depth("R16", 5))
        self.assertIsNone(depth("Q1", 5))

    def test_reuses_rows_and_fixes_wrong_winner(self):
        rows = [
            # шард записал победителем не того -- PDF прав
            {"id": 7, "round": "R1", "bracket_pos": None, "status": "completed", "import_key": "a",
             "players": {1: 102, 2: 101}},
            # заглушка «игрок + TBD» на той же глубине
            {"id": 8, "round": "SF", "bracket_pos": None, "status": "scheduled", "import_key": "b",
             "players": {1: 103}},
        ]
        plan = plan_edition(self.draw(), rows, [], self.pos_player)
        sf1 = next(u for u in plan["upserts"] if u["round"] == "SF" and u["bracket_pos"] == 1)
        self.assertEqual((sf1["row_id"], sf1["side1"], sf1["side2"], sf1["winner_side"]), (7, 101, 102, 2))
        # счёт глазами победителя -> глазами сторон
        self.assertEqual(sf1["sets"], [(4, 6, None), (6, 7, 3)])
        sf2 = next(u for u in plan["upserts"] if u["round"] == "SF" and u["bracket_pos"] == 2)
        self.assertEqual((sf2["row_id"], sf2["status"]), (8, None))
        final = next(u for u in plan["upserts"] if u["round"] == "F")
        self.assertIsNone(final["row_id"])
        self.assertEqual(final["import_key"], "draw:x_2026:F:1")
        self.assertEqual(plan["cancel"], [])

    def test_unplaced_rows_are_cancelled_not_deleted(self):
        rows = [{"id": 9, "round": "SF", "bracket_pos": None, "status": "completed", "import_key": "z",
                 "players": {1: 103, 2: 999}}]  # пары 103–999 в сетке нет
        plan = plan_edition(self.draw(), rows, [], self.pos_player)
        self.assertEqual(plan["cancel"], [9])

    def test_shard_supplies_time_and_interim_result(self):
        recs = [{"round": "R1", "start_at": "2026-10-03T12:00:00+08:00", "player_ids": (103, 104),
                 "status": "completed", "winner_id": 104, "outcome": "normal",
                 "sets_by_player": [({103: 3, 104: 6}, None), ({103: 4, 104: 6}, None)]}]
        plan = plan_edition(self.draw(), [], recs, self.pos_player)
        sf2 = next(u for u in plan["upserts"] if u["round"] == "SF" and u["bracket_pos"] == 2)
        self.assertEqual(sf2["scheduled_at"], "2026-10-03T12:00:00+08:00")
        self.assertEqual((sf2["status"], sf2["winner_side"], sf2["sets"]), ("completed", 2, [(3, 6, None), (4, 6, None)]))

    def test_sets_for_sides(self):
        self.assertEqual(sets_for_sides("7-6(1), 6-4", "top"), [(7, 6, 1), (6, 4, None)])
        self.assertEqual(sets_for_sides("7-6(1), 6-4", "bottom"), [(6, 7, 1), (4, 6, None)])


class ResolvePlayerTests(unittest.TestCase):
    players = [
        {"id": 1, "slug": "burruchaga", "first": "Andres", "last": "Burruchaga", "display": "Andres Burruchaga", "tracked": False},
        {"id": 2, "slug": "tirante", "first": "Agustin", "last": "Tirante", "display": "Agustin Tirante", "tracked": False},
        {"id": 3, "slug": "ugo_carabelli", "first": "Ugo", "last": "Carabelli", "display": "Ugo Carabelli", "tracked": False},
        {"id": 4, "slug": "jm_cerundolo", "first": "Jm", "last": "Cerundolo", "display": "Jm Cerundolo", "tracked": True},
        {"id": 5, "slug": "f_cerundolo", "first": "F.", "last": "Cerundolo", "display": "F. Cerundolo", "tracked": True},
    ]

    def resolve(self, first, last):
        p = resolve_player({"first": first, "last": last}, self.players)
        return p and p["slug"]

    def test_brothers_are_different_people(self):
        self.assertIsNone(self.resolve("Roman", "Burruchaga"))
        self.assertEqual(self.resolve("Andres", "Burruchaga"), "burruchaga")

    def test_second_given_name(self):
        self.assertEqual(self.resolve("Thiago Agustin", "Tirante"), "tirante")

    def test_compound_surname_restored_from_slug(self):
        self.assertEqual(self.resolve("Camilo", "Ugo Carabelli"), "ugo_carabelli")

    def test_same_surname_split_by_initial(self):
        self.assertEqual(self.resolve("Juan M", "Cerundolo"), "jm_cerundolo")
        self.assertEqual(self.resolve("Francisco", "Cerundolo"), "f_cerundolo")


if __name__ == "__main__":
    unittest.main()
