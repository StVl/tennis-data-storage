"""WTA в шардах и миграции: тур игрока, слаги розыгрышей, рейтинг и проверка шардов.

  python3 -m unittest tests.test_migrate_wta

Без базы: проверяются чистые функции migrate_data.py и build_config.validate."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import build_config  # noqa: E402
import migrate_data  # noqa: E402


def shards(players=None, rankings=None, tournaments=None):
    players = players or [
        {"id": "sinner", "name": "Jannik Sinner", "playStyleId": "baseliner"},
        {"id": "swiatek", "name": "Iga Swiatek", "playStyleId": "baseliner", "gender": "f"},
    ]
    rankings = rankings or [
        {"playerId": "sinner", "current": 1, "trend": "stable"},
        {"playerId": "swiatek", "current": 2, "trend": "stable", "tour": "wta"},
    ]
    return {
        "reference": {"playStyles": [{"id": "baseliner"}]},
        "players": {"players": players},
        "rankings": {"rankings": rankings},
        "matches_upcoming": {"matches": []},
        "matches_past": {"matches": []},
        "tournaments_upcoming": {"tournaments": tournaments or []},
        "tournaments_past": {"tournaments": []},
    }


class EditionSlugTest(unittest.TestCase):
    def test_edition_slug(self):
        self.assertEqual(migrate_data.edition_slug("wimbledon", "atp", 2026), "wimbledon_2026")
        self.assertEqual(migrate_data.edition_slug("wimbledon", "wta", 2026), "wimbledon_wta_2026")

    def test_tour_for_gender(self):
        self.assertEqual(migrate_data.tour_for_gender("m"), "atp")
        self.assertEqual(migrate_data.tour_for_gender("f"), "wta")
        self.assertEqual(migrate_data.tour_for_gender(None), "atp")


class MatchEditionTest(unittest.TestCase):
    def setUp(self):
        self.genders = {"sinner": "m", "swiatek": "f"}

    def test_match_goes_to_players_tour_edition(self):
        women = {"playerId": "swiatek", "tournament": "Wimbledon 2026"}
        men = {"playerId": "sinner", "tournament": "Wimbledon 2026"}
        self.assertEqual(migrate_data.match_edition_slug(women, self.genders), "wimbledon_wta_2026")
        self.assertEqual(migrate_data.match_edition_slug(men, self.genders), "wimbledon_2026")

    def test_tournament_shard_name_is_a_fallback_alias(self):
        names = migrate_data.tournament_names([
            {"tournament": "wuhan", "tour": "wta", "name": "Wuhan Open"},
            {"tournament": "wimbledon", "name": "The Championships, Wimbledon"},
        ])
        m = {"playerId": "swiatek", "tournament": "Wuhan Open 2026"}
        self.assertEqual(migrate_data.match_edition_slug(m, self.genders, names), "wuhan_wta_2026")
        # ручной алиас важнее названия из шарда
        w = {"playerId": "swiatek", "tournament": "Wimbledon 2026"}
        self.assertEqual(migrate_data.match_edition_slug(w, self.genders, names), "wimbledon_wta_2026")

    def test_unknown_tournament_has_no_edition(self):
        m = {"playerId": "swiatek", "tournament": "Nowhere Open 2026"}
        self.assertIsNone(migrate_data.match_edition_slug(m, self.genders))

    def test_outsider_inherits_gender_of_roster_opponent(self):
        matches = [
            {"playerId": "swiatek", "opponentId": "svitolina"},
            {"playerId": "sinner", "opponentId": "khachanov"},
            {"playerId": "sinner", "opponentId": "TBD"},
        ]
        self.assertEqual(
            migrate_data.outsider_genders(matches, self.genders),
            {"svitolina": "f", "khachanov": "m"},
        )


class TournamentTourTest(unittest.TestCase):
    def test_brand_tour_is_wta_only_without_atp_record(self):
        tournaments = [
            {"tournament": "madrid", "tour": "wta"},
            {"tournament": "madrid"},
            {"tournament": "wuhan", "tour": "wta"},
        ]
        self.assertEqual(
            migrate_data.brand_tours(tournaments), {"madrid": "atp", "wuhan": "wta"})


class RankingTourTest(unittest.TestCase):
    def test_ranking_row_uses_tour(self):
        self.assertEqual(migrate_data.ranking_tour({"playerId": "swiatek", "tour": "wta"}), "wta")
        self.assertEqual(migrate_data.ranking_tour({"playerId": "sinner"}), "atp")


class ShardValidationTest(unittest.TestCase):
    def test_wta_shards_pass(self):
        self.assertEqual(build_config.validate(shards()), [])

    def test_build_check_rejects_unknown_gender(self):
        s = shards(players=[{"id": "sinner", "name": "Jannik Sinner", "playStyleId": "baseliner",
                             "gender": "x"}],
                   rankings=[{"playerId": "sinner", "current": 1, "trend": "stable"}])
        self.assertTrue(any("gender" in e for e in build_config.validate(s)))

    def test_build_check_rejects_ranking_from_other_tour(self):
        s = shards(rankings=[
            {"playerId": "sinner", "current": 1, "trend": "stable"},
            {"playerId": "swiatek", "current": 2, "trend": "stable"},  # без tour = atp, а она f
        ])
        self.assertTrue(any("swiatek" in e and "tour" in e for e in build_config.validate(s)))

    def test_build_check_rejects_duplicate_tournament_tour(self):
        s = shards(tournaments=[{"tournament": "madrid", "tour": "wta"},
                                {"tournament": "madrid", "tour": "wta"}])
        self.assertTrue(any("madrid" in e for e in build_config.validate(s)))


if __name__ == "__main__":
    unittest.main()


class SyncRobustnessTest(unittest.TestCase):
    """2026-10-23: the tournaments agent wrote courtType "outdoor_hard" for the new WTA events and
    the whole sync rolled back (enum surface_t has no such value) — ATP updates stalled with it."""

    def test_outdoor_hard_is_hard(self):
        self.assertEqual(migrate_data.normalize_surface("outdoor_hard"), ("hard", False))
        self.assertEqual(migrate_data.normalize_surface("indoor_hard"), ("hard", True))

    def test_build_check_rejects_unknown_court_type(self):
        s = shards(tournaments=[{"tournament": "wuhan", "tour": "wta", "name": "Wuhan Open",
                                 "courtType": "hardcourt"}])
        self.assertTrue(any("courtType" in e for e in build_config.validate(s)))
        s = shards(tournaments=[{"tournament": "wuhan", "tour": "wta", "name": "Wuhan Open",
                                 "courtType": "outdoor_hard"}])
        self.assertEqual(build_config.validate(s), [])

    def test_sofascore_names_of_wta_events_resolve(self):
        genders = {"rybakina": "f"}
        for name, edition in [("Ningbo Open 2026", "ningbo_wta_2026"),
                              ("Japan Women's Open 2026", "japan_womens_open_wta_2026"),
                              ("Pan Pacific Open 2026", "pan_pacific_open_wta_2026"),
                              ("Guangzhou Open 2026", "guangzhou_wta_2026"),
                              ("Hong Kong Tennis Open 2026", "hong_kong_wta_2026"),
                              ("Chennai Open 2026", "chennai_wta_2026"),
                              ("WTA Finals 2026", "wta_finals_wta_2026")]:
            m = {"playerId": "rybakina", "tournament": name}
            self.assertEqual(migrate_data.match_edition_slug(m, genders), edition, name)
