"""SQLite schema and audit-event persistence tests."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import db_store


def test_schema_has_counterfactual_and_decision_audit_fields():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "audit.sqlite3")}):
            conn = db_store.connect_database()
            try:
                db_store.ensure_schema(conn)
                columns = {r[1] for r in conn.execute("PRAGMA table_info(signal_log)").fetchall()}
                required = {"decision_status", "proposed_direction", "entry_ask_cents", "trend_bias",
                            "counterfactual_outcome", "counterfactual_pnl_cents", "counterfactual_settled_at",
                            "counterfactual_last_checked_at", "counterfactual_check_note"}
                assert required.issubset(columns)
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                assert "decision_audit" in tables
            finally:
                conn.close()


def test_record_decision_audit_serializes_safely():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "audit.sqlite3")}):
            conn = db_store.connect_database()
            try:
                db_store.ensure_schema(conn)
                event_id = db_store.record_decision_audit(
                    conn, event_type="signal_evaluation", decision="vetoed", reason="strong opposing trend",
                    alert_id=9, market_ticker="KXBTC15M-TEST", direction="UP", entry_ask_cents=70,
                    trend_bias="DOWN", trend_evidence_score=82, payload={"no_trade": True},
                )
                conn.commit()
                row = conn.execute("SELECT event_type,decision,market_ticker,payload_json FROM decision_audit WHERE id=?", (event_id,)).fetchone()
                assert row[:3] == ("signal_evaluation", "vetoed", "KXBTC15M-TEST")
                assert '"no_trade": true' in row[3]
            finally:
                conn.close()
