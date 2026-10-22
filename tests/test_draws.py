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


def parse_wta(name):
    from draw_pdf import parse_draw_pdf
    return parse_draw_pdf((FIXTURES / name).read_bytes(), source="wta")


@unittest.skipUnless(HAVE_PDF, "нужен pdfplumber")
class WtaDrawPdfTests(unittest.TestCase):
    """PDF WTA (wtafiles.wtatennis.com/pdf/draws/<год>/<id>/MDS.pdf): Washington 2026
    (WTA 500, 28 игроков + 4 bye), Antalya 2026 (WTA 125, 32), Indian Wells 2026 (96, две страницы)."""

    def test_wta_32_parses(self):
        d = parse_wta("wta_1045.pdf")
        self.assertEqual((d["title"], d["location"]), ("Mubadala DC Open", "Washington DC, USA"))
        self.assertEqual((d["startDate"], d["endDate"]), ("2026-07-27", "2026-08-02"))
        self.assertEqual(d["released"], "2026-08-03T13:54:00")
        # у WTA размер сетки -- тоже без bye: 28 (singlesDrawSize в API WTA)
        self.assertEqual((d["drawSize"], d["slots"]), (28, 32))
        e = by_pos(d)
        self.assertEqual((e[1]["last"], e[1]["first"], e[1]["seed"], e[1]["country"]), ("Pegula", "Jessica", 1, "USA"))
        self.assertTrue(e[2]["bye"])
        self.assertEqual((e[12]["last"], e[12]["entry"], e[12]["seed"]), ("Williams", "WC", None))
        self.assertEqual((e[32]["last"], e[32]["seed"]), ("Svitolina", 2))
        # у нейтральных игроков страны нет
        self.assertEqual((e[15]["last"], e[15]["country"]), ("Samsonova", None))
        final = match(d, "F", 1)
        self.assertEqual((e[final["top"]]["last"], e[final["bottom"]]["last"]), ("Pegula", "Eala"))
        self.assertEqual((final["winner"], final["score"]), ("bottom", "4-6, 6-4, 6-0"))
        # тай-брейк в WTA пишется в строку: «76(7)»
        self.assertEqual(match(d, "R16", 2)["score"], "1-6, 6-3, 7-6(7)")
        # «Xin. Wang» -- инициал из трёх букв
        self.assertEqual(match(d, "R32", 7)["winner"], "bottom")
        self.assertEqual(d["warnings"], [])

        d = parse_wta("wta_1107.pdf")
        self.assertEqual((d["drawSize"], d["slots"]), (32, 32))
        self.assertEqual((d["startDate"], d["endDate"]), ("2026-03-01", "2026-03-08"))
        e = by_pos(d)
        # имя перенесено на вторую строку
        self.assertEqual((e[7]["last"], e[7]["first"]), ("Maristany Zuleta de Reales", "Guiomar"))
        self.assertEqual((match(d, "R32", 4)["winner"], match(d, "R32", 4)["score"]), ("top", "6-1, 7-5"))
        self.assertEqual(e[match(d, "F", 1)["top"]]["last"], "Oliynykova")
        self.assertEqual(d["warnings"], [])

    def test_wta_1000_two_pages(self):
        d = parse_wta("wta_609.pdf")
        self.assertEqual((d["drawSize"], d["slots"]), (96, 128))
        self.assertEqual((d["startDate"], d["endDate"]), ("2026-03-04", "2026-03-15"))
        self.assertEqual([r["code"] for r in d["rounds"]], ["R128", "R64", "R32", "R16", "QF", "SF", "F"])
        e = by_pos(d)
        # посев -- отдельно от фамилии
        self.assertEqual((e[8]["last"], e[8]["seed"]), ("Joint", 29))
        self.assertEqual((e[1]["last"], e[1]["seed"]), ("Sabalenka", 1))
        self.assertEqual((e[29]["last"], e[29]["entry"]), ("Galfi", "LL"))
        played = sum(1 for r in d["rounds"] for m in r["matches"] if m["winner"])
        self.assertEqual(played, 95)
        final = match(d, "F", 1)
        self.assertEqual(e[final["top"]]["last"], "Sabalenka")
        self.assertEqual((final["winner"], final["score"]), ("top", "3-6, 6-3, 7-6(6)"))
        # «62 20 RET»: Eala выходит в R16 по отказу
        self.assertEqual((match(d, "R32", 5)["outcome"], match(d, "R32", 5)["score"]), ("retirement", "6-2, 2-0"))
        self.assertEqual(d["warnings"], [])

    def test_injury_note_is_not_part_of_name(self):
        # «R. Sramkova Lower Back Injury» -- из блока Retirements под сеткой, не результат
        d = parse_wta("wta_609.pdf")
        self.assertFalse(any("Injury" in w for w in d["warnings"]), d["warnings"])
        names = [x.get("last", "") + " " + (x.get("first") or "") for x in d["entries"]]
        self.assertFalse(any("Injury" in n or "2026" in n for n in names), names)
        # «15 Mar 2026» из строки RELEASED -- не позиция 15
        self.assertEqual(by_pos(d)[15], {"pos": 15, "bye": True})
        self.assertEqual(match(d, "SF", 1)["winner"], "top")
        d = parse_wta("wta_1045.pdf")
        self.assertFalse(any("injury" in w.lower() for w in d["warnings"]), d["warnings"])

    def test_wta_category_from_points(self):
        from draw_pdf import category_for_points
        self.assertEqual(parse_wta("wta_1045.pdf")["category"], "WTA 500")
        self.assertEqual(parse_wta("wta_609.pdf")["category"], "WTA 1000")
        d = parse_wta("wta_1107.pdf")
        self.assertEqual((d["points"], d["category"]), (125, None))  # WTA 125 не ведём
        self.assertEqual(category_for_points("wta", 250), "WTA 250")
        self.assertEqual(category_for_points("wta", 2000), "Grand Slam")
        self.assertEqual(category_for_points("atp", 1000), "ATP 1000")
        self.assertIsNone(category_for_points("wta", 125))


