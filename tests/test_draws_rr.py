"""Тесты итогового турнира (группы + плей-офф): разбор PDF ATP Finals и план заливки RR.

  python3 -m unittest discover -s tests

Фикстуры -- настоящие PDF protennislive.com/posting/<год>/605/mds.pdf трёх разных шаблонов:
2024 и 2025 (завершены, «ALCARAZ, Carlos», дат в шапке нет), 2026 (до старта, «Carlos
Alcaraz», даты в шапке, результатов ещё нет)."""

import datetime
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

from migrate_draws import plan_edition  # noqa: E402


def parse(name):
    from draw_pdf import parse_draw_pdf
    return parse_draw_pdf((FIXTURES / name).read_bytes())


def last_names(d):
    return {e["pos"]: e["last"] for e in d["entries"]}


def rnd(d, code):
    return next(r for r in d["rounds"] if r["code"] == code)["matches"]


def pair_result(d, a, b):
    """Матч группы по фамилиям: (фамилия победителя, счёт глазами победителя)."""
    names = last_names(d)
    for m in rnd(d, "RR"):
        if {names[m["top"]], names[m["bottom"]]} == {a, b}:
            return (names[m[m["winner"]]] if m["winner"] else None), m["score"]
    raise AssertionError(f"нет пары {a}–{b}")


def ko(d, code, pos):
    names = last_names(d)
    m = next(m for m in rnd(d, code) if m["pos"] == pos)
    side = lambda p: names.get(p)  # noqa: E731
    return side(m["top"]), side(m["bottom"]), (side(m[m["winner"]]) if m["winner"] else None), m["score"]


@unittest.skipUnless(HAVE_PDF, "нужен pdfplumber")
class RoundRobinPdfTests(unittest.TestCase):

    def test_knockout_pdfs_are_not_round_robin(self):
        import pdfplumber
        from draw_pdf import is_round_robin_page
        for name in ("beijing_2026_mds.pdf", "cincinnati_2026_mds.pdf", "wta_1045.pdf"):
            with pdfplumber.open(FIXTURES / name) as pdf:
                self.assertFalse(is_round_robin_page(pdf.pages[0]), name)

    def test_2026_before_start(self):
        d = parse("atp_finals_2026_mds.pdf")
        self.assertEqual(d["format"], "round_robin")
        self.assertEqual((d["title"], d["location"]), ("Nitto ATP Finals", "Turin, Italy"))
        self.assertEqual((d["startDate"], d["endDate"]), ("2026-11-15", "2026-11-22"))
        self.assertEqual(d["released"], "2026-11-12T12:23:15")
        self.assertEqual((d["drawSize"], d["slots"]), (8, 4))
        self.assertEqual([g["name"] for g in d["groups"]], ["John McEnroe Group", "Ivan Lendl Group"])
        self.assertEqual([g["positions"] for g in d["groups"]], [[1, 2, 3, 4], [5, 6, 7, 8]])
        e = {x["pos"]: x for x in d["entries"]}
        self.assertEqual((e[1]["first"], e[1]["last"], e[1]["seed"], e[1]["country"]), ("Alexander", "Zverev", 1, "GER"))
        self.assertEqual((e[5]["last"], e[5]["seed"]), ("Alcaraz", 2))
        self.assertIsNone(e[6]["country"])  # Medvedev -- нейтральный
        # все 12 пар групп известны заранее, сыгранных нет; плей-офф пуст
        self.assertEqual(len(rnd(d, "RR")), 12)
        self.assertFalse(any(m["winner"] for m in rnd(d, "RR")))
        self.assertEqual([(m["top"], m["bottom"]) for m in rnd(d, "SF") + rnd(d, "F")], [(None, None)] * 3)
        self.assertEqual(d["warnings"], [])

    def test_2025_full_tournament(self):
        d = parse("atp_finals_2025_mds.pdf")
        self.assertEqual([g["name"] for g in d["groups"]], ["Jimmy Connors Group", "Bjorn Borg Group"])
        names = last_names(d)
        self.assertEqual(sorted(names.values()), sorted(
            ["Alcaraz", "Fritz", "de Minaur", "Musetti", "Sinner", "Zverev", "Shelton", "Auger-Aliassime"]))
        self.assertTrue(all(m["winner"] for m in rnd(d, "RR")))
        self.assertEqual(pair_result(d, "Alcaraz", "Fritz"), ("Alcaraz", "6-7(2), 7-5, 6-3"))
        self.assertEqual(pair_result(d, "de Minaur", "Musetti"), ("Musetti", "7-5, 3-6, 7-5"))
        self.assertEqual(pair_result(d, "Shelton", "Auger-Aliassime"), ("Auger-Aliassime", "4-6, 7-6(7), 7-5"))
        # SF#1 = 1-е место группы 1 против 2-го группы 2, SF#2 -- наоборот
        self.assertEqual(ko(d, "SF", 1), ("Alcaraz", "Auger-Aliassime", "Alcaraz", "6-2, 6-4"))
        self.assertEqual(ko(d, "SF", 2), ("Sinner", "de Minaur", "Sinner", "7-5, 6-2"))
        self.assertEqual(ko(d, "F", 1), ("Alcaraz", "Sinner", "Sinner", "7-6(4), 7-5"))
        self.assertEqual(d["warnings"], [])

    def test_2024_older_template(self):
        d = parse("atp_finals_2024_mds.pdf")
        self.assertIsNone(d["startDate"])
        self.assertEqual(d["released"], "2024-11-17T20:07:00")
        self.assertEqual(pair_result(d, "Fritz", "Medvedev"), ("Fritz", "6-4, 6-3"))
        self.assertEqual(pair_result(d, "Alcaraz", "Rublev"), ("Alcaraz", "6-3, 7-6(8)"))
        self.assertEqual(ko(d, "SF", 1), ("Sinner", "Ruud", "Sinner", "6-1, 6-2"))
        self.assertEqual(ko(d, "SF", 2), ("Zverev", "Fritz", "Fritz", "6-3, 3-6, 7-6(3)"))
        self.assertEqual(ko(d, "F", 1), ("Sinner", "Fritz", "Sinner", "6-4, 6-4"))

    def test_rr_match_numbers_do_not_depend_on_print_order(self):
        # pos -- номер пары внутри групп по возрастанию позиций, а не строка DAILY RESULTS
        d = parse("atp_finals_2025_mds.pdf")
        self.assertEqual([(m["pos"], m["top"], m["bottom"]) for m in rnd(d, "RR")[:6]],
                         [(1, 1, 2), (2, 1, 3), (3, 1, 4), (4, 2, 3), (5, 2, 4), (6, 3, 4)])
        self.assertEqual((rnd(d, "RR")[6]["top"], rnd(d, "RR")[6]["bottom"]), (5, 6))


