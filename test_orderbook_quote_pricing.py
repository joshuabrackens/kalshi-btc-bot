"""Tests for consistent bid/ask interpretation from Kalshi's bid-only order book."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
import unittest

# The test runner in the user's repository has all project modules available.
# For isolated local validation, stub optional runtime-only modules if absent.
def _stub_if_missing(name: str, **attrs):
    if name in sys.modules:
        return
    if Path(__file__).with_name(f"{name}.py").is_file():
        return
    try:
        spec = importlib.util.find_spec(name)
    except (ValueError, ModuleNotFoundError):
        spec = None
    if spec is None:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod


_stub_if_missing("candle_feed", fetch_signal_candles=lambda: {}, fetch_closed_candles=lambda *_a, **_k: {}, CandleFeedError=RuntimeError)
_stub_if_missing("trend_stop", update_trend_stop=lambda *_a, **_k: {})
_stub_if_missing("market_discovery", discover=lambda: {"candidates": [], "candidate_count": 0})

from paper_worker import _best_quotes  # noqa: E402


class OrderbookQuotePricingTests(unittest.TestCase):
    def test_derives_asks_from_opposite_side_best_bids(self):
        book = {"orderbook_fp": {
            "yes_dollars": [["0.4500", "10.00"], ["0.4900", "5.00"]],
            "no_dollars": [["0.4200", "10.00"], ["0.5000", "6.00"]],
        }}
        quotes = _best_quotes(book)
        self.assertEqual(quotes["yes_bid"], 49.0)
        self.assertEqual(quotes["no_bid"], 50.0)
        self.assertEqual(quotes["yes_ask"], 50.0)
        self.assertEqual(quotes["no_ask"], 51.0)
        self.assertGreaterEqual(quotes["yes_ask"], quotes["yes_bid"])
        self.assertGreaterEqual(quotes["no_ask"], quotes["no_bid"])

    def test_supports_legacy_integer_cent_orderbook(self):
        quotes = _best_quotes({"orderbook": {"yes": [[49, 1]], "no": [[50, 1]]}})
        self.assertEqual(quotes["yes_ask"], 50.0)
        self.assertEqual(quotes["no_ask"], 51.0)

    def test_missing_opposite_bid_does_not_invent_ask(self):
        quotes = _best_quotes({"orderbook_fp": {"yes_dollars": [["0.4900", "5.00"]], "no_dollars": []}})
        self.assertIsNone(quotes["yes_ask"])
        self.assertIsNone(quotes["no_bid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