class ImportDrawsTests(unittest.TestCase):

    def test_draw_shard_path_for_wta(self):
        import import_draws as imp
        tournaments = [
            {"tournament": "washington", "dates": {"start": "2026-07-27", "end": "2026-08-02"}},
            {"tournament": "washington", "tour": "wta", "dates": {"start": "2026-07-27", "end": "2026-08-02"}},
        ]
        eds = imp.editions_from(tournaments)
        self.assertEqual(set(eds), {"washington_2026", "washington_wta_2026"})
        self.assertEqual(eds["washington_wta_2026"][:2], ("washington", "wta"))
        self.assertEqual(imp.shard_path("washington_wta_2026"), imp.DRAWS / "washington_wta_2026.json")
        self.assertEqual(imp.source_url("wta", 2026, 1045),
                         "https://wtafiles.wtatennis.com/pdf/draws/2026/1045/MDS.pdf")
        self.assertEqual(set(imp.SOURCES), {"atp", "wta"})
        shard = imp.to_shard("washington_wta_2026", "wta", 1045, "u",
                             {"drawSize": 28, "slots": 32, "entries": [], "rounds": []})
        self.assertEqual((shard["tour"], shard["sourceId"]), ("wta", 1045))
        self.assertNotIn("atpId", shard)

    def test_due_editions_use_ids_of_their_tour(self):
        import datetime
        import import_draws as imp
        eds = {
            "washington_wta_2026": ("washington", "wta", datetime.date(2026, 7, 27), datetime.date(2026, 8, 2)),
            "los_cabos_wta_2026": ("los_cabos", "wta", datetime.date(2026, 7, 27), datetime.date(2026, 8, 2)),
            "los_cabos_2026": ("los_cabos", "atp", datetime.date(2026, 7, 27), datetime.date(2026, 8, 2)),
        }
        self.assertEqual(imp.due_editions(eds, datetime.date(2026, 7, 28)),
                         ["los_cabos_2026", "washington_wta_2026"])

    def test_source_meta_reads_old_atp_id(self):
        from migrate_draws import source_meta
        self.assertEqual(source_meta({"atpId": 418}), {"atp_id": 418})
        self.assertEqual(source_meta({"tour": "atp", "sourceId": 418}), {"atp_id": 418})
        self.assertEqual(source_meta({"tour": "wta", "sourceId": 1045}), {"wta_id": 1045})


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

    def test_wta_draw_resolves_only_women(self):
        from migrate_draws import players_of_tour
        players = [
            {"id": 10, "slug": "wang_yunchaokete", "first": "Yunchaokete", "last": "Bu", "display": "Bu", "tracked": False, "gender": "m"},
            {"id": 11, "slug": "x_wang", "first": "Xinyu", "last": "Wang", "display": "Xinyu Wang", "tracked": False, "gender": "m"},
            {"id": 12, "slug": "xinyu_wang", "first": "Xinyu", "last": "Wang", "display": "Xinyu Wang", "tracked": True, "gender": "f"},
        ]
        p = resolve_player({"first": "Xinyu", "last": "Wang"}, players_of_tour(players, "wta"))
        self.assertEqual(p["id"], 12)
        self.assertEqual([q["id"] for q in players_of_tour(players, "atp")], [10, 11])


if __name__ == "__main__":
    unittest.main()
