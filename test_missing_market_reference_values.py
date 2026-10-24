"""Regression tests for unavailable BTC reference values in paper WAIT records."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import main  # noqa: E402
import paper_worker as w  # noqa: E402


class MissingMarketReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env_backup = {key: os.environ.get(key) for key in (
            "BOT_DB_PATH", "DATABASE_URL", "TRADING_MODE", "LIVE_TRADING_ENABLED",
            "PAPER_SIGNAL_SOURCE", "PAPER_SHARED_SQLITE_OK",
        )}
        os.environ.update({
            "BOT_DB_PATH": str(Path(self.tmp.name) / "missing-market.sqlite3"),
            "DATABASE_URL": "",
            "TRADING_MODE": "paper",
            "LIVE_TRADING_ENABLED": "false",
            "PAPER_SIGNAL_SOURCE": "hybrid",
            "PAPER_SHARED_SQLITE_OK": "true",
        })

    def tearDown(self):
        for key, value in self.env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_wait_without_market_data_stores_null_instead_of_fake_prices_and_zero_time(self):
        conn = w._connect()
        try:
            w._log_wait(
                conn,
                "No candidate had a valid BTC strike and close time.",
                external={"id": 42, "direction": "UP", "age_seconds": 20},
                internal_payload={"decision_status": "skipped"},
            )
            row = conn.execute(
                """SELECT btc_price,strike_price,seconds_remaining,payload_json
                   FROM signal_log ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row[0], "missing BTC price must remain unknown")
            self.assertIsNone(row[1], "missing strike must not be copied from BTC price")
            self.assertIsNone(row[2], "unknown time remaining must not be presented as zero")
            payload = json.loads(row[3])
            self.assertIsNone(payload["btc_price"])
            self.assertIsNone(payload["strike_price"])
            self.assertIsNone(payload["seconds_remaining"])
        finally:
            conn.close()

    def test_wait_keeps_independent_valid_price_and_does_not_copy_it_to_missing_strike(self):
        conn = w._connect()
        try:
            w._log_wait(
                conn, "Missing strike context.",
                internal_payload={"btc_price": 65000.25, "seconds_remaining": 120},
            )
            row = conn.execute(
                "SELECT btc_price,strike_price,seconds_remaining FROM signal_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(row[0], 65000.25)
            self.assertIsNone(row[1])
            self.assertEqual(row[2], 120)
        finally:
            conn.close()

    def test_dashboard_hides_legacy_001_placeholder_pair(self):
        row = (
            1, datetime.now(timezone.utc).isoformat(), "KXBTC15M-LEGACY", "WAIT", 0,
            0.01, 0.01, 0, 92.0, "skipped", "UP", None, None, None, 0, None,
            json.dumps({"reasons": ["Historical placeholder row"]}),
        )
        signal = main._dashboard_signal_from_row(row)
        self.assertIsNone(signal["btc_price"])
        self.assertIsNone(signal["strike_price"])
        self.assertIsNone(signal["seconds_remaining"])

    def test_dashboard_preserves_valid_prices(self):
        row = (
            2, datetime.now(timezone.utc).isoformat(), "KXBTC15M-VALID", "BUY_UP", 70,
            65000.25, 65000.0, 120, 92.0, "approved", "UP", 70.0, 2.0, "UP", 70, 1.0,
            json.dumps({"distance_from_strike": 0.25, "reasons": []}),
        )
        signal = main._dashboard_signal_from_row(row)
        self.assertEqual(signal["btc_price"], 65000.25)
        self.assertEqual(signal["strike_price"], 65000.0)
        self.assertEqual(signal["seconds_remaining"], 120)


if __name__ == "__main__":
    unittest.main()
