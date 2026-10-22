"""Сид WTA-ростера из снимка рейтинга и сборка config.json без WTA.

  python3 -m unittest tests.test_seed_wta"""

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import build_config  # noqa: E402
import seed_wta_roster as seed  # noqa: E402

SNAPSHOT = {
    "source": "https://www.wtatennis.com/rankings/singles",
    "fetchedAt": "2026-10-22",
    "rows": [
        {"rank": 1, "move": "-", "wtaId": 324166, "name": "Elena Rybakina", "country": "KAZ", "points": 9196},
        {"rank": 22, "move": "1", "wtaId": 328120, "name": "Qinwen Zheng", "country": "CHN", "points": 2018},
        {"rank": 27, "move": "-1", "wtaId": 325601, "name": "Anastasia Potapova", "country": "AUT", "points": 1814},
    ],
}


def base_shards():
    return {
        "reference": {"playStyles": [{"id": "baseliner"}]},
        "players": {"players": [{"id": "sinner", "name": "Jannik Sinner", "playStyleId": "baseliner"}]},
        "rankings": {"rankings": [{"playerId": "sinner", "current": 1, "seasonDelta": 0, "trend": "stable",
                                   "ytdPoints": 10950, "seasonPoints": 9150}]},
        "matches_upcoming": {"matches": [
            {"id": "sinner_x", "playerId": "sinner", "opponentId": "zheng", "status": "upcoming",
             "tournament": "Paris Masters 2026", "startAt": "2026-10-28T12:00:00+01:00"}]},
        "matches_past": {"matches": []},
        "tournaments_upcoming": {"tournaments": []},
        "tournaments_past": {"tournaments": []},
    }


class SeedTest(unittest.TestCase):
    def test_slug_is_last_name_unless_taken(self):
        self.assertEqual(seed.player_slug("Elena Rybakina", taken=set()), "rybakina")
        self.assertEqual(seed.player_slug("Qinwen Zheng", taken={"zheng"}), "qinwen_zheng")

    def test_trend_from_weekly_move(self):
        self.assertEqual(seed.trend("-"), "stable")
        self.assertEqual(seed.trend("3"), "up")
        self.assertEqual(seed.trend("-2"), "down")

    def test_seed_adds_wta_players_and_rankings(self):
        shards = base_shards()
        seed.apply(shards, SNAPSHOT)
        women = [p for p in shards["players"]["players"] if p.get("gender") == "f"]
        self.assertEqual([p["id"] for p in women], ["rybakina", "qinwen_zheng", "potapova"])
        self.assertEqual(women[0]["wtaId"], 324166)
        self.assertEqual(women[0]["links"]["profile"],
                         "https://www.wtatennis.com/players/324166/elena-rybakina")
        self.assertNotIn("playStyleId", women[0])
        ranks = {r["playerId"]: r for r in shards["rankings"]["rankings"]}
        self.assertEqual(ranks["rybakina"],
                         {"playerId": "rybakina", "tour": "wta", "current": 1, "trend": "stable", "ytdPoints": 9196})
        self.assertEqual(ranks["qinwen_zheng"]["trend"], "up")
        self.assertEqual(build_config.validate(shards), [])

    def test_seed_is_idempotent_and_updates_rank(self):
        shards = base_shards()
        seed.apply(shards, SNAPSHOT)
        once = copy.deepcopy(shards)
        seed.apply(shards, SNAPSHOT)
        self.assertEqual(shards, once)

        moved = copy.deepcopy(SNAPSHOT)
        moved["rows"][0].update(rank=2, move="-1", points=9000)
        seed.apply(shards, moved)
        ranks = {r["playerId"]: r for r in shards["rankings"]["rankings"]}
        self.assertEqual((ranks["rybakina"]["current"], ranks["rybakina"]["trend"]), (2, "down"))
        self.assertEqual(sum(1 for p in shards["players"]["players"] if p["id"] == "rybakina"), 1)


class ConfigTest(unittest.TestCase):
    def test_config_json_stays_atp_only(self):
        # config.json читают старые версии приложения: WTA туда не попадает.
        shards = base_shards()
        seed.apply(shards, SNAPSHOT)
        shards["matches_upcoming"]["matches"].append(
            {"id": "rybakina_x", "playerId": "rybakina", "opponentId": "TBD", "status": "upcoming",
             "tournament": "Wuhan Open 2026", "startAt": "2026-10-28T12:00:00+08:00"})
        shards["tournaments_upcoming"]["tournaments"].append(
            {"tournament": "wuhan", "tour": "wta", "name": "Wuhan Open"})
        config = build_config.build(shards)
        self.assertEqual([p["id"] for p in config["players"]], ["sinner"])
        self.assertEqual([m["id"] for m in config["matches"]], ["sinner_x"])
        self.assertEqual(config["tournaments"], [])
        self.assertEqual([c["playerId"] for c in config["playerCards"]], ["sinner"])


if __name__ == "__main__":
    unittest.main()
