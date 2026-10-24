from __future__ import annotations

import csv
import io
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import db_store
import main
from audit_historical_export import audit_export


def _stamp(day: int, hour: int = 12) -> str:
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc).isoformat()


def test_admin_replay_export_contains_curated_trade_files_but_not_credentials(tmp_path):
    db_path = tmp_path / "export.sqlite3"
    env = {"DATABASE_URL": "", "BOT_DB_PATH": str(db_path), "KALSHI_VAULT_ADMIN_SECRET": "admin-secret-long-enough-for-export-test-123456",
           "PAPER_ADMIN_API_SECRET": "", "MARKET_SCANNER_AUTO_START": "false", "MARKET_ANALYSIS_AUTO_START": "false"}
    with patch.dict(os.environ, env, clear=False):
        db_store._INITIALIZED.clear()
        conn = db_store.connect_database()
        try:
            db_store.ensure_schema(conn)
            conn.execute("INSERT INTO paper_trades (created_at,settled_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,outcome,pnl_cents,notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (_stamp(1), _stamp(1, 13), "KXBTC15M-TEST", "UP", 1, 60, 2, "UP", 38, "test"))
            conn.execute("INSERT INTO kalshi_credentials (environment,key_id_hint,encrypted_payload,updated_at) VALUES (?,?,?,?)",
                         ("demo", "hint", "DO_NOT_EXPORT_PRIVATE_KEY", _stamp(1)))
            conn.commit()
        finally:
            conn.close()
        with TestClient(main.app) as client:
            denied = client.get("/api/diagnostics/replay-export")
            assert denied.status_code == 401
            login = client.post("/api/admin/login", json={"secret": env["KALSHI_VAULT_ADMIN_SECRET"]})
            assert login.status_code == 200
            response = client.get("/api/diagnostics/replay-export")
        assert response.status_code == 200
        assert "application/zip" in response.headers["content-type"]
        archive_bytes = response.content
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as z:
            assert "paper_trades.csv" in z.namelist()
            assert "market_research_prediction_outcomes.csv" in z.namelist()
            assert "kalshi_credentials.csv" not in z.namelist()
            assert b"DO_NOT_EXPORT_PRIVATE_KEY" not in archive_bytes
            content = z.read("paper_trades.csv").decode()
            assert "KXBTC15M-TEST" in content


def test_historical_export_audit_calculates_actual_win_rate_and_excludes_exit_labels_from_directional_accuracy(tmp_path):
    path = tmp_path / "sample_export.zip"
    tables = {
        "paper_trades.csv": [
            {"id": "1", "created_at": _stamp(1), "settled_at": _stamp(1, 13), "market_ticker": "BTC1", "direction": "UP", "contracts": "1", "entry_cents": "60", "fee_cents_per_contract": "2", "outcome": "UP", "pnl_cents": "38", "entry_signal_source": "Hybrid", "entry_signal_score": "75", "timeframe": "15m"},
            {"id": "2", "created_at": _stamp(1), "settled_at": _stamp(2, 13), "market_ticker": "BTC2", "direction": "DOWN", "contracts": "1", "entry_cents": "70", "fee_cents_per_contract": "2", "outcome": "UP", "pnl_cents": "-72", "entry_signal_source": "Hybrid", "entry_signal_score": "45", "timeframe": "15m"},
            {"id": "3", "created_at": _stamp(2), "settled_at": _stamp(2, 14), "market_ticker": "BTC3", "direction": "UP", "contracts": "1", "entry_cents": "80", "fee_cents_per_contract": "2", "outcome": "TREND STOP LOSS", "pnl_cents": "-31", "entry_signal_source": "Hybrid", "entry_signal_score": "55", "timeframe": "15m"},
        ],
        "trade_learning.csv": [], "signal_decisions.csv": [], "market_research_decisions.csv": [],
        "market_research_prediction_outcomes.csv": [], "market_research_paper_trades.csv": [],
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, rows in tables.items():
            if rows:
                buf = io.StringIO(newline="")
                writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
                writer.writeheader(); writer.writerows(rows)
                z.writestr(name, buf.getvalue())
            else:
                z.writestr(name, "empty\n")
    result = audit_export(path)
    metrics = result["paper_trade_metrics"]
    assert metrics["settled_trades"] == 3
    assert metrics["wins"] == 1 and metrics["losses"] == 2
    assert metrics["win_rate_percent"] == 33.33
    assert metrics["net_pnl_cents"] == -65.0
    assert result["paper_directional_correctness"]["comparable_settled_rows"] == 2
    assert result["paper_directional_correctness"]["correct"] == 1
