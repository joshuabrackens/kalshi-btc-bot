"""Offline tests for the manual market-data diagnostics endpoint."""
from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd


def _install_stub(name: str, **attrs):
    if name in sys.modules:
        return
    try:
        spec = importlib.util.find_spec(name)
    except (ValueError, ModuleNotFoundError):
        spec = None
    if spec is None:
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module


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
        self.assertFalse(report["trade_placed"])

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
