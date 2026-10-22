"""Offline unit tests for paper BTC trend-stop calculations.

Run with: python -m unittest -v test_trend_stop.py
"""
import unittest
import pandas as pd
from trend_stop import update_trend_stop


def candles_from_closes(closes, spread=0.4):
    rows = []
    previous = closes[0]
    for i, close in enumerate(closes):
        rows.append({
            "timestamp": f"2026-10-22T00:{i // 60:02d}:{i % 60:02d}+00:00",
            "open": previous,
            "high": max(previous, close) + spread,
            "low": min(previous, close) - spread,
            "close": close,
            "volume": 100.0,
        })
        previous = close
    return pd.DataFrame(rows)


class TrendStopTests(unittest.TestCase):
    def test_up_stop_is_below_price_and_has_diagnostics(self):
        closes = [100 + i * 0.5 for i in range(60)]
        result = update_trend_stop(candles_from_closes(closes), "UP", high_water_btc=128.0, low_water_btc=100.0)
        self.assertFalse(result["triggered"])
        self.assertLess(result["trailing_stop_btc"], result["btc_close"])
        self.assertGreater(result["atr_14"], 0)
        self.assertIn("UP trend stop", result["reason"])

    def test_up_stop_only_ratchets_up_and_triggers_on_closed_close(self):
        rising = candles_from_closes([100 + i * 0.5 for i in range(60)])
        first = update_trend_stop(rising, "UP", high_water_btc=129.5, low_water_btc=100.0)
        pulled_back = candles_from_closes([100 + i * 0.5 for i in range(58)] + [127.0, 125.0])
        second = update_trend_stop(
            pulled_back, "UP", high_water_btc=first["high_water_btc"],
            low_water_btc=100.0, trailing_stop_btc=first["trailing_stop_btc"],
        )
        self.assertGreaterEqual(second["trailing_stop_btc"], first["trailing_stop_btc"])
        self.assertTrue(second["triggered"])

    def test_down_stop_only_ratchets_down(self):
        falling = candles_from_closes([150 - i * 0.5 for i in range(60)])
        first = update_trend_stop(falling, "DOWN", high_water_btc=150.0, low_water_btc=120.5)
        lower = candles_from_closes([150 - i * 0.5 for i in range(58)] + [121.0, 125.0])
        second = update_trend_stop(
            lower, "DOWN", high_water_btc=150.0,
            low_water_btc=first["low_water_btc"], trailing_stop_btc=first["trailing_stop_btc"],
        )
        self.assertLessEqual(second["trailing_stop_btc"], first["trailing_stop_btc"])
        self.assertTrue(second["triggered"])

    def test_invalid_direction_and_invalid_ohlc_fail_closed(self):
        frame = candles_from_closes([100 + i * 0.25 for i in range(30)])
        with self.assertRaises(ValueError):
            update_trend_stop(frame, "SIDEWAYS")
        frame.loc[0, "high"] = 1
        with self.assertRaises(ValueError):
            update_trend_stop(frame, "UP")


if __name__ == "__main__":
    unittest.main()
