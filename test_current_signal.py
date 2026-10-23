"""Tests for a single current-signal display and three-minute indicator response allowance."""
from __future__ import annotations
import importlib.util, os, sys, types, unittest
from datetime import datetime, timezone

def _install_stub(name, **attrs):
    if importlib.util.find_spec(name) is None and name not in sys.modules:
        mod=types.ModuleType(name)
        for key,value in attrs.items(): setattr(mod,key,value)
        sys.modules[name]=mod
class _PlaceholderError(Exception): pass
_install_stub("market_discovery", discover=lambda: {"candidates": []})
_install_stub("kalshi_api", get_market=lambda *_a, **_k: {}, get_orderbook=lambda *_a, **_k: {}, KalshiAPIError=_PlaceholderError)
_install_stub("candle_feed", fetch_signal_candles=lambda: {}, fetch_closed_candles=lambda *_a, **_k: {}, CandleFeedError=_PlaceholderError)
_install_stub("trend_stop", update_trend_stop=lambda *_a, **_k: {})
from main import _current_window_decision

class CurrentSignalTests(unittest.TestCase):
    def setUp(self):
        self.old={k:os.environ.get(k) for k in ("PAPER_SIGNAL_SOURCE","TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS","INDICATOR_RESPONSE_GRACE_SECONDS")}
        os.environ["PAPER_SIGNAL_SOURCE"]="hybrid"
        os.environ["TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS"]="300"
        os.environ["INDICATOR_RESPONSE_GRACE_SECONDS"]="180"
    def tearDown(self):
        for key,value in self.old.items():
            if value is None: os.environ.pop(key,None)
            else: os.environ[key]=value
    def test_first_three_minutes_waits_for_indicator_without_carrying_old_direction(self):
        now=datetime(2026,10,23,0,1,30,tzinfo=timezone.utc)
        old=[{"direction":"UP","received_at":"2026-10-22T23:45:00+00:00","processing_status":"ignored"}]
        d=_current_window_decision(old,now=now,signal_source="hybrid")
        self.assertEqual(d["status"],"WAIT THIS WINDOW")
        self.assertEqual(d["phase"],"awaiting_indicator")
        self.assertEqual(d["indicator_grace_remaining_seconds"],90)
        self.assertIsNone(d["alert_received_at"])
        self.assertNotIn("latest saved alert was UP",d["reason"])
    def test_after_three_minutes_waits_without_showing_old_signal(self):
        now=datetime(2026,10,23,0,6,30,tzinfo=timezone.utc)
        old=[{"direction":"DOWN","received_at":"2026-10-23T00:00:10+00:00","processing_status":"ignored"}]
        d=_current_window_decision(old,now=now,signal_source="hybrid")
        self.assertEqual(d["status"],"WAIT THIS WINDOW")
        self.assertEqual(d["phase"],"no_fresh_signal")
        self.assertIsNone(d["alert_received_at"])
        self.assertNotIn("latest saved alert",d["reason"])
    def test_fresh_up_signal_is_current(self):
        now=datetime(2026,10,23,0,7,0,tzinfo=timezone.utc)
        alerts=[{"direction":"UP","received_at":"2026-10-23T00:06:00+00:00","processing_status":"queued","timeframe":"1m"}]
        d=_current_window_decision(alerts,now=now,signal_source="hybrid")
        self.assertEqual(d["status"],"UP SIGNAL RECEIVED")
        self.assertEqual(d["phase"],"signal_received")
        self.assertEqual(d["timeframe"],"1m")
    def test_explicit_wait_is_current_window_only(self):
        now=datetime(2026,10,23,0,7,0,tzinfo=timezone.utc)
        alerts=[{"direction":"WAIT","received_at":"2026-10-23T00:06:00+00:00","processing_status":"wait"}]
        d=_current_window_decision(alerts,now=now,signal_source="hybrid")
        self.assertEqual(d["status"],"WAIT THIS WINDOW")
        self.assertEqual(d["phase"],"indicator_wait")
    def test_previous_window_up_does_not_carry_into_current_window(self):
        now=datetime(2026,10,23,0,16,0,tzinfo=timezone.utc)
        alerts=[{"direction":"UP","received_at":"2026-10-23T00:14:30+00:00","processing_status":"ignored"}]
        d=_current_window_decision(alerts,now=now,signal_source="hybrid")
        self.assertEqual(d["phase"],"awaiting_indicator")
        self.assertEqual(d["status"],"WAIT THIS WINDOW")

if __name__=="__main__": unittest.main()
