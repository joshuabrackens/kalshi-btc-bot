import os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paper_worker as w


class WorkerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env_backup = {
            key: os.environ.get(key)
            for key in ("BOT_DB_PATH", "DATABASE_URL", "TRADING_MODE", "LIVE_TRADING_ENABLED",
                        "MAX_TRADE_DOLLARS", "MAX_DAILY_LOSS_DOLLARS", "PAPER_STOP_LOSS_ENABLED",
                        "PAPER_TREND_STOP_LOSS_ENABLED", "PAPER_TREND_STOP_SHADOW_MODE", "PAPER_SIGNAL_SOURCE",
                        "PAPER_SHARED_SQLITE_OK")
        }
        # Use a test-owned SQLite journal. A leftover default journal must never
        # influence risk limits, trigger provider calls, or leak positions across tests.
        os.environ.update({
            "BOT_DB_PATH": str(Path(self.tmp.name) / "test.sqlite3"),
            "DATABASE_URL": "",
            "TRADING_MODE": "paper",
            "LIVE_TRADING_ENABLED": "false",
            "PAPER_SIGNAL_SOURCE": "internal",
            "PAPER_SHARED_SQLITE_OK": "true",
            "MAX_TRADE_DOLLARS": "1.00",
            "MAX_DAILY_LOSS_DOLLARS": "10.00",
            "PAPER_STOP_LOSS_ENABLED": "false",
            "PAPER_TREND_STOP_LOSS_ENABLED": "false",
            "PAPER_TREND_STOP_SHADOW_MODE": "false",
        })
        self._old_bundle = w._LATEST_SIGNAL_BUNDLE
        self._old_bundle_at = w._LATEST_SIGNAL_BUNDLE_AT
        w._LATEST_SIGNAL_BUNDLE = None
        w._LATEST_SIGNAL_BUNDLE_AT = None

    def tearDown(self):
        w._LATEST_SIGNAL_BUNDLE = self._old_bundle
        w._LATEST_SIGNAL_BUNDLE_AT = self._old_bundle_at
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_queued_external_alert_is_explicitly_marked_during_risk_cooldown(self):
        conn = w._connect()
        try:
            now = w._utcnow().isoformat()
            cur = conn.execute(
                """INSERT INTO gainzalgo_signals
                   (created_at,direction,symbol,timeframe,payload_json)
                   VALUES (?,?,?,?,?)""",
                (now, "UP", "BTCUSDT", "1m", "{}"),
            )
            alert_id = cur.lastrowid
            conn.commit()

            marked = w._mark_queued_alerts_during_cooldown(
                conn, "hybrid", "paper entry circuit breaker: 4 consecutive net-loss trades",
            )

            self.assertEqual(marked, 1)
            row = conn.execute(
                "SELECT processed_at,processing_status,processing_note FROM gainzalgo_signals WHERE id=?",
                (alert_id,),
            ).fetchone()
            self.assertIsNotNone(row[0])
            self.assertEqual(row[1], "skipped")
            self.assertIn("Blocked by paper risk cooldown", row[2])
            self.assertIn("4 consecutive net-loss trades", row[2])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0], 0)
        finally:
            conn.close()

    def test_live_mode_fails_closed(self):
        os.environ["LIVE_TRADING_ENABLED"] = "true"
        self.assertEqual(w.run_once()["action"], "blocked")
        os.environ["LIVE_TRADING_ENABLED"] = "false"

    def test_market_without_explicit_strike_rejected_with_diagnostic(self):
        m = {"market": {
            "ticker": "KXBTC15M-TEST", "status": "active",
            "title": "BTC price up in next 15 mins?",
            "close_time": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "yes_ask": 50, "no_ask": 50,
        }}
        rejection_counts = {}
        self.assertIsNone(w._extract_market(m, rejection_counts))
        self.assertEqual(rejection_counts, {"missing_or_invalid_strike": 1})

    def test_initialized_market_is_rejected_even_when_strike_and_close_time_are_valid(self):
        m = {"market": {
            "ticker": "KXBTC15M-TEST", "status": "initialized",
            "title": "Bitcoin above $80,500",
            "close_time": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "floor_strike": 80500,
        }}
        rejection_counts = {}
        self.assertIsNone(w._extract_market(m, rejection_counts))
        self.assertEqual(rejection_counts, {"market_not_active": 1})

    def test_market_explicit_strike_accepted(self):
        m = {"market": {
            "ticker": "KXBTC15M-TEST", "status": "active",
            "title": "BTC price up in next 15 mins?",
            "close_time": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "floor_strike": 65000, "yes_ask": 50, "no_ask": 50,
        }}
        parsed = w._extract_market(m)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["strike"], 65000)

    def test_btc_title_accepts_target_price_dollars_without_literal_up(self):
        m = {"market": {
            "ticker": "KXBTC15M-TEST", "status": "active",
            "title": "Bitcoin above $80,500 at 4:00 PM ET",
            "close_time": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "target_price_dollars": 80500.0,
        }}
        parsed = w._extract_market(m)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["strike"], 80500.0)

    def test_top_bid_levels(self):
        self.assertEqual(
            w._top_bid({"orderbook": {"yes": [[40, 3], [42, 1]]}}, "yes"),
            42,
        )
        real_format = {"orderbook_fp": {
            "yes_dollars": [["0.4200", "3"], ["0.4500", "1"]],
            "no_dollars": [["0.2900", "2"], ["0.3100", "4"]],
        }}
        self.assertAlmostEqual(w._top_bid(real_format, "yes"), 45.0)
        self.assertAlmostEqual(w._top_bid(real_format, "no"), 31.0)
        empty_book = {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
        self.assertIsNone(w._top_bid(empty_book, "yes"))
        self.assertIsNone(w._top_bid(empty_book, "no"))

    def test_discovery_without_strike_does_not_open_trade(self):
        old_discover, old_get_market = w.discover, w.get_market
        w.discover = lambda: {"candidates": [{"ticker": "KXBTC15M-TEST"}]}
        w.get_market = lambda ticker: {"market": {
            "ticker": ticker, "status": "active",
            "title": "BTC price up in next 15 mins?",
            "close_time": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "yes_ask": 50, "no_ask": 50,
        }}
        try:
            with patch.object(w, "_refresh_btc_trend_monitor", return_value={
                "status": "ok", "monitor_only": True, "updated_at": w._utcnow().isoformat(),
                "candidate_bias": "MIXED", "evidence_score": 0, "timeframes": {},
            }):
                result = w.run_once()
            self.assertEqual(result["action"], "wait")
            c = w._connect()
            try:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0], 0)
            finally:
                c.close()
        finally:
            w.discover, w.get_market = old_discover, old_get_market

    def test_limits_halts_when_open_risk_reaches_daily_limit(self):
        conn = w._connect()
        try:
            now = w._utcnow().isoformat()
            conn.execute(
                """INSERT INTO paper_trades
                (created_at, market_ticker, direction, contracts,
                 entry_cents, fee_cents_per_contract, notes, stop_loss_cents)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, "KXBTC15M-TEST", "UP", 10, 98, 2, "test", 60),
            )
            conn.commit()
            realized, open_cost, count, halt = w._limits(conn, 1.00, 10.00)
            self.assertTrue(halt)
            self.assertGreaterEqual(open_cost, 1000)
        finally:
            conn.close()

    def test_limits_halts_after_daily_realized_loss(self):
        conn = w._connect()
        try:
            now = w._utcnow().isoformat()
            conn.execute(
                """INSERT INTO paper_trades
                (created_at, market_ticker, direction, contracts,
                 entry_cents, fee_cents_per_contract, notes, stop_loss_cents,
                 settled_at, outcome, pnl_cents)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, "KXBTC15M-TEST", "UP", 1, 50, 2, "test", 30,
                 now, "DOWN", -1000),
            )
            conn.commit()
            realized, open_cost, count, halt = w._limits(conn, 1.00, 10.00)
            self.assertLessEqual(realized, -1000)
            self.assertTrue(halt)
        finally:
            conn.close()

    def test_stop_loss_closes_paper_trade_at_trigger(self):
        conn = w._connect()
        original_get_market = w.get_market
        original_get_orderbook = w.get_orderbook
        try:
            now = w._utcnow().isoformat()
            conn.execute(
                """INSERT INTO paper_trades
                (created_at, market_ticker, direction, contracts,
                 entry_cents, fee_cents_per_contract, notes, stop_loss_cents)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, "KXBTC15M-TEST", "UP", 1, 60, 2, "test", 40),
            )
            conn.commit()
            w.get_market = lambda ticker: {"market": {"status": "active"}}
            w.get_orderbook = lambda ticker: {
                "orderbook_fp": {"yes_dollars": [["0.3900", "5"]]}
            }
            with patch.dict(os.environ, {
                "PAPER_STOP_LOSS_ENABLED": "true",
                "PAPER_TREND_STOP_LOSS_ENABLED": "false",
                "PAPER_TREND_STOP_SHADOW_MODE": "false",
            }):
                w._manage_open(conn)
            trade = conn.execute(
                """SELECT settled_at, outcome, pnl_cents
                   FROM paper_trades
                   WHERE created_at=? AND market_ticker=?""",
                (now, "KXBTC15M-TEST"),
            ).fetchone()
            self.assertIsNotNone(trade[0])
            self.assertEqual(trade[1], "STOP_LOSS")
            self.assertAlmostEqual(trade[2], 39 - 60 - 2 - 2)
        finally:
            w.get_market = original_get_market
            w.get_orderbook = original_get_orderbook
            conn.close()


    def test_trend_stop_shadow_records_hypothetical_exit_without_closing_trade(self):
        conn = w._connect()
        original_get_market = w.get_market
        original_get_orderbook = w.get_orderbook
        try:
            now = w._utcnow().isoformat()
            conn.execute(
                """INSERT INTO paper_trades
                (created_at, market_ticker, direction, contracts, entry_cents,
                 fee_cents_per_contract, notes, stop_loss_cents, entry_btc_price,
                 high_water_btc, low_water_btc, last_stop_candle_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, "KXBTC15M-TEST", "UP", 1, 60, 2, "shadow test", None,
                 100000.0, 100000.0, 100000.0, "old-candle"),
            )
            conn.commit()
            w.get_market = lambda ticker: {"market": {"status": "active"}}
            w.get_orderbook = lambda ticker: {
                "orderbook_fp": {"yes_dollars": [["0.3900", "5"]]}
            }
            frame = pd.DataFrame({
                "timestamp": ["2026-10-22T05:20:00+00:00"],
                "open": [100000.0], "high": [100100.0],
                "low": [99900.0], "close": [99950.0], "volume": [10.0],
            })
            fake_update = {
                "high_water_btc": 100100.0,
                "low_water_btc": 99900.0,
                "trailing_stop_btc": 99975.0,
                "triggered": True,
                "reason": "unit-test chart stop breach",
            }
            with patch.dict(os.environ, {
                "PAPER_STOP_LOSS_ENABLED": "false",
                "PAPER_TREND_STOP_LOSS_ENABLED": "false",
                "PAPER_TREND_STOP_SHADOW_MODE": "true",
                "PAPER_TREND_STOP_CHECK_SECONDS": "30",
                "PAPER_EXIT_FEE_CENTS_PER_CONTRACT": "2.0",
            }), patch.object(w, "fetch_closed_candles", return_value=frame), \
                 patch.object(w, "update_trend_stop", return_value=fake_update) as update_mock:
                w._manage_open(conn)

            update_mock.assert_called_once()
            row = conn.execute(
                """SELECT settled_at, outcome, pnl_cents,
                          trend_stop_shadow_triggered_at,
                          trend_stop_shadow_trigger_bid_cents,
                          trend_stop_shadow_pnl_cents,
                          trend_stop_shadow_reason,
                          trend_stop_shadow_candle_timestamp
                   FROM paper_trades
                   WHERE created_at=? AND market_ticker=?""",
            (now, "KXBTC15M-TEST"),
        ).fetchone()
            self.assertIsNone(row[0], "shadow mode must not settle/close the trade")
            self.assertIsNone(row[1], "shadow mode must not set an outcome")
            self.assertIsNone(row[2], "hypothetical P&L must not become realized P&L")
            self.assertIsNotNone(row[3])
            self.assertAlmostEqual(row[4], 39.0)
            self.assertAlmostEqual(row[5], 39 - 60 - 2 - 2)
            self.assertIn("unit-test chart stop breach", row[6])
            self.assertEqual(row[7], frame["timestamp"].iloc[-1])
        finally:
            w.get_market = original_get_market
            w.get_orderbook = original_get_orderbook
            conn.close()



if __name__ == "__main__":
    unittest.main()
