"""Offline tests for the manual market-data diagnostics endpoint."""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd


def _install_stub(name, **attrs):
    if name in sys.modules:
        return
    # Prefer real project source files over process-global placeholder modules.
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

class _KalshiPlaceholderError(Exception):
    pass


_install_stub("market_discovery", discover=lambda: {"candidates": [], "candidate_count": 0})
_install_stub("kalshi_api", get_market=lambda *_a, **_k: {}, get_orderbook=lambda *_a, **_k: {},
              KalshiAPIError=_KalshiPlaceholderError)
_install_stub("candle_feed", fetch_signal_candles=lambda: {}, fetch_closed_candles=lambda *_a, **_k: {},
              CandleFeedError=RuntimeError)

import main  # noqa: E402


class MarketDataDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        main._MARKET_DATA_DIAGNOSTIC_CACHE = None
        main._MARKET_DATA_DIAGNOSTIC_CACHE_AT = 0.0
        self.now = datetime.now(timezone.utc)
        self.ticker = "KXBTC15M-26OCT230015-00"
        self.market = {
            "ticker": self.ticker,
            "title": "Will BTC be up at the end of this 15 minute period?",
            "status": "active",
            "floor_strike": 81000,
            "close_time": (self.now + timedelta(minutes=8)).isoformat(),
            "yes_ask": 54,
            "no_ask": 48,
        }
        self.candles = {
            key: pd.DataFrame([{"timestamp": self.now.isoformat(), "close": 81234.5, "volume": 9.0}])
            for key in ("candles_1m", "candles_5m", "candles_15m")
        }
        self.discovery = {"candidate_count": 1, "candidates": [{"ticker": self.ticker, "raw": self.market}]}
        self.orderbook = {"orderbook_fp": {"yes": [[52, 100]], "no": [[45, 100]]}}

    def test_reports_ready_when_candles_market_and_book_are_valid(self):
        with patch("main.fetch_signal_candles", return_value=self.candles), \
             patch("main.discover", return_value=self.discovery), \
             patch("main.get_market", return_value={"market": self.market}), \
             patch("main.get_orderbook", return_value=self.orderbook):
            report = main._probe_market_data()
        self.assertTrue(report["ready_for_evaluation"])
        self.assertEqual(report["sources"]["btc_candles"]["status"], "ok")
        kalshi = report["sources"]["kalshi_market"]
        self.assertEqual(kalshi["status"], "ok")
        self.assertEqual(kalshi["ticker"], self.ticker)
        self.assertEqual(kalshi["quotes"]["yes_bid_cents"], 52.0)
        self.assertEqual(kalshi["quotes"]["yes_ask_cents"], 55.0)
        self.assertEqual(kalshi["quotes"]["no_ask_cents"], 48.0)
        self.assertFalse(report["trade_placed"])

    def test_uses_orderbook_derived_asks_when_summary_asks_are_crossed(self):
        # Summary fields may be stale relative to the current orderbook. Kalshi's
        # bid-only book is the source of truth for asks: ask = 100 - opposite bid.
        self.market["yes_ask"] = 47
        self.market["no_ask"] = 54
        self.orderbook = {"orderbook_fp": {"yes": [[49, 100]], "no": [[50, 100]]}}
        with patch("main.fetch_signal_candles", return_value=self.candles), \
             patch("main.discover", return_value=self.discovery), \
             patch("main.get_market", return_value={"market": self.market}), \
             patch("main.get_orderbook", return_value=self.orderbook):
            report = main._probe_market_data()
        self.assertTrue(report["ready_for_evaluation"])
        quotes = report["sources"]["kalshi_market"]["quotes"]
        self.assertEqual(quotes["yes_bid_cents"], 49.0)
        self.assertEqual(quotes["yes_ask_cents"], 50.0)
        self.assertEqual(quotes["no_bid_cents"], 50.0)
        self.assertEqual(quotes["no_ask_cents"], 51.0)
        self.assertTrue(report["sources"]["kalshi_market"]["market_summary_asks_differ_from_orderbook"])

    def test_candle_age_is_reported_since_close_not_open(self):
        # The timestamp from Binance's candle feed represents candle OPEN time.
        now = datetime.now(timezone.utc)
        candles = {
            "candles_1m": pd.DataFrame([{"timestamp": (now - timedelta(seconds=120)).isoformat(), "close": 81234.5, "volume": 9.0}]),
            "candles_5m": pd.DataFrame([{"timestamp": (now - timedelta(seconds=360)).isoformat(), "close": 81234.5, "volume": 9.0}]),
            "candles_15m": pd.DataFrame([{"timestamp": (now - timedelta(seconds=1200)).isoformat(), "close": 81234.5, "volume": 9.0}]),
        }
        with patch("main.fetch_signal_candles", return_value=candles), \
             patch("main.discover", return_value={"candidate_count": 0, "candidates": []}):
            report = main._probe_market_data()
        intervals = report["sources"]["btc_candles"]["intervals"]
        # Approximate elapsed time after each candle's close: 120-60, 360-300, 1200-900.
        self.assertAlmostEqual(intervals["1m"]["age_seconds"], 60, delta=2)
        self.assertAlmostEqual(intervals["5m"]["age_seconds"], 60, delta=2)
        self.assertAlmostEqual(intervals["15m"]["age_seconds"], 300, delta=2)
        self.assertIn("latest_candle_open_at", intervals["15m"])

    def test_missing_current_market_fails_closed_in_diagnostic(self):
        with patch("main.fetch_signal_candles", return_value=self.candles), \
             patch("main.discover", return_value={"candidate_count": 0, "candidates": []}):
            report = main._probe_market_data()
        self.assertFalse(report["ready_for_evaluation"])
        self.assertEqual(report["sources"]["kalshi_market"]["status"], "not_ready")
        self.assertFalse(report["trade_placed"])

    def test_diagnostic_endpoint_caches_to_prevent_api_bursts(self):
        with patch("main._probe_market_data", return_value={"checked_at_utc": self.now.isoformat(), "ready_for_evaluation": True}) as probe:
            first = main.market_data_diagnostics()
            second = main.market_data_diagnostics()
        self.assertEqual(first["cache"], "fresh")
        self.assertEqual(second["cache"], "cached")
        self.assertEqual(probe.call_count, 1)


if __name__ == "__main__":
    unittest.main()
