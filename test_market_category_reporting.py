"""Reporting labels and independent category performance accounting."""
import unittest

from market_category_registry import market_display_group
from market_category_reporting import build_research_category_performance


class MarketDisplayGroupTests(unittest.TestCase):
    def test_sports_leagues_are_separated_for_reporting_only(self):
        self.assertEqual(market_display_group("sports", "KXNFLGAME-26OCT", "Team A at Team B"), "NFL")
        self.assertEqual(market_display_group("sports", "KXNBA-GAME", "Team A at Team B"), "NBA")
        self.assertEqual(market_display_group("sports", "KXNHLGAME", "Team A at Team B"), "NHL")
        self.assertEqual(market_display_group("sports", "KXWNBA-GAME", "Team A at Team B"), "WNBA")
        self.assertEqual(market_display_group("sports", "UNKNOWN", "Local club winner?"), "Other Sports")

    def test_non_sports_categories_have_clear_reporting_groups(self):
        self.assertEqual(market_display_group("politics", "KXPRES"), "Politics")
        self.assertEqual(market_display_group("crypto_event", "KXETH"), "Crypto Events")
        self.assertEqual(market_display_group("btc_15m", "KXBTC15M-TEST"), "BTC 15-minute")
        self.assertEqual(market_display_group("mystery", "KXUNKNOWN"), "Other / Unclassified")


class CategoryPerformanceTests(unittest.TestCase):
    def test_open_and_settled_counts_are_separated_and_pnl_is_net(self):
        rows = [
            ("sports", "settled", "2026-10-24T01:00:00+00:00", 8.0,
             '{"decision":{"display_group":"NFL"}}', "KXNFL-1"),
            ("sports", "settled", "2026-10-24T02:00:00+00:00", -13.0,
             '{"decision":{"display_group":"NFL"}}', "KXNFL-2"),
            ("sports", "open", None, None,
             '{"decision":{"display_group":"NBA"}}', "KXNBA-1"),
            ("politics", "settled", "2026-10-24T03:00:00+00:00", 20.0,
             '{"decision":{"display_group":"Politics"}}', "KXPOL-1"),
        ]
        report = {item["display_group"]: item for item in build_research_category_performance(rows)}
        nfl = report["NFL"]
        self.assertEqual(nfl["total_trades"], 2)
        self.assertEqual(nfl["settled_trades"], 2)
        self.assertEqual(nfl["wins"], 1)
        self.assertEqual(nfl["losses"], 1)
        self.assertEqual(nfl["net_pnl_cents"], -5.0)
        self.assertEqual(nfl["win_rate_percent"], 50.0)
        self.assertEqual(report["NBA"]["open_trades"], 1)
        self.assertIsNone(report["NBA"]["win_rate_percent"])
        self.assertEqual(report["Politics"]["net_pnl_cents"], 20.0)

    def test_empty_groups_are_explicitly_reported_as_empty(self):
        report = {item["display_group"]: item for item in build_research_category_performance([])}
        self.assertEqual(report["NFL"]["total_trades"], 0)
        self.assertIsNone(report["NFL"]["win_rate_percent"])
        self.assertEqual(report["NFL"]["sample_status"], "No paper trades recorded")


if __name__ == "__main__":
    unittest.main()
