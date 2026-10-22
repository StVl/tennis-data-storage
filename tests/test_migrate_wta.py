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
