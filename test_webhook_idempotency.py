"""Webhook fingerprint storage is atomic and schema migration preserves old rows."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import db_store


def test_schema_migration_preserves_old_duplicate_alerts_but_clears_extra_fingerprints():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.sqlite3"
        raw = sqlite3.connect(path)
        raw.execute("""CREATE TABLE gainzalgo_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, direction TEXT NOT NULL,
            symbol TEXT, timeframe TEXT, payload_json TEXT NOT NULL, processed_at TEXT,
            processing_status TEXT, processing_note TEXT, fingerprint TEXT)""")
        raw.execute("INSERT INTO gainzalgo_signals(created_at,direction,payload_json,fingerprint) VALUES('t','UP','{}','same')")
        raw.execute("INSERT INTO gainzalgo_signals(created_at,direction,payload_json,fingerprint) VALUES('t','UP','{}','same')")
        raw.commit(); raw.close()

        with patch.dict(os.environ, {"DATABASE_URL": "", "BOT_DB_PATH": str(path)}, clear=False):
            conn = db_store.connect_database()
            try:
                db_store.ensure_schema(conn)
                rows = conn.execute("SELECT id,fingerprint FROM gainzalgo_signals ORDER BY id").fetchall()
                assert rows == [(1, "same"), (2, None)]
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute("INSERT INTO gainzalgo_signals(created_at,direction,payload_json,fingerprint) VALUES(?,?,?,?)",
                                 ("t", "UP", "{}", "same"))
            finally:
                conn.close()
