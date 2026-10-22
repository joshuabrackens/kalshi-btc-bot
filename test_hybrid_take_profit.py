"""Offline tests for TradingView-primary hybrid decisions and paper take-profit exits."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import patch
import pandas as pd

import paper_worker as w


class HybridVetoTests(unittest.TestCase):
    def test_accepts_one_minute_tradingview_alert(self):
        tmp = tempfile.TemporaryDirectory()
        old_db = w.DB_PATH
        try:
            w.DB_PATH = Path(tmp.name) / "signal-1m.sqlite3"
            with w._connect() as conn:
                conn.execute(
                    """INSERT INTO gainzalgo_signals
                       (created_at,direction,symbol,timeframe,payload_json)
                       VALUES (?,?,?,?,?)""",
                    (w._utcnow().isoformat(), "UP", "BTCUSDT", "1m",
                     '{"direction":"UP","symbol":"BTCUSDT","timeframe":"1m"}'),
                )
                conn.commit()
                row, direction = w._next_gainzalgo_signal(conn)
                self.assertIsNotNone(row)
                self.assertEqual(direction, "UP")
                self.assertEqual(row["direction"], "UP")
        finally:
            w.DB_PATH = old_db
            tmp.cleanup()

    def test_internal_wait_or_low_score_does_not_veto_by_itself(self):
        payload = {
            "direction": "WAIT",
            "score": 0,
            "strategies": {
                "multi_timeframe_trend": {"timeframes": {"1m": 1, "5m": 0, "15m": -1}},
                "evidence_totals": {"up_points": 3, "down_points": 4},
                "box_theory": {"state": "inside_box", "direction": 0},
                "market_structure": {"direction": 0},
                "volume_confirmation": {"confirmed": False},
            },
        }
        vetoed, reasons = w._major_flaw_veto("UP", payload, 4)
        self.assertFalse(vetoed)
        self.assertEqual(reasons, [])

    def test_opposed_5m_and_15m_with_strong_opposing_evidence_vetoes(self):
        payload = {
            "strategies": {
                "multi_timeframe_trend": {"timeframes": {"1m": 1, "5m": -1, "15m": -1}},
                "evidence_totals": {"up_points": 2, "down_points": 7},
                "box_theory": {"state": "inside_box", "direction": 0},
                "market_structure": {"direction": 0},
                "volume_confirmation": {"confirmed": False},
            }
        }
        vetoed, reasons = w._major_flaw_veto("UP", payload, 4)
        self.assertTrue(vetoed)
        self.assertTrue(any("both the 5-minute and 15-minute" in reason for reason in reasons))

    def test_opposite_confirmed_breakout_and_structure_vetoes(self):
        payload = {
            "strategies": {
                "multi_timeframe_trend": {"timeframes": {"1m": 1, "5m": 0, "15m": 0}},
                "evidence_totals": {"up_points": 5, "down_points": 3},
                "box_theory": {"state": "bearish_breakout", "direction": -1},
                "market_structure": {"direction": -1},
                "volume_confirmation": {"confirmed": True},
            }
        }
        vetoed, reasons = w._major_flaw_veto("UP", payload, 4)
        self.assertTrue(vetoed)
        self.assertTrue(any("volume-confirmed bearish box breakout" in reason for reason in reasons))

    def test_secret_is_removed_from_saved_external_payload(self):
        result = w._safe_external_payload('{"secret":"dont-save-me","direction":"BUY","symbol":"BTCUSDT"}')
        self.assertNotIn("secret", result)
        self.assertEqual(result["direction"], "BUY")

    def test_hybrid_uses_tradingview_even_when_internal_signal_is_ordinary_wait(self):
        tmp = tempfile.TemporaryDirectory()
        old_db = w.DB_PATH
        old_discover, old_get_market, old_get_orderbook = w.discover, w.get_market, w.get_orderbook
        old_fetch, old_evaluate = w.fetch_signal_candles, w.evaluate_signal
        try:
            w.DB_PATH = Path(tmp.name) / "hybrid.sqlite3"
            conn = w._connect()
            created = w._utcnow().isoformat()
            conn.execute(
                """INSERT INTO gainzalgo_signals
                   (created_at,direction,symbol,timeframe,payload_json)
                   VALUES (?,?,?,?,?)""",
                (created, "BUY", "BTCUSDT", "15m",
                 '{"secret":"test-secret","direction":"BUY","symbol":"BTCUSDT","timeframe":"15m"}'),
            )
            conn.commit()
            conn.close()

            close_time = (w._utcnow() + timedelta(minutes=8)).isoformat()
            w.discover = lambda: {"candidates": [{"ticker": "KXBTC15M-TEST"}]}
            w.get_market = lambda ticker: {"market": {
                "ticker": ticker, "status": "active",
                "title": "BTC price up in next 15 mins?",
                "close_time": close_time, "floor_strike": 65000,
                "yes_ask": 50, "no_ask": 50,
            }}
            w.get_orderbook = lambda ticker: {"orderbook_fp": {
                "yes_dollars": [["0.4900", "5"]],
                "no_dollars": [["0.4900", "5"]],
            }}
            frame = pd.DataFrame({
                "timestamp": [f"2026-10-22T00:{i:02d}:00+00:00" for i in range(25)],
                "open": [65000.0] * 25, "high": [65100.0] * 25,
                "low": [64900.0] * 25, "close": [65010.0] * 25,
                "volume": [100.0] * 25,
            })
            w.fetch_signal_candles = lambda: {
                "candles_1m": frame, "candles_5m": frame, "candles_15m": frame,
            }
            weak_internal = {
                "direction": "WAIT", "score": 0, "reasons": ["Internal score below threshold."],
                "warnings": [], "btc_price": 65010.0, "strike_price": 65000.0,
                "distance_from_strike": 10.0, "seconds_remaining": 480,
                "max_entry_cents": None,
                "strategies": {
                    "multi_timeframe_trend": {"timeframes": {"1m": 1, "5m": 0, "15m": 0}},
                    "evidence_totals": {"up_points": 3, "down_points": 2},
                    "box_theory": {"state": "inside_box", "direction": 0},
                    "market_structure": {"direction": 0},
                    "volume_confirmation": {"confirmed": False},
                },
            }
            w.evaluate_signal = lambda *args, **kwargs: SimpleNamespace(
                to_dict=lambda: weak_internal, score=0, btc_price=65010.0,
                strike_price=65000.0, max_entry_cents=None,
                direction="WAIT", reasons=weak_internal["reasons"], warnings=[],
            )
            with patch.dict(os.environ, {
                "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
                "PAPER_SIGNAL_SOURCE": "hybrid", "MAX_TRADE_DOLLARS": "1.00",
                "MAX_DAILY_LOSS_DOLLARS": "10.00", "PAPER_FEE_CENTS_PER_CONTRACT": "2",
                "PAPER_MAX_ENTRY_CENTS": "78", "PAPER_MAX_SPREAD_CENTS": "5",
                "PAPER_TAKE_PROFIT_ENABLED": "true", "PAPER_TAKE_PROFIT_PERCENT": "20",
                "PAPER_TREND_STOP_LOSS_ENABLED": "false", "PAPER_STOP_LOSS_ENABLED": "false",
            }, clear=False):
                result = w.run_once()
            self.assertEqual(result["action"], "paper_opened")
            self.assertEqual(result["direction"], "UP")
            self.assertAlmostEqual(result["take_profit_target_cents"], 64.4)
            conn = w._connect()
            try:
                trade = conn.execute("SELECT direction,take_profit_target_cents,entry_signal_source FROM paper_trades").fetchone()
                alert = conn.execute("SELECT processing_status,payload_json FROM gainzalgo_signals").fetchone()
                logged = conn.execute("SELECT direction,payload_json FROM signal_log ORDER BY id DESC LIMIT 1").fetchone()
            finally:
                conn.close()
            self.assertEqual(trade, ("UP", 64.4, "hybrid"))
            self.assertEqual(alert[0], "processed")
            self.assertNotIn("test-secret", logged[1])
            self.assertIn("TradingView primary", logged[1])
        finally:
            w.DB_PATH = old_db
            w.discover, w.get_market, w.get_orderbook = old_discover, old_get_market, old_get_orderbook
            w.fetch_signal_candles, w.evaluate_signal = old_fetch, old_evaluate
            tmp.cleanup()


class TakeProfitTests(unittest.TestCase):
    def test_take_profit_target_includes_entry_and_exit_fees(self):
        # 20% net ROI on a 50c entry plus 2c entry fee; 2c exit fee included.
        self.assertAlmostEqual(w._take_profit_target_cents(50, 2, 2, 20), 64.4)

    def test_invalid_take_profit_percent_fails_closed(self):
        with self.assertRaises(ValueError):
            w._take_profit_target_cents(50, 2, 2, 0)

    def test_trailing_floor_rises_with_peak_and_never_drops_below_target(self):
        self.assertAlmostEqual(w._take_profit_trailing_floor(64.4, 80, 5), 76.0)
        self.assertAlmostEqual(w._take_profit_trailing_floor(64.4, 65, 5), 64.4)

    def test_first_touch_arms_take_profit_without_closing_trade(self):
        tmp = tempfile.TemporaryDirectory()
        old_db = w.DB_PATH
        old_get_market, old_get_orderbook = w.get_market, w.get_orderbook
        try:
            w.DB_PATH = Path(tmp.name) / "tp.sqlite3"
            conn = w._connect()
            now = w._utcnow().isoformat()
            conn.execute(
                """INSERT INTO paper_trades
                   (created_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,
                    notes,stop_loss_cents,take_profit_target_cents)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, "KXBTC15M-TEST", "UP", 2, 50, 2, "test", None, 64.4),
            )
            conn.commit()
            conn.close()
            w.get_market = lambda ticker: {"market": {"status": "active"}}
            w.get_orderbook = lambda ticker: {"orderbook_fp": {"yes_dollars": [["0.6500", "5"]]}}
            with patch.dict(os.environ, {
                "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
                "PAPER_TAKE_PROFIT_ENABLED": "true", "PAPER_TAKE_PROFIT_PERCENT": "20",
                "PAPER_TAKE_PROFIT_TRAIL_PERCENT": "5",
                "PAPER_TAKE_PROFIT_REVERSAL_EXIT_ENABLED": "false",
                "PAPER_TREND_STOP_LOSS_ENABLED": "false", "PAPER_TREND_STOP_SHADOW_MODE": "false",
                "PAPER_STOP_LOSS_ENABLED": "false", "PAPER_EXIT_FEE_CENTS_PER_CONTRACT": "2",
            }, clear=False):
                conn = w._connect()
                w._manage_open(conn, 10)
                row = conn.execute(
                    "SELECT settled_at,outcome,take_profit_activated_at,take_profit_peak_bid_cents FROM paper_trades"
                ).fetchone()
                conn.close()
            self.assertIsNone(row[0], "first target touch must arm the trail, not immediately exit")
            self.assertIsNone(row[1])
            self.assertIsNotNone(row[2])
            self.assertAlmostEqual(row[3], 65.0)
        finally:
            w.DB_PATH = old_db
            w.get_market, w.get_orderbook = old_get_market, old_get_orderbook
            tmp.cleanup()

    def test_trailing_take_profit_exits_after_pullback_from_peak(self):
        tmp = tempfile.TemporaryDirectory()
        old_db = w.DB_PATH
        old_get_market, old_get_orderbook = w.get_market, w.get_orderbook
        try:
            w.DB_PATH = Path(tmp.name) / "tp_trail.sqlite3"
            conn = w._connect()
            now = w._utcnow().isoformat()
            conn.execute(
                """INSERT INTO paper_trades
                   (created_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,
                    notes,stop_loss_cents,take_profit_target_cents)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, "KXBTC15M-TEST", "UP", 1, 50, 2, "test", None, 64.4),
            )
            conn.commit()
            conn.close()
            bids = {"value": 65.0}
            w.get_market = lambda ticker: {"market": {"status": "active"}}
            w.get_orderbook = lambda ticker: {"orderbook_fp": {"yes_dollars": [[f"{bids['value']/100:.4f}", "5"]]}}
            env = {
                "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
                "PAPER_TAKE_PROFIT_ENABLED": "true", "PAPER_TAKE_PROFIT_PERCENT": "20",
                "PAPER_TAKE_PROFIT_TRAIL_PERCENT": "5",
                "PAPER_TAKE_PROFIT_REVERSAL_EXIT_ENABLED": "false",
                "PAPER_TREND_STOP_LOSS_ENABLED": "false", "PAPER_TREND_STOP_SHADOW_MODE": "false",
                "PAPER_STOP_LOSS_ENABLED": "false", "PAPER_EXIT_FEE_CENTS_PER_CONTRACT": "2",
            }
            with patch.dict(os.environ, env, clear=False):
                conn = w._connect()
                w._manage_open(conn, 10)  # target first reached: arm only
                bids["value"] = 80.0
                w._manage_open(conn, 10)  # new peak moves floor up to 76c
                not_closed = conn.execute("SELECT settled_at,take_profit_peak_bid_cents FROM paper_trades").fetchone()
                self.assertIsNone(not_closed[0])
                self.assertAlmostEqual(not_closed[1], 80.0)
                bids["value"] = 75.9
                w._manage_open(conn, 10)  # pullback through trailing floor triggers simulated exit
                row = conn.execute("SELECT settled_at,outcome,pnl_cents,notes FROM paper_trades").fetchone()
                conn.close()
            self.assertIsNotNone(row[0])
            self.assertEqual(row[1], "TAKE_PROFIT")
            self.assertAlmostEqual(row[2], 75.9 - 50 - 2 - 2)
            self.assertIn("trailing floor", row[3])
        finally:
            w.DB_PATH = old_db
            w.get_market, w.get_orderbook = old_get_market, old_get_orderbook
            tmp.cleanup()

    def test_major_internal_reversal_can_exit_after_target_armed(self):
        tmp = tempfile.TemporaryDirectory()
        old_db = w.DB_PATH
        old_get_market, old_get_orderbook = w.get_market, w.get_orderbook
        old_fetch, old_evaluate, old_stop = w.fetch_signal_candles, w.evaluate_signal, w.update_trend_stop
        try:
            w.DB_PATH = Path(tmp.name) / "tp_reversal.sqlite3"
            conn = w._connect()
            now = w._utcnow().isoformat()
            conn.execute(
                """INSERT INTO paper_trades
                   (created_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,
                    notes,stop_loss_cents,take_profit_target_cents,take_profit_activated_at,take_profit_peak_bid_cents)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (now, "KXBTC15M-TEST", "UP", 1, 50, 2, "test", None, 64.4,
                 (w._utcnow() - timedelta(minutes=2)).isoformat(), 70.0),
            )
            conn.commit()
            conn.close()
            close_time = (w._utcnow() + timedelta(minutes=7)).isoformat()
            w.get_market = lambda ticker: {"market": {
                "ticker": ticker, "status": "active", "title": "BTC price up in next 15 mins?",
                "close_time": close_time, "floor_strike": 65000,
                "yes_ask": 69, "no_ask": 31,
            }}
            w.get_orderbook = lambda ticker: {"orderbook_fp": {
                "yes_dollars": [["0.6800", "5"]], "no_dollars": [["0.2700", "5"]],
            }}
            frame = pd.DataFrame({
                "timestamp": [f"2026-10-22T00:{i:02d}:00+00:00" for i in range(25)],
                "open": [65000.0] * 25, "high": [65100.0] * 25,
                "low": [64900.0] * 25, "close": [65010.0] * 25,
                "volume": [100.0] * 25,
            })
            w.fetch_signal_candles = lambda: {"candles_1m": frame, "candles_5m": frame, "candles_15m": frame}
            opposing_payload = {"strategies": {
                "multi_timeframe_trend": {"timeframes": {"1m": -1, "5m": -1, "15m": -1}},
                "evidence_totals": {"up_points": 2, "down_points": 7},
                "box_theory": {"state": "inside_box", "direction": 0},
                "market_structure": {"direction": 0}, "volume_confirmation": {"confirmed": False},
            }}
            w.evaluate_signal = lambda *args, **kwargs: SimpleNamespace(to_dict=lambda: opposing_payload)
            w.update_trend_stop = lambda *args, **kwargs: {
                "high_water_btc": 65100.0, "low_water_btc": 64900.0,
                "trailing_stop_btc": 64000.0, "reason": "test trail", "triggered": False,
            }
            with patch.dict(os.environ, {
                "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
                "PAPER_TAKE_PROFIT_ENABLED": "true", "PAPER_TAKE_PROFIT_PERCENT": "20",
                "PAPER_TAKE_PROFIT_TRAIL_PERCENT": "5",
                "PAPER_TAKE_PROFIT_REVERSAL_EXIT_ENABLED": "true",
                "PAPER_HYBRID_VETO_MIN_POINTS": "4",
                "PAPER_TREND_STOP_LOSS_ENABLED": "false", "PAPER_TREND_STOP_SHADOW_MODE": "false",
                "PAPER_STOP_LOSS_ENABLED": "false", "PAPER_EXIT_FEE_CENTS_PER_CONTRACT": "2",
            }, clear=False):
                conn = w._connect()
                w._manage_open(conn, 10)
                row = conn.execute("SELECT settled_at,outcome,pnl_cents,notes FROM paper_trades").fetchone()
                conn.close()
            self.assertIsNotNone(row[0])
            self.assertEqual(row[1], "TAKE_PROFIT")
            self.assertAlmostEqual(row[2], 68 - 50 - 2 - 2)
            self.assertIn("major opposing setup", row[3])
        finally:
            w.DB_PATH = old_db
            w.get_market, w.get_orderbook = old_get_market, old_get_orderbook
            w.fetch_signal_candles, w.evaluate_signal, w.update_trend_stop = old_fetch, old_evaluate, old_stop
            tmp.cleanup()

    def test_take_profit_does_not_close_below_target(self):
        tmp = tempfile.TemporaryDirectory()
        old_db = w.DB_PATH
        old_get_market, old_get_orderbook = w.get_market, w.get_orderbook
        try:
            w.DB_PATH = Path(tmp.name) / "tp.sqlite3"
            conn = w._connect()
            now = w._utcnow().isoformat()
            conn.execute(
                """INSERT INTO paper_trades
                   (created_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,
                    notes,stop_loss_cents,take_profit_target_cents)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, "KXBTC15M-TEST", "UP", 1, 50, 2, "test", None, 64.4),
            )
            conn.commit()
            conn.close()
            w.get_market = lambda ticker: {"market": {"status": "active"}}
            w.get_orderbook = lambda ticker: {"orderbook_fp": {"yes_dollars": [["0.6000", "5"]]}}
            with patch.dict(os.environ, {
                "PAPER_TAKE_PROFIT_ENABLED": "true",
                "PAPER_TREND_STOP_LOSS_ENABLED": "false",
                "PAPER_TREND_STOP_SHADOW_MODE": "false",
                "PAPER_STOP_LOSS_ENABLED": "false",
                "PAPER_EXIT_FEE_CENTS_PER_CONTRACT": "2",
            }, clear=False):
                conn = w._connect()
                w._manage_open(conn, 10)
                row = conn.execute("SELECT settled_at,outcome FROM paper_trades").fetchone()
                conn.close()
            self.assertIsNone(row[0])
            self.assertIsNone(row[1])
        finally:
            w.DB_PATH = old_db
            w.get_market, w.get_orderbook = old_get_market, old_get_orderbook
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
