"""One-time importer for an existing local SQLite paper-bot journal into PostgreSQL.

Run exactly once per SQLite database file after setting DATABASE_URL to the target
PostgreSQL connection string. Example:
  python migrate_sqlite_to_postgres.py --sqlite-path /var/data/bot_journal.sqlite3

It appends records with fresh target IDs and remaps paper_trades.external_signal_id
when its referenced alert is present in the same source SQLite file. A SHA-256
manifest prevents importing the same file twice. This copies paper records only;
it never places orders.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

TABLES = ("gainzalgo_signals", "webhook_events", "signal_log", "paper_trades")
SENSITIVE_KEYS = {"secret", "token", "api_key", "authorization", "password"}


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _target_columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=? ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def migrate(source_path: Path) -> None:
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("Set DATABASE_URL to the target Render PostgreSQL connection string first.")
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite file not found: {source_path}")
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()

    from db_store import connect_database, ensure_schema
    target = connect_database()
    if target.backend != "postgresql":
        target.close()
        raise RuntimeError("DATABASE_URL did not select PostgreSQL.")
    try:
        ensure_schema(target)
        target.execute("""CREATE TABLE IF NOT EXISTS bot_migration_log (
            source_sha256 TEXT PRIMARY KEY, source_name TEXT NOT NULL, migrated_at TEXT NOT NULL, row_counts TEXT NOT NULL
        )""")
        already = target.execute("SELECT source_sha256 FROM bot_migration_log WHERE source_sha256=?", (source_hash,)).fetchone()
        if already:
            print(f"SKIP: {source_path.name} was already imported (same SHA-256).")
            return

        src = sqlite3.connect(str(source_path))
        src.row_factory = None
        try:
            source_tables = {str(r[0]) for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            id_maps: dict[int, int] = {}
            counts: dict[str, int] = {}
            for table in TABLES:
                if table not in source_tables:
                    counts[table] = 0
                    continue
                source_cols = _sqlite_columns(src, table)
                target_cols = _target_columns(target, table)
                common = [c for c in source_cols if c in target_cols and c != "id"]
                rows = src.execute(f"SELECT id, {', '.join(common)} FROM {table} ORDER BY id").fetchall()
                max_id_row = target.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()
                next_id = int(max_id_row[0] or 0) + 1
                imported = 0
                for row in rows:
                    old_id = int(row[0])
                    values = list(row[1:])
                    new_id = next_id
                    next_id += 1
                    if table == "paper_trades" and "external_signal_id" in common:
                        idx = common.index("external_signal_id")
                        old_alert_id = values[idx]
                        if old_alert_id is not None:
                            values[idx] = id_maps.get(int(old_alert_id))
                    cols = ["id"] + common
                    placeholders = ", ".join(["?"] * len(cols))
                    quoted_cols = ", ".join('"' + c.replace('"', '""') + '"' for c in cols)
                    target.execute(
                        f"INSERT INTO {table} ({quoted_cols}) VALUES ({placeholders})",
                        tuple([new_id] + values),
                    )
                    if table == "gainzalgo_signals":
                        id_maps[old_id] = new_id
                    imported += 1
                counts[table] = imported
                # Reset PostgreSQL's serial sequence so future inserts do not collide.
                safe_table = '"' + table.replace('"', '""') + '"'
                target.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), GREATEST(COALESCE((SELECT MAX(id) FROM {safe_table}), 1), 1), true)"
                )
            import json
            target.execute(
                "INSERT INTO bot_migration_log (source_sha256,source_name,migrated_at,row_counts) VALUES (?,?,?,?)",
                (source_hash, source_path.name, __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(), json.dumps(counts)),
            )
            target.commit()
            print(f"Imported {source_path.name}: {counts}")
        finally:
            src.close()
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-path", required=True, help="Path to the source SQLite bot_journal.sqlite3 file")
    args = parser.parse_args()
    try:
        migrate(Path(args.sqlite_path).expanduser())
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
