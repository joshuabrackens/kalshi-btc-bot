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


def _use_test_db(path: Path) -> None:
    # Unit tests must never attach to a developer/Render PostgreSQL DATABASE_URL.
    os.environ["DATABASE_URL"] = ""
    os.environ["BOT_DB_PATH"] = str(path)


def _restore_db_environment(old_path, old_url):
    if old_path is None:
        os.environ.pop("BOT_DB_PATH", None)
    else:
        os.environ["BOT_DB_PATH"] = old_path
    if old_url is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = old_url


class HybridVetoTests(unittest.TestCase):
    def test_accepts_one_minute_tradingview_alert(self):
        tmp = tempfile.TemporaryDirectory()
        old_db = os.environ.get("BOT_DB_PATH")
        old_url = os.environ.get("DATABASE_URL")
        try:
            _use_test_db(Path(tmp.name) / "signal-1m.sqlite3")
            conn = w._connect()
            try:
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
                conn.close()
        finally:
            _restore_db_environment(old_db, old_url)
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
        self.assertTrue(any("both 5m and 15m trends" in reason.lower() for reason in reasons))

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
        self.assertTrue(any("volume-confirmed breakout" in reason for reason in reasons))

    def test_secret_is_removed_from_saved_external_payload(self):
        result = w._safe_external_payload('{"secret":"dont-save-me","direction":"BUY","symbol":"BTCUSDT"}')
        self.assertNotIn("secret", result)
        self.assertEqual(result["direction"], "BUY")

    def test_hybrid_uses_tradingview_even_when_internal_signal_is_ordinary_wait(self):
        tmp = tempfile.TemporaryDirectory()
        old_db = os.environ.get("BOT_DB_PATH")
        old_url = os.environ.get("DATABASE_URL")
        old_discover, old_get_market, old_get_orderbook = w.discover, w.get_market, w.get_orderbook
        old_fetch, old_evaluate = w.fetch_signal_candles, w.evaluate_signal
        old_bundle, old_bundle_at = w._LATEST_SIGNAL_BUNDLE, w._LATEST_SIGNAL_BUNDLE_AT
        try:
            w._LATEST_SIGNAL_BUNDLE = None
            w._LATEST_SIGNAL_BUNDLE_AT = None
            _use_test_db(Path(tmp.name) / "hybrid.sqlite3")
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
                "PAPER_SHARED_SQLITE_OK": "true",
            }, clear=False):
                result = w.run_once()
            self.assertEqual(result["action"], "paper_opened")
            self.assertEqual(result["direction"], "UP")
            self.assertAlmostEqual(result["take_profit_target_cents"], 65.6)
            conn = w._connect()
            try:
                trade = conn.execute("SELECT direction,take_profit_target_cents,entry_signal_source FROM paper_trades").fetchone()
                alert = conn.execute("SELECT processing_status,payload_json FROM gainzalgo_signals").fetchone()
                logged = conn.execute("SELECT direction,payload_json FROM signal_log ORDER BY id DESC LIMIT 1").fetchone()
            finally:
                conn.close()
            self.assertEqual(trade, ("UP", 65.6, "hybrid"))
            self.assertEqual(alert[0], "processed")
            self.assertNotIn("test-secret", logged[1])
            self.assertIn("TradingView primary", logged[1])
        finally:
            _restore_db_environment(old_db, old_url)
            w.discover, w.get_market, w.get_orderbook = old_discover, old_get_market, old_get_orderbook
            w.fetch_signal_candles, w.evaluate_signal = old_fetch, old_evaluate
            w._LATEST_SIGNAL_BUNDLE, w._LATEST_SIGNAL_BUNDLE_AT = old_bundle, old_bundle_at
            tmp.cleanup()


