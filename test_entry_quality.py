"""Unit tests for the no-chase BTC entry price guard."""
import importlib.util
import sys
import types
import math
import unittest
from datetime import datetime, timezone

# Keep this pure helper test runnable even when optional project-specific modules
# are not mounted into the local test environment. Existing repo modules win.
def _stub_if_missing(name, **attrs):
    if name in sys.modules:
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
_stub_if_missing("market_discovery", discover=lambda: {"candidates": []})

from paper_worker import _entry_price_gate, _quarter_hour_window_start

class EntryPriceQualityTests(unittest.TestCase):
    def test_expensive_contract_waits_for_target_not_entry(self):
        action, note = _entry_price_gate(90, 92, 80)
        self.assertEqual(action, "wait")
        self.assertIn("90.00c", note)
        self.assertIn("80.00c", note)
        self.assertIn("waiting for a better price", note)

    def test_price_at_target_is_allowed(self):
        action, _ = _entry_price_gate(80, 92, 80)
        self.assertEqual(action, "enter")

    def test_price_below_target_is_allowed(self):
        action, _ = _entry_price_gate(78, 92, 80)
        self.assertEqual(action, "enter")

    def test_hard_cap_clamps_target(self):
        action, note = _entry_price_gate(79, 78, 80)
        self.assertEqual(action, "wait")
        self.assertIn("78.00c", note)

    def test_invalid_non_finite_or_out_of_range_prices_fail_closed(self):
        for ask in (0, 100, -1, float('nan'), float('inf')):
            action, _ = _entry_price_gate(ask, 92, 80)
            self.assertEqual(action, "invalid")

    def test_alert_must_not_be_carried_into_next_15_minute_window(self):
        first = datetime(2026, 10, 23, 1, 14, 59, tzinfo=timezone.utc)
        second = datetime(2026, 10, 23, 1, 15, 1, tzinfo=timezone.utc)
        self.assertNotEqual(_quarter_hour_window_start(first), _quarter_hour_window_start(second))

if __name__ == '__main__':
    unittest.main()
