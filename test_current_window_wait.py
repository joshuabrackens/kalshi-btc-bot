"""Offline tests for automatic WAIT fallback when the current 15m window has no fresh direction."""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
import unittest
from datetime import datetime, timezone

# Stub project-specific connectors; this tests dashboard status derivation only.
def _install_stub(name, **attrs):
    if name in sys.modules:
        return
    # The project source in this directory must win over a process-wide test stub.
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

class _PlaceholderError(Exception):
    pass

_install_stub("market_discovery", discover=lambda: {"candidates": []})
_install_stub("kalshi_api", get_market=lambda *_a, **_k: {}, get_orderbook=lambda *_a, **_k: {}, KalshiAPIError=_PlaceholderError)
_install_stub("candle_feed", fetch_signal_candles=lambda: {}, fetch_closed_candles=lambda *_a, **_k: {}, CandleFeedError=_PlaceholderError)
_install_stub("trend_stop", update_trend_stop=lambda *_a, **_k: {})

from main import _current_window_decision


class CurrentWindowWaitTests(unittest.TestCase):
    def setUp(self):
        self.old_source = os.environ.get("PAPER_SIGNAL_SOURCE")
        self.old_ttl = os.environ.get("TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS")
        os.environ["PAPER_SIGNAL_SOURCE"] = "hybrid"
        os.environ["TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS"] = "300"
        self.now = datetime(2026, 10, 23, 0, 7, 0, tzinfo=timezone.utc)

    def tearDown(self):
        if self.old_source is None:
            os.environ.pop("PAPER_SIGNAL_SOURCE", None)
        else:
            os.environ["PAPER_SIGNAL_SOURCE"] = self.old_source
        if self.old_ttl is None:
            os.environ.pop("TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS", None)
        else:
            os.environ["TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS"] = self.old_ttl

    def test_no_alert_creates_only_automatic_wait_status(self):
        d = _current_window_decision([], now=self.now, signal_source="hybrid")
        self.assertEqual(d["status"], "WAIT THIS WINDOW")
        self.assertEqual(d["source"], "automatic_fallback")
        self.assertTrue(d["automatic_fallback"])
        self.assertIn("not a fabricated TradingView alert", d["reason"])
        self.assertEqual(d["window_start"], "2026-10-23T00:00:00+00:00")
        self.assertEqual(d["window_end"], "2026-10-23T00:15:00+00:00")

    def test_old_direction_from_previous_window_does_not_remain_current(self):
        alerts = [{"direction": "UP", "received_at": "2026-10-22T23:45:00+00:00", "processing_status": "ignored"}]
        d = _current_window_decision(alerts, now=self.now, signal_source="hybrid")
        self.assertEqual(d["status"], "WAIT THIS WINDOW")
        self.assertTrue(d["automatic_fallback"])
        self.assertNotIn("latest saved alert was UP", d["reason"])
        self.assertIsNone(d["alert_received_at"])

    def test_fresh_up_signal_in_window_disables_fallback(self):
        alerts = [{"direction": "UP", "received_at": "2026-10-23T00:06:00+00:00", "processing_status": "queued"}]
        d = _current_window_decision(alerts, now=self.now, signal_source="hybrid")
        self.assertEqual(d["status"], "UP SIGNAL RECEIVED")
        self.assertEqual(d["source"], "fresh_directional_alert")
        self.assertFalse(d["automatic_fallback"])

    def test_explicit_wait_is_distinct_from_automatic_fallback(self):
        alerts = [{"direction": "WAIT", "received_at": "2026-10-23T00:06:00+00:00", "processing_status": "wait"}]
        d = _current_window_decision(alerts, now=self.now, signal_source="hybrid")
        self.assertEqual(d["status"], "WAIT THIS WINDOW")
        self.assertEqual(d["source"], "indicator_wait")
        self.assertFalse(d["automatic_fallback"])


if __name__ == "__main__":
    unittest.main()
