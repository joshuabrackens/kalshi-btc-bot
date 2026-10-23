from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone

import pandas as pd

from db_store import connect_database, ensure_schema, backend_name
from signal_engine import MarketContext, evaluate_signal
from signal_fusion import evaluate_fusion


class StorageTests(unittest.TestCase):
    def test_sqlite_schema_includes_shared_queue_and_status(self):
        old_path = os.environ.get("BOT_DB_PATH")
        old_url = os.environ.pop("DATABASE_URL", None)
        try:
            with tempfile.TemporaryDirectory() as temp:
                os.environ["BOT_DB_PATH"] = os.path.join(temp, "journal.sqlite3")
                conn = connect_database()
                ensure_schema(conn)
                self.assertEqual(backend_name(), "sqlite")
                columns = {r[1] for r in conn.execute("PRAGMA table_info(gainzalgo_signals)").fetchall()}
                self.assertTrue({"processed_at", "processing_status", "processing_note", "fingerprint"}.issubset(columns))
                cur = conn.execute(
                    "INSERT INTO gainzalgo_signals (created_at,direction,symbol,timeframe,payload_json) VALUES (?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), "UP", "BTCUSDT", "1m", "{}"),
                )
                conn.commit()
                self.assertIsNotNone(cur.lastrowid)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM gainzalgo_signals").fetchone()[0], 1)
                conn.close()
        finally:
            if old_path is None:
                os.environ.pop("BOT_DB_PATH", None)
            else:
                os.environ["BOT_DB_PATH"] = old_path
            if old_url is not None:
                os.environ["DATABASE_URL"] = old_url


class SignalScoreTests(unittest.TestCase):
    @staticmethod
    def candles(n=35):
        rows = []
        for i in range(n):
            close = 100.0 + i * 0.25
            rows.append({"timestamp": f"2026-01-01T00:{i:02d}:00+00:00", "open": close - 0.10,
                         "high": close + 0.20, "low": close - 0.25, "close": close, "volume": 100.0})
        return pd.DataFrame(rows)

    def test_wait_for_threshold_preserves_actual_evidence_score(self):
        c1, c5, c15 = self.candles(), self.candles(), self.candles()
        result = evaluate_signal(
            c1, c5, c15,
            MarketContext(strike_price=102.0, seconds_remaining=600, up_ask_cents=50,
                          down_ask_cents=50, spread_cents=2, estimated_fee_cents=1),
            minimum_score=99,
        )
        self.assertEqual(result.direction, "WAIT")
        evidence = result.strategies["evidence_totals"]
        expected_evidence_score = round(max(evidence["up_points"], evidence["down_points"]) / 11 * 100)
        self.assertEqual(result.score, expected_evidence_score)
        self.assertTrue(any("below the configured 99/100 threshold" in reason for reason in result.reasons))
        self.assertEqual(result.entry_ask_cents, 50)


class FusionTests(unittest.TestCase):
    def test_hybrid_keeps_tradingview_primary_when_no_major_veto(self):
        now = datetime.now(timezone.utc)
        result = evaluate_fusion(
            direction="UP", alert_created_at=now.isoformat(), symbol="BTCUSDT", timeframe="1m",
            internal_payload={"direction": "WAIT", "score": 18, "strategies": {
                "multi_timeframe_trend": {"timeframes": {"5m": 0, "15m": 0}},
                "evidence_totals": {"up_points": 1, "down_points": 1},
            }},
            market_ticker="KXBTC15M-26OCT221700-00", seconds_remaining=600,
            up_ask_cents=50, down_ask_cents=51, spread_cents=2, fee_cents_per_contract=1,
            now=now,
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.direction, "BUY_UP")

    def test_hybrid_vetoes_clear_major_opposing_conflict(self):
        now = datetime.now(timezone.utc)
        result = evaluate_fusion(
            direction="UP", alert_created_at=now.isoformat(), symbol="BTCUSDT", timeframe="1m",
            internal_payload={"direction": "BUY_DOWN", "score": 30, "strategies": {
                "multi_timeframe_trend": {"timeframes": {"5m": -1, "15m": -1}},
                "evidence_totals": {"up_points": 1, "down_points": 7},
            }},
            market_ticker="KXBTC15M-26OCT221700-00", seconds_remaining=600,
            up_ask_cents=50, down_ask_cents=51, spread_cents=2, fee_cents_per_contract=1,
            now=now,
        )
        self.assertFalse(result.approved)
        self.assertTrue(result.vetoed_by_internal)
        self.assertEqual(result.direction, "WAIT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