class RoundRobinNameTests(unittest.TestCase):

    def test_parse_name_both_orders(self):
        from draw_pdf_rr import parse_name
        self.assertEqual(parse_name("ALCARAZ, Carlos"), ("Carlos", "Alcaraz"))
        self.assertEqual(parse_name("DE MINAUR, Alex"), ("Alex", "de Minaur"))
        self.assertEqual(parse_name("Carlos Alcaraz"), ("Carlos", "Alcaraz"))
        self.assertEqual(parse_name("Alex de Minaur"), ("Alex", "de Minaur"))
        self.assertEqual(parse_name("Felix Auger-Aliassime"), ("Felix", "Auger-Aliassime"))
        self.assertEqual(parse_name("CARLOS ALCARAZ"), ("Carlos", "Alcaraz"))

    def test_semifinal_slots_while_one_group_still_plays(self):
        from draw_pdf_rr import _semifinal_slots
        groups = [{"name": "A", "positions": [1, 2, 3, 4]}, {"name": "B", "positions": [5, 6, 7, 8]}]
        warnings = []
        # группа A доиграла: её №1 и №2 сверху вниз -> SF#1 top и SF#2 bottom
        self.assertEqual(_semifinal_slots([{"pos": 3}, {"pos": 1}], groups, warnings), [3, None, None, 1])
        # группа B: сверху её №2 (SF#1 bottom), ниже №1 (SF#2 top)
        self.assertEqual(_semifinal_slots([{"pos": 7}, {"pos": 5}], groups, warnings), [None, 7, 5, None])
        self.assertEqual(warnings, [])


