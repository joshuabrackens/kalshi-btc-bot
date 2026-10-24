"""PostgreSQL insert-ID adapter regression tests; no external database required."""
from __future__ import annotations

import unittest

from db_store import DatabaseConnection


class FakeCursor:
    def __init__(self, returned_id=123):
        self.returned_id = returned_id
        self.sql = None
        self.params = None
        self.rowcount = 1

    def execute(self, sql, params):
        self.sql = sql
        self.params = tuple(params)

    def fetchone(self):
        return (self.returned_id,) if self.returned_id is not None else None

    def fetchall(self):
        return []


class FakePostgresConnection:
    def __init__(self, returned_id=123):
        self.cursor_instance = FakeCursor(returned_id)

    def cursor(self):
        return self.cursor_instance


class PostgresAutoReturnIdTests(unittest.TestCase):
    def test_research_decision_insert_returns_generated_id(self):
        raw = FakePostgresConnection(returned_id=417)
        conn = DatabaseConnection(raw, "postgresql")
        cur = conn.execute(
            """INSERT INTO market_research_decisions
               (evaluated_at,market_ticker,category,decision,selected_side,model_probability_yes,
                expected_net_edge_cents,model_id,model_version,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("2026-10-24T19:00:00+00:00", "KXSPORTS-TEST", "sports", "blocked",
             None, 0.5, None, "test-model", "v1", "{}"),
        )
        self.assertEqual(cur.lastrowid, 417)
        self.assertIn("RETURNING ID", raw.cursor_instance.sql.upper())
        self.assertEqual(raw.cursor_instance.params[1], "KXSPORTS-TEST")

    def test_all_new_serial_journals_request_returning_id(self):
        tables = (
            "decision_audit", "trade_learning", "market_research_decisions",
            "market_research_paper_trades", "market_research_prediction_outcomes",
            "market_research_prediction_checks", "adaptive_policy_history",
        )
        for table in tables:
            with self.subTest(table=table):
                raw = FakePostgresConnection(returned_id=9)
                conn = DatabaseConnection(raw, "postgresql")
                cur = conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (9,))
                self.assertEqual(cur.lastrowid, 9)
                self.assertIn("RETURNING id", raw.cursor_instance.sql.upper())

    def test_non_serial_setting_table_does_not_request_generated_id(self):
        raw = FakePostgresConnection(returned_id=None)
        conn = DatabaseConnection(raw, "postgresql")
        cur = conn.execute(
            "INSERT INTO runtime_settings (setting_key,setting_value,updated_at) VALUES (?,?,?)",
            ("example", "false", "2026-10-24T19:00:00+00:00"),
        )
        self.assertIsNone(cur.lastrowid)
        self.assertNotIn("RETURNING ID", raw.cursor_instance.sql.upper())


if __name__ == "__main__":
    unittest.main()
