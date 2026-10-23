import unittest
from datetime import datetime, timezone

import pandas as pd

from btc_trend_monitor import build_trend_snapshot


def candles(n, minutes, drift):
    close = [81000.0 + drift * i for i in range(n)]
    open_ = [close[0]] + close[:-1]
    high = [max(o, c) + 3 for o, c in zip(open_, close)]
    low = [min(o, c) - 3 for o, c in zip(open_, close)]
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-10-23T00:00:00Z", periods=n, freq=f"{minutes}min"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": [100.0 + (i % 5) for i in range(n)],
    })


class ContinuousTrendMonitorTests(unittest.TestCase):
    def test_snapshot_contains_multitimeframe_chart_context_not_trade_approval(self):
        bundle = {
            "candles_1m": candles(100, 1, 2.5),
            "candles_5m": candles(60, 5, 4.0),
            "candles_15m": candles(40, 15, 8.0),
        }
        snapshot = build_trend_snapshot(bundle, now=datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(snapshot["status"], "ok")
        self.assertTrue(snapshot["monitor_only"])
        self.assertEqual(set(snapshot["timeframes"]), {"1m", "5m", "15m"})
        self.assertIn(snapshot["candidate_bias"], {"UP", "DOWN", "MIXED"})
        self.assertGreater(snapshot["btc_price"], 0)
        self.assertIn("calibrated win probability", snapshot["disclaimer"].lower())
        self.assertNotIn("approved", snapshot)
        self.assertNotIn("trade_placed", snapshot)
        self.assertIn("evidence_points", snapshot)
        self.assertEqual(snapshot["calendar_context"]["day_type_utc"], "weekday")
        self.assertEqual(snapshot["calendar_context"]["utc_time_bucket"], "00:00–05:59 UTC")
        self.assertIn(snapshot["market_regime"]["trend_regime"], {
            "breakout_trend", "aligned_trend", "directional_trend", "conflicted",
            "range_or_consolidation", "mixed_or_unclear", "unknown",
        })
        self.assertIn(snapshot["market_regime"]["volatility_regime"], {"elevated_warning", "not_flagged", "unknown"})

    def test_missing_timeframe_fails_closed(self):
        bundle = {"candles_1m": candles(100, 1, 1.0), "candles_5m": candles(30, 5, 1.0)}
        with self.assertRaises(ValueError):
            build_trend_snapshot(bundle)


if __name__ == "__main__":
    unittest.main()
