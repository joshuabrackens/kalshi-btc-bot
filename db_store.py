"""Shared SQLite/PostgreSQL storage adapter for the paper-only Kalshi bot.

Use DATABASE_URL for a database shared by Render's web service and background
worker. SQLite is supported for local development or a single combined service.
No live trading code lives in this module.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Optional

_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()


def _database_url() -> str:
    return (os.getenv("DATABASE_URL") or "").strip()


def backend_name() -> str:
    return "postgresql" if _database_url() else "sqlite"


def database_location() -> str:
    url = _database_url()
    if url:
        # Do not expose credentials in logs or status responses.
        return "DATABASE_URL configured (credentials hidden)"
    path = Path(os.getenv("BOT_DB_PATH", str(Path(__file__).resolve().parent / "bot_journal.sqlite3"))).expanduser()
    return str(path)


class _Cursor:
    def __init__(self, cursor: Any, lastrowid: Optional[int] = None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return int(getattr(self._cursor, "rowcount", -1))

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class DatabaseConnection:
    """Small common denominator for sqlite3 and psycopg3 connections."""
    def __init__(self, raw: Any, backend: str):
        self.raw = raw
        self.backend = backend

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> _Cursor:
        values = tuple(params or ())
        if self.backend == "sqlite":
            cur = self.raw.execute(sql, values)
            return _Cursor(cur, getattr(cur, "lastrowid", None))

        # Convert the application's qmark parameters to psycopg placeholders.
        # Psycopg uses percent formatting for parameters, so literal percent signs
        # in SQL (for example LIKE '%15M%') must be escaped as '%%' whenever a
        # parameter sequence is supplied. Escape literals before inserting %s.
        pg_sql = sql.replace("%", "%%").replace("?", "%s")
        match = re.match(r"\s*INSERT\s+INTO\s+[\"`]?([a-zA-Z_][\w]*)", pg_sql, re.IGNORECASE)
        auto_return_id = bool(match and match.group(1).lower() in _TABLE_COLUMNS and not re.search(r"\bRETURNING\b", pg_sql, re.IGNORECASE))
        if auto_return_id:
            pg_sql = pg_sql.rstrip().rstrip(";") + " RETURNING id"
        cur = self.raw.cursor()
        cur.execute(pg_sql, values)
        lastrowid = None
        if auto_return_id:
            row = cur.fetchone()
            if row:
                try:
                    lastrowid = int(row[0])
                except (ValueError, TypeError, IndexError):
                    lastrowid = None
        return _Cursor(cur, lastrowid)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


def connect_database() -> DatabaseConnection:
    url = _database_url()
    if url:
        try:
            import psycopg  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "DATABASE_URL is set, but psycopg is not installed. Add 'psycopg[binary]>=3.2,<4' to requirements."
            ) from exc
        # Render commonly supplies postgres://; libpq/psycopg expect postgresql://.
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        raw = psycopg.connect(url, connect_timeout=10)
        return DatabaseConnection(raw, "postgresql")

    path = Path(os.getenv("BOT_DB_PATH", str(Path(__file__).resolve().parent / "bot_journal.sqlite3"))).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path), timeout=15)
    raw.execute("PRAGMA busy_timeout = 15000")
    raw.execute("PRAGMA foreign_keys = ON")
    return DatabaseConnection(raw, "sqlite")


# Canonical application schema. Column types are deliberately plain numeric/text
# types so the same model works across SQLite and PostgreSQL.
_TABLE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "signal_log": [
        ("created_at", "TEXT"), ("market_ticker", "TEXT"), ("direction", "TEXT"),
        ("score", "INTEGER"), ("btc_price", "REAL"), ("strike_price", "REAL"),
        ("seconds_remaining", "INTEGER"), ("max_entry_cents", "REAL"), ("payload_json", "TEXT"),
        # Explicit evaluation metadata; old rows remain null and are not guessed.
        ("external_signal_id", "INTEGER"), ("decision_status", "TEXT"), ("proposed_direction", "TEXT"),
        ("entry_ask_cents", "REAL"), ("spread_cents", "REAL"), ("trend_bias", "TEXT"),
        ("trend_evidence_score", "INTEGER"), ("trend_snapshot_age_seconds", "REAL"),
        ("counterfactual_outcome", "TEXT"), ("counterfactual_pnl_cents", "REAL"),
        ("counterfactual_settled_at", "TEXT"), ("counterfactual_last_checked_at", "TEXT"),
        ("counterfactual_check_note", "TEXT"),
    ],
    "paper_trades": [
        ("created_at", "TEXT"), ("settled_at", "TEXT"), ("market_ticker", "TEXT"),
        ("direction", "TEXT"), ("contracts", "INTEGER"), ("entry_cents", "REAL"),
        ("fee_cents_per_contract", "REAL"), ("outcome", "TEXT"), ("pnl_cents", "REAL"),
        ("notes", "TEXT"), ("stop_loss_cents", "REAL"), ("timeframe", "TEXT"),
        ("entry_btc_price", "REAL"), ("high_water_btc", "REAL"), ("low_water_btc", "REAL"),
        ("trailing_stop_btc", "REAL"), ("last_stop_candle_timestamp", "TEXT"),
        ("last_trend_check_at", "TEXT"), ("trend_stop_shadow_triggered_at", "TEXT"),
        ("trend_stop_shadow_trigger_bid_cents", "REAL"), ("trend_stop_shadow_pnl_cents", "REAL"),
        ("trend_stop_shadow_reason", "TEXT"), ("trend_stop_shadow_candle_timestamp", "TEXT"),
        ("take_profit_target_cents", "REAL"), ("take_profit_activated_at", "TEXT"),
        ("take_profit_peak_bid_cents", "REAL"), ("take_profit_last_reversal_candle_timestamp", "TEXT"),
        ("entry_signal_source", "TEXT"), ("external_signal_id", "INTEGER"),
        ("entry_signal_log_id", "INTEGER"), ("entry_signal_score", "INTEGER"),
    ],
    "gainzalgo_signals": [
        ("created_at", "TEXT"), ("direction", "TEXT"), ("symbol", "TEXT"),
        ("timeframe", "TEXT"), ("payload_json", "TEXT"), ("processed_at", "TEXT"),
        ("processing_status", "TEXT"), ("processing_note", "TEXT"), ("fingerprint", "TEXT"),
    ],
    "webhook_events": [
        ("received_at", "TEXT"), ("status_code", "INTEGER"), ("status", "TEXT"),
        ("reason", "TEXT"), ("symbol", "TEXT"), ("timeframe", "TEXT"), ("direction", "TEXT"),
    ],
}


def _table_ddl(name: str, backend: str) -> str:
    id_type = "BIGSERIAL PRIMARY KEY" if backend == "postgresql" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    cols = _TABLE_COLUMNS[name]
    definitions = [f"id {id_type}"]
    for col, typ in cols:
        # PostgreSQL REAL is 32-bit; use DOUBLE PRECISION for prices/P&L.
        column_type = "DOUBLE PRECISION" if backend == "postgresql" and typ == "REAL" else typ
        # Required fields in the original journal retain NOT NULL requirements.
        required = {
            "signal_log": {"created_at", "direction", "score", "btc_price", "strike_price", "seconds_remaining", "payload_json"},
            "paper_trades": {"created_at", "market_ticker", "direction", "contracts", "entry_cents", "fee_cents_per_contract"},
            "gainzalgo_signals": {"created_at", "direction", "payload_json"},
            "webhook_events": {"received_at", "status_code", "status", "reason"},
        }[name]
        suffix = " NOT NULL" if col in required else ""
        definitions.append(f"{col} {column_type}{suffix}")
    return f"CREATE TABLE IF NOT EXISTS {name} ({', '.join(definitions)})"


def _existing_columns(conn: DatabaseConnection, table: str) -> set[str]:
    if conn.backend == "sqlite":
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ?",
        (table,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def ensure_schema(conn: DatabaseConnection) -> None:
    """Create/migrate schema once per process/database, safely for web + worker."""
    if _database_url():
        schema_key = "postgresql:" + sha256(_database_url().encode()).hexdigest()[:16]
    else:
        schema_key = "sqlite:" + database_location()
    with _INIT_LOCK:
        if schema_key in _INITIALIZED:
            return
        backend = conn.backend
        if backend == "postgresql":
            # Serialize schema creation/migrations if web and worker boot together.
            conn.execute("SELECT pg_advisory_xact_lock(872344221)")
        for table in _TABLE_COLUMNS:
            conn.execute(_table_ddl(table, backend))
            present = _existing_columns(conn, table)
            for col, typ in _TABLE_COLUMNS[table]:
                col_type = ("DOUBLE PRECISION" if backend == "postgresql" and typ == "REAL" else typ)
                if col not in present:
                    if backend == "postgresql":
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}")
                    else:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gainzalgo_queue ON gainzalgo_signals(processed_at, id)")
        # Preserve the alert audit trail if old versions stored duplicate fingerprints,
        # but clear duplicate keys before enforcing idempotency for concurrent webhooks.
        conn.execute(
            """UPDATE gainzalgo_signals SET fingerprint=NULL
               WHERE fingerprint IS NOT NULL
                 AND id NOT IN (SELECT MIN(id) FROM gainzalgo_signals
                                WHERE fingerprint IS NOT NULL GROUP BY fingerprint)"""
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_gainzalgo_fingerprint_unique "
            "ON gainzalgo_signals(fingerprint) WHERE fingerprint IS NOT NULL"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_open ON paper_trades(settled_at, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_log_recent ON signal_log(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_counterfactual ON signal_log(counterfactual_settled_at, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_counterfactual_lookup ON signal_log(decision_status, proposed_direction, counterfactual_settled_at, id)")
        # Append-only audit stream for alert status transitions, evaluations,
        # paper entries/exits, and rejected-signal counterfactual outcomes.
        audit_id_type = "BIGSERIAL PRIMARY KEY" if backend == "postgresql" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        conn.execute(f"""CREATE TABLE IF NOT EXISTS decision_audit (
            id {audit_id_type}, created_at TEXT NOT NULL, event_type TEXT NOT NULL,
            alert_id INTEGER, signal_log_id INTEGER, trade_id INTEGER, market_ticker TEXT,
            direction TEXT, decision TEXT, reason TEXT, entry_ask_cents REAL, spread_cents REAL,
            trend_bias TEXT, trend_evidence_score INTEGER, trend_snapshot_age_seconds REAL,
            internal_score INTEGER, payload_json TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_audit_recent ON decision_audit(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_audit_decision ON decision_audit(decision, id)")
        if backend == "postgresql":
            conn.execute("""CREATE TABLE IF NOT EXISTS worker_heartbeat (
                worker_name TEXT PRIMARY KEY, last_seen_at TEXT NOT NULL, signal_source TEXT,
                mode TEXT, last_action TEXT, last_reason TEXT)""")
        else:
            conn.execute("""CREATE TABLE IF NOT EXISTS worker_heartbeat (
                worker_name TEXT PRIMARY KEY, last_seen_at TEXT NOT NULL, signal_source TEXT,
                mode TEXT, last_action TEXT, last_reason TEXT)""")
        # Latest precomputed BTC chart state, shared between the worker and dashboard.
        # It is informational only and does not create or modify paper positions.
        conn.execute("""CREATE TABLE IF NOT EXISTS btc_trend_monitor (
            monitor_key TEXT PRIMARY KEY, updated_at TEXT NOT NULL,
            status TEXT NOT NULL, snapshot_json TEXT NOT NULL)""")
        # Persistent, idempotent post-trade learning journal. It stores an immutable
        # entry-feature snapshot plus realized paper P&L classification; it never
        # modifies entry rules. Historical settled trades are backfilled by the worker.
        learning_id_type = "BIGSERIAL PRIMARY KEY" if backend == "postgresql" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        learning_number_type = "DOUBLE PRECISION" if backend == "postgresql" else "REAL"
        conn.execute(f"""CREATE TABLE IF NOT EXISTS trade_learning (
            id {learning_id_type}, trade_id INTEGER NOT NULL UNIQUE, created_at TEXT NOT NULL,
            settled_at TEXT NOT NULL, market_ticker TEXT, direction TEXT, contracts INTEGER,
            entry_cents {learning_number_type}, fee_cents_per_contract {learning_number_type}, trade_outcome TEXT, pnl_result TEXT NOT NULL,
            pnl_cents {learning_number_type} NOT NULL, entry_signal_log_id INTEGER, signal_source TEXT, entry_score INTEGER,
            trend_bias TEXT, trend_evidence_score INTEGER, trend_snapshot_age_seconds {learning_number_type}, trend_alignment TEXT,
            entry_price_bucket TEXT, evidence_bucket TEXT, volume_confirmation TEXT, momentum_alignment TEXT,
            chase_warning INTEGER, feature_snapshot_json TEXT NOT NULL, review_json TEXT NOT NULL
        )""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_learning_trade_id ON trade_learning(trade_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_learning_settled ON trade_learning(settled_at, trade_id)")
        # Bounded adaptive entry-target state. It can only tighten the preferred
        # paper entry target; the configured 92c hard cap and dollar risk limits
        # are never modified by this policy.
        conn.execute("""CREATE TABLE IF NOT EXISTS adaptive_policy_state (
            policy_key TEXT PRIMARY KEY, base_target_cents REAL NOT NULL,
            effective_target_cents REAL NOT NULL, last_evaluated_trade_count INTEGER NOT NULL DEFAULT 0,
            last_evaluated_latest_trade_id INTEGER NOT NULL DEFAULT 0,
            adjustment_count INTEGER NOT NULL DEFAULT 0, last_sample_count INTEGER NOT NULL DEFAULT 0,
            last_sample_net_pnl_cents REAL, last_reason TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        adaptive_policy_columns = _existing_columns(conn, "adaptive_policy_state")
        if "last_evaluated_latest_trade_id" not in adaptive_policy_columns:
            if backend == "postgresql":
                conn.execute("ALTER TABLE adaptive_policy_state ADD COLUMN IF NOT EXISTS last_evaluated_latest_trade_id INTEGER NOT NULL DEFAULT 0")
            else:
                conn.execute("ALTER TABLE adaptive_policy_state ADD COLUMN last_evaluated_latest_trade_id INTEGER NOT NULL DEFAULT 0")
        # Existing records don't identify the TradingView chart timeframe; don't invent it.
        conn.execute(
            "UPDATE paper_trades SET timeframe = '15m contract; TV timeframe unknown' "
            "WHERE timeframe IS NULL AND upper(market_ticker) LIKE '%15M%'"
        )
        conn.commit()
        _INITIALIZED.add(schema_key)


def record_decision_audit(
    conn: DatabaseConnection, *, event_type: str, decision: str | None = None, reason: str | None = None,
    alert_id: int | None = None, signal_log_id: int | None = None, trade_id: int | None = None,
    market_ticker: str | None = None, direction: str | None = None,
    entry_ask_cents: float | None = None, spread_cents: float | None = None,
    trend_bias: str | None = None, trend_evidence_score: int | None = None,
    trend_snapshot_age_seconds: float | None = None, internal_score: int | None = None,
    payload: dict[str, Any] | None = None, created_at: str | None = None,
) -> int | None:
    """Append a best-effort, paper-only decision audit event. Never place orders."""
    import json
    from datetime import datetime, timezone

    stamp = created_at or datetime.now(timezone.utc).isoformat()
    safe_payload = payload if isinstance(payload, dict) else {}
    encoded = json.dumps(safe_payload, ensure_ascii=False, default=str, allow_nan=False)
    cur = conn.execute(
        """INSERT INTO decision_audit (created_at,event_type,alert_id,signal_log_id,trade_id,market_ticker,
             direction,decision,reason,entry_ask_cents,spread_cents,trend_bias,trend_evidence_score,
             trend_snapshot_age_seconds,internal_score,payload_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (stamp, str(event_type)[:80], alert_id, signal_log_id, trade_id, market_ticker, direction,
         str(decision)[:80] if decision is not None else None, str(reason)[:800] if reason is not None else None,
         entry_ask_cents, spread_cents, trend_bias, trend_evidence_score, trend_snapshot_age_seconds,
         internal_score, encoded),
    )
    return cur.lastrowid
