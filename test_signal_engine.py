"""Offline regression tests for signal_engine.py. Run: python -m unittest -v test_signal_engine.py"""
import unittest
import pandas as pd

from signal_engine import MarketContext, evaluate_signal, _box_theory, _validate_candles


def rising_candles(n=30):
    close = [60000 + i * 10 for i in range(n)]
    return pd.DataFrame({
        "open": [x - 2 for x in close],
        "high": [x + 5 for x in close],
        "low": [x - 5 for x in close],
        "close": close,
        "volume": [100.0] * n,
    })


class SignalEngineTests(unittest.TestCase):
    def test_signal_exposes_observed_ask_separately_from_cap(self):
        candles = rising_candles()
        signal = evaluate_signal(
            candles, candles, candles,
            MarketContext(strike_price=60000, seconds_remaining=300,
                          up_ask_cents=40, down_ask_cents=55, spread_cents=1),
            minimum_score=50,
        )
        self.assertEqual(signal.direction, "BUY_UP")
        self.assertEqual(signal.max_entry_cents, 78.0)
        self.assertEqual(signal.entry_ask_cents, 40.0)

    def test_outside_market_window_is_blocked(self):
        candles = rising_candles()
        signal = evaluate_signal(
            candles, candles, candles,
            MarketContext(strike_price=60000, seconds_remaining=901,
                          up_ask_cents=40, down_ask_cents=55, spread_cents=1),
        )
        self.assertEqual(signal.direction, "WAIT")
        self.assertTrue(any("outside the configured market window" in r for r in signal.reasons))

    def test_invalid_ask_is_rejected(self):
        candles = rising_candles()
        with self.assertRaises(ValueError):
            evaluate_signal(candles, candles, candles,
                            MarketContext(strike_price=60000, seconds_remaining=300,
                                          up_ask_cents=100, down_ask_cents=50))

    def test_negative_volume_is_rejected(self):
        candles = rising_candles()
        candles.loc[candles.index[-1], "volume"] = -1
        with self.assertRaises(ValueError):
            _validate_candles(candles, "test")

    def test_box_excludes_current_candle(self):
        candles = rising_candles(20)
        box = _box_theory(candles)
        self.assertEqual(box["prior_candles"], 19)
        self.assertIn(box["state"], {"inside_box", "wick_break_or_rejection", "bullish_breakout", "bearish_breakout"})


if __name__ == "__main__":
    unittest.main()