class TakeProfitTests(unittest.TestCase):
    def test_take_profit_target_includes_entry_and_exit_fees(self):
        # 20% net ROI on a 50c entry plus 2c entry fee; 2c exit fee included.
        self.assertAlmostEqual(w._take_profit_target_cents(50, 2, 2, 20), 64.4)

    def test_take_profit_target_caps_unreachable_return_at_contract_maximum(self):
        # A 95c entry cannot achieve a 10% net ROI after fees on a 100c maximum bid.
        self.assertEqual(w._take_profit_target_cents(95, 2, 2, 10), 100.0)

    def test_invalid_take_profit_percent_fails_closed(self):
        with self.assertRaises(ValueError):
            w._take_profit_target_cents(50, 2, 2, 0)

    def test_trailing_floor_rises_with_peak_and_never_drops_below_target(self):
        self.assertAlmostEqual(w._take_profit_trailing_floor(64.4, 80, 5), 76.0)
        self.assertAlmostEqual(w._take_profit_trailing_floor(64.4, 65, 5), 64.4)

    def test_major_adverse_btc_drop_detected_for_up_position(self):
        frame = pd.DataFrame({
            "open": [100.0] * 24 + [100.0],
            "high": [101.0] * 24 + [100.5],
            "low": [99.0] * 24 + [88.0],
            "close": [100.0] * 24 + [88.0],
        })
        triggered, reason = w._major_adverse_btc_move(frame, "UP", 2.5)
        self.assertTrue(triggered)
        self.assertIn("sharp BTC drop", reason)

    def test_major_adverse_btc_rally_detected_for_down_position(self):
        frame = pd.DataFrame({
            "open": [100.0] * 24 + [100.0],
            "high": [101.0] * 24 + [112.0],
            "low": [99.0] * 24 + [99.5],
            "close": [100.0] * 24 + [112.0],
        })
        triggered, reason = w._major_adverse_btc_move(frame, "DOWN", 2.5)
        self.assertTrue(triggered)
        self.assertIn("sharp BTC rally", reason)

    def test_ordinary_candle_does_not_trigger_major_move_protection(self):
        frame = pd.DataFrame({
            "open": [100.0] * 24 + [100.0],
            "high": [101.0] * 24 + [101.2],
            "low": [99.0] * 24 + [99.6],
            "close": [100.0] * 24 + [100.2],
        })
        triggered, reason = w._major_adverse_btc_move(frame, "UP", 2.5)
        self.assertFalse(triggered)
        self.assertIn("no major adverse", reason)

    def test_first_touch_arms_take_profit_without_closing_trade(self):
        tmp = tempfile.TemporaryDirectory()
        old_db = os.environ.get("BOT_DB_PATH")
        old_url = os.environ.get("DATABASE_URL")
        old_get_market, old_get_orderbook = w.get_market, w.get_orderbook
        try:
            _use_test_db(Path(tmp.name) / "tp.sqlite3")
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
                w._manage_open(conn)
                row = conn.execute(
                    "SELECT settled_at,outcome,take_profit_activated_at,take_profit_peak_bid_cents FROM paper_trades"
                ).fetchone()
                conn.close()
            self.assertIsNone(row[0], "first target touch must arm the trail, not immediately exit")
            self.assertIsNone(row[1])
            self.assertIsNotNone(row[2])
            self.assertAlmostEqual(row[3], 65.0)
        finally:
            _restore_db_environment(old_db, old_url)
            w.get_market, w.get_orderbook = old_get_market, old_get_orderbook
            tmp.cleanup()

    def test_trailing_take_profit_exits_after_pullback_from_peak(self):
        tmp = tempfile.TemporaryDirectory()
        old_db = os.environ.get("BOT_DB_PATH")
        old_url = os.environ.get("DATABASE_URL")
        old_get_market, old_get_orderbook = w.get_market, w.get_orderbook
        try:
            _use_test_db(Path(tmp.name) / "tp_trail.sqlite3")
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
                w._manage_open(conn)  # target first reached: arm only
                bids["value"] = 80.0
                w._manage_open(conn)  # new peak moves floor up to 76c
                not_closed = conn.execute("SELECT settled_at,take_profit_peak_bid_cents FROM paper_trades").fetchone()
                self.assertIsNone(not_closed[0])
                self.assertAlmostEqual(not_closed[1], 80.0)
                bids["value"] = 75.9
                w._manage_open(conn)  # pullback through trailing floor triggers simulated exit
                row = conn.execute("SELECT settled_at,outcome,pnl_cents,notes FROM paper_trades").fetchone()
                conn.close()
            self.assertIsNotNone(row[0])
            self.assertEqual(row[1], "TAKE_PROFIT")
            self.assertAlmostEqual(row[2], 75.9 - 50 - 2 - 2)
            self.assertIn("trailing floor", row[3])
        finally:
            _restore_db_environment(old_db, old_url)
            w.get_market, w.get_orderbook = old_get_market, old_get_orderbook
            tmp.cleanup()

    def test_major_btc_drop_after_target_armed_triggers_profit_protection_exit(self):
        tmp = tempfile.TemporaryDirectory()
        old_db = os.environ.get("BOT_DB_PATH")
        old_url = os.environ.get("DATABASE_URL")
        old_get_market, old_get_orderbook = w.get_market, w.get_orderbook
        old_bundle_fn = w._get_signal_bundle_for_evaluation
        try:
            _use_test_db(Path(tmp.name) / "tp_major_drop.sqlite3")
            conn = w._connect()
            now = w._utcnow().isoformat()
            conn.execute(
                """INSERT INTO paper_trades
                   (created_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,
                    notes,stop_loss_cents,take_profit_target_cents,take_profit_activated_at,take_profit_peak_bid_cents)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (now, "KXBTC15M-TEST", "UP", 1, 50, 2, "test", None, 64.4,
                 (w._utcnow() - timedelta(minutes=2)).isoformat(), 72.0),
            )
            conn.commit()
            conn.close()

            rows = []
            for i in range(24):
                rows.append({"timestamp": f"2026-10-22T00:{i:02d}:00+00:00",
                             "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100.0})
            rows.append({"timestamp": "2026-10-22T00:24:00+00:00",
                         "open": 100.0, "high": 100.5, "low": 88.0, "close": 88.0, "volume": 250.0})
            frame = pd.DataFrame(rows)
            bundle = {"candles_1m": frame, "candles_5m": frame, "candles_15m": frame}
            w._get_signal_bundle_for_evaluation = lambda: bundle
            w.get_market = lambda ticker: {"market": {"status": "active"}}
            w.get_orderbook = lambda ticker: {"orderbook_fp": {"yes_dollars": [["0.7000", "5"]]}}
            with patch.dict(os.environ, {
                "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
                "PAPER_TAKE_PROFIT_ENABLED": "true", "PAPER_TAKE_PROFIT_PERCENT": "20",
                "PAPER_TAKE_PROFIT_TRAIL_PERCENT": "5",
                "PAPER_TAKE_PROFIT_REVERSAL_EXIT_ENABLED": "false",
                "PAPER_TAKE_PROFIT_PROFIT_PROTECTION_ENABLED": "true",
                "PAPER_TAKE_PROFIT_MAJOR_MOVE_ATR_MULTIPLIER": "2.5",
                "PAPER_TREND_STOP_LOSS_ENABLED": "false", "PAPER_TREND_STOP_SHADOW_MODE": "false",
                "PAPER_STOP_LOSS_ENABLED": "false", "PAPER_EXIT_FEE_CENTS_PER_CONTRACT": "2",
            }, clear=False):
                conn = w._connect()
                w._manage_open(conn)
                row = conn.execute("SELECT settled_at,outcome,pnl_cents,notes FROM paper_trades").fetchone()
                conn.close()
            self.assertIsNotNone(row[0])
            self.assertEqual(row[1], "PROFIT_PROTECTION")
            self.assertAlmostEqual(row[2], 70 - 50 - 2 - 2)
            self.assertIn("sharp BTC drop", row[3])
            self.assertIn("at/above fee-adjusted break-even", row[3])
        finally:
            _restore_db_environment(old_db, old_url)
            w.get_market, w.get_orderbook = old_get_market, old_get_orderbook
            w._get_signal_bundle_for_evaluation = old_bundle_fn
            tmp.cleanup()

    def test_major_internal_reversal_can_exit_after_target_armed(self):
        tmp = tempfile.TemporaryDirectory()
        old_db = os.environ.get("BOT_DB_PATH")
        old_url = os.environ.get("DATABASE_URL")
        old_get_market, old_get_orderbook = w.get_market, w.get_orderbook
        old_fetch, old_evaluate, old_stop = w.fetch_signal_candles, w.evaluate_signal, w.update_trend_stop
        try:
            _use_test_db(Path(tmp.name) / "tp_reversal.sqlite3")
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
                "PAPER_TAKE_PROFIT_PROFIT_PROTECTION_ENABLED": "true",
                "PAPER_TAKE_PROFIT_MAJOR_MOVE_ATR_MULTIPLIER": "2.5",
                "PAPER_HYBRID_VETO_MIN_POINTS": "4",
                "PAPER_TREND_STOP_LOSS_ENABLED": "false", "PAPER_TREND_STOP_SHADOW_MODE": "false",
                "PAPER_STOP_LOSS_ENABLED": "false", "PAPER_EXIT_FEE_CENTS_PER_CONTRACT": "2",
            }, clear=False):
                conn = w._connect()
                w._manage_open(conn)
                row = conn.execute("SELECT settled_at,outcome,pnl_cents,notes FROM paper_trades").fetchone()
                conn.close()
            self.assertIsNotNone(row[0])
            self.assertEqual(row[1], "PROFIT_PROTECTION")
            self.assertAlmostEqual(row[2], 68 - 50 - 2 - 2)
            self.assertIn("major opposing setup", row[3])
            self.assertIn("fee-adjusted break-even", row[3])
        finally:
            _restore_db_environment(old_db, old_url)
            w.get_market, w.get_orderbook = old_get_market, old_get_orderbook
            w.fetch_signal_candles, w.evaluate_signal, w.update_trend_stop = old_fetch, old_evaluate, old_stop
            tmp.cleanup()

    def test_take_profit_does_not_close_below_target(self):
        tmp = tempfile.TemporaryDirectory()
        old_db = os.environ.get("BOT_DB_PATH")
        old_url = os.environ.get("DATABASE_URL")
        old_get_market, old_get_orderbook = w.get_market, w.get_orderbook
        try:
            _use_test_db(Path(tmp.name) / "tp.sqlite3")
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
                w._manage_open(conn)
                row = conn.execute("SELECT settled_at,outcome FROM paper_trades").fetchone()
                conn.close()
            self.assertIsNone(row[0])
            self.assertIsNone(row[1])
        finally:
            _restore_db_environment(old_db, old_url)
            w.get_market, w.get_orderbook = old_get_market, old_get_orderbook
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