class RoundRobinImportTests(unittest.TestCase):

    def test_dates_fall_back_to_release_date(self):
        import import_draws as imp
        start, end = datetime.date(2025, 11, 9), datetime.date(2025, 11, 16)
        self.assertTrue(imp.dates_match({"released": "2025-11-16T21:03:00"}, start, end))
        self.assertTrue(imp.dates_match({"released": "2025-11-05T10:00:00"}, start, end))
        # прошлогодний документ по тому же адресу -- не этот розыгрыш
        self.assertFalse(imp.dates_match({"released": "2024-11-17T20:07:00"}, start, end))
        self.assertFalse(imp.dates_match({}, start, end))

    def test_shard_keeps_groups_only_for_round_robin(self):
        import import_draws as imp
        base = {"drawSize": 8, "slots": 4, "entries": [], "rounds": []}
        rr = imp.to_shard("atp_finals_2026", "atp", 605, "u",
                          {**base, "format": "round_robin", "groups": [{"name": "A", "positions": [1]}]})
        self.assertEqual((rr["format"], rr["groups"][0]["name"]), ("round_robin", "A"))
        ko_shard = imp.to_shard("beijing_2026", "atp", 747, "u", base)
        self.assertNotIn("format", ko_shard)
        self.assertNotIn("groups", ko_shard)


class RoundRobinPlanTests(unittest.TestCase):
    """Группа из 3 для краткости: позиции 1-3, пары RR#1 1-2, RR#2 1-3, RR#3 2-3; финал 1-2."""

    pos_player = {1: 101, 2: 102, 3: 103}

    def draw(self):
        def m(pos, top, bottom, winner=None, score=None):
            return {"pos": pos, "top": top, "bottom": bottom, "winner": winner, "score": score,
                    "outcome": "normal" if winner else None}
        return {
            "edition": "atp_finals_2026", "slots": 4, "format": "round_robin",
            "entries": [{"pos": p, "last": str(p)} for p in (1, 2, 3)],
            "rounds": [
                {"code": "RR", "matches": [m(1, 1, 2, "top", "6-4, 6-4"), m(2, 1, 3), m(3, 2, 3)]},
                {"code": "SF", "matches": [m(1, None, None), m(2, None, None)]},
                {"code": "F", "matches": [m(1, 1, 2, "bottom", "7-5, 7-5")]},
            ],
        }

    def test_group_rows_from_llm_shard_are_reused_by_pair(self):
        rows = [
            {"id": 1, "round": "RR", "bracket_pos": None, "status": "completed", "import_key": "a",
             "players": {1: 102, 2: 101}},
            {"id": 2, "round": "RR", "bracket_pos": None, "status": "scheduled", "import_key": "b",
             "players": {1: 103, 2: 101}},
        ]
        recs = [{"round": "RR", "start_at": "2026-11-16T14:00:00+01:00", "player_ids": (103, 101),
                 "status": "scheduled", "winner_id": None, "outcome": None, "sets_by_player": []}]
        plan = plan_edition(self.draw(), rows, recs, self.pos_player)
        rr = {u["bracket_pos"]: u for u in plan["upserts"] if u["round"] == "RR"}
        self.assertEqual((rr[1]["row_id"], rr[1]["side1"], rr[1]["winner_side"]), (1, 101, 1))
        self.assertEqual((rr[2]["row_id"], rr[2]["scheduled_at"]), (2, "2026-11-16T14:00:00+01:00"))
        self.assertIsNone(rr[3]["row_id"])
        self.assertEqual(rr[3]["import_key"], "draw:atp_finals_2026:RR:3")
        self.assertEqual(plan["cancel"], [])

    def test_final_rematch_does_not_take_the_group_row(self):
        # финал 1–2 повторяет матч группы: строку RR той же пары финалу не отдаём
        rows = [{"id": 1, "round": "RR", "bracket_pos": None, "status": "completed", "import_key": "a",
                 "players": {1: 101, 2: 102}}]
        plan = plan_edition(self.draw(), rows, [], self.pos_player)
        final = next(u for u in plan["upserts"] if u["round"] == "F")
        group = next(u for u in plan["upserts"] if u["round"] == "RR" and u["bracket_pos"] == 1)
        self.assertEqual((group["row_id"], final["row_id"]), (1, None))

    def test_unknown_group_pair_is_cancelled(self):
        rows = [{"id": 5, "round": "RR", "bracket_pos": None, "status": "scheduled", "import_key": "z",
                 "players": {1: 101, 2: 999}}]
        plan = plan_edition(self.draw(), rows, [], self.pos_player)
        self.assertEqual(plan["cancel"], [5])

    def test_knockout_draw_still_ignores_rr_rows(self):
        draw = self.draw()
        draw["rounds"] = [r for r in draw["rounds"] if r["code"] != "RR"]
        rows = [{"id": 5, "round": "RR", "bracket_pos": None, "status": "scheduled", "import_key": "z",
                 "players": {1: 101, 2: 999}}]
        plan = plan_edition(draw, rows, [], self.pos_player)
        self.assertEqual(plan["cancel"], [])


if __name__ == "__main__":
    unittest.main()
