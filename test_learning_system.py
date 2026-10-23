import json
import sqlite3
import unittest

from learning_system import (
    build_learning_features,
    ensure_learning_schema,
    evaluate_learned_adjustment,
    learning_summary,
    record_settled_trade,
)


class LearningSystemTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                settled_at TEXT,
                market_ticker TEXT NOT NULL,
                direction TEXT NOT NULL,
                contracts INTEGER NOT NULL,
                entry_cents REAL NOT NULL,
                fee_cents_per_contract REAL NOT NULL,
                outcome TEXT,
                pnl_cents REAL,
                notes TEXT,
                stop_loss_cents REAL
            )"""
        )
        ensure_learning_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def features(
        timeframe="opposed", volume="not_confirmed", box="breakout_opposed", structure="opposed"
    ):
        return {
            "direction": "UP",
            "signal_source": "hybrid",
            "timeframe_alignment": timeframe,
            "volume_confirmation": volume,
            "box_context": box,
            "market_structure": structure,
        }

    def insert_settled(self, trade_id, features, pnl=-10, outcome="DOWN"):
        cur = self.conn.execute(
            """INSERT INTO paper_trades
               (created_at, settled_at, market_ticker, direction, contracts,
                entry_cents, fee_cents_per_contract, outcome, pnl_cents, learning_features_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("2026-10-01T00:00:00+00:00", "2026-10-01T00:15:00+00:00",
             f"KXBTC15M-TEST-{trade_id}", "UP", 1, 90, 2, outcome, pnl,
             json.dumps(features)),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_records_each_settlement_once_and_updates_profiles(self):
        trade_id = self.insert_settled(1, self.features(), pnl=10, outcome="UP")
        self.assertTrue(record_settled_trade(self.conn, trade_id))
        self.conn.commit()
        self.assertFalse(record_settled_trade(self.conn, trade_id))
        self.conn.commit()
        event_count = self.conn.execute("SELECT COUNT(*) FROM trade_learning_events").fetchone()[0]
        self.assertEqual(event_count, 1)
        row = self.conn.execute(
            "SELECT sample_count, wins, losses FROM trade_learning_profiles WHERE feature_key='timeframe_alignment=opposed'"
        ).fetchone()
        self.assertEqual(row, (1, 1, 0))

    def test_two_independent_negative_roi_patterns_can_veto_after_minimum_samples(self):
        for i in range(8):
            trade_id = self.insert_settled(i, self.features(), pnl=-10)
            record_settled_trade(self.conn, trade_id)
        self.conn.commit()
        result = evaluate_learned_adjustment(
            self.conn, self.features(), min_samples=8, bad_roi_percent=-8
        )
        self.assertTrue(result["vetoed"])
        self.assertTrue(result["adjustment_applied"])
        self.assertGreaterEqual(len(result["underperforming_factors"]), 2)

    def test_one_underperforming_pattern_is_caution_not_veto(self):
        features = self.features(timeframe="opposed", volume="unknown", box="unknown", structure="mixed_or_unknown")
        for i in range(8):
            trade_id = self.insert_settled(i, features, pnl=-10)
            record_settled_trade(self.conn, trade_id)
        self.conn.commit()
        result = evaluate_learned_adjustment(
            self.conn, features, min_samples=8, bad_roi_percent=-8
        )
        self.assertFalse(result["vetoed"])
        self.assertEqual(len(result["cautions"]), 1)

    def test_insufficient_samples_never_veto(self):
        features = self.features()
        for i in range(7):
            trade_id = self.insert_settled(i, features, pnl=-10)
            record_settled_trade(self.conn, trade_id)
        self.conn.commit()
        result = evaluate_learned_adjustment(
            self.conn, features, min_samples=8, bad_roi_percent=-8
        )
        self.assertFalse(result["vetoed"])
        self.assertTrue(all(p["sample_count"] < 8 for p in result["observed_contexts"]))

    def test_build_features_uses_saved_strategy_diagnostics(self):
        payload = {"strategies": {
            "multi_timeframe_trend": {"timeframes": {"1m": 1, "5m": -1, "15m": -1}},
            "volume_confirmation": {"confirmed": True},
            "box_theory": {"state": "bearish_breakout", "direction": -1},
            "market_structure": {"direction": -1},
        }}
        features = build_learning_features("UP", "hybrid", payload)
        self.assertEqual(features["timeframe_alignment"], "opposed")
        self.assertEqual(features["volume_confirmation"], "confirmed")
        self.assertEqual(features["box_context"], "breakout_opposed")
        self.assertEqual(features["market_structure"], "opposed")

    def test_summary_reports_learned_settlements_and_policy(self):
        trade_id = self.insert_settled(9, self.features(), pnl=10, outcome="UP")
        record_settled_trade(self.conn, trade_id)
        self.conn.commit()
        summary = learning_summary(self.conn)
        self.assertEqual(summary["resolved_trades_learned"], 1)
        self.assertIn("never raises size", summary["policy"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
