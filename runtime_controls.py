"""Shared persistent runtime switches for dashboard-controlled automation.

The toggles persist in runtime_settings so the Web Service and Worker can read the
same state from PostgreSQL. All switches default to OFF and fail closed if storage
is unavailable. No secret values are returned from this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db_store import connect_database, ensure_schema

MARKET_AUTOPILOT_KEY = "market_autopilot_enabled"
ACTIVE_TRADING_MODE_KEY = "active_trading_mode"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_runtime_setting(key: str, default: str = "") -> str:
    if not isinstance(key, str) or not key or len(key) > 100:
        return default
    conn = None
    try:
        conn = connect_database()
        ensure_schema(conn)
        row = conn.execute("SELECT setting_value FROM runtime_settings WHERE setting_key=?", (key,)).fetchone()
        conn.commit()
        return str(row[0]) if row else default
    except Exception:
        return default
    finally:
        if conn is not None:
            conn.close()


def set_runtime_setting(key: str, value: str) -> None:
    if not isinstance(key, str) or not key or len(key) > 100:
        raise ValueError("Invalid runtime-setting key.")
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError("Invalid runtime-setting value.")
    conn = connect_database()
    try:
        ensure_schema(conn)
        conn.execute(
            """INSERT INTO runtime_settings(setting_key,setting_value,updated_at) VALUES(?,?,?)
               ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_at=excluded.updated_at""",
            (key, value, _stamp()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def market_autopilot_enabled(conn: Any | None = None) -> bool:
    """Return the persistent, dashboard-controlled master autopilot state.

    Missing or unreadable settings fail closed: the automatic scanner/research
    trade pipeline remains off until an admin explicitly enables it. Callers that
    already hold a database connection may pass it to avoid a second SQLite
    connection while a journal transaction is open.
    """
    sentinel = "__MISSING__"
    if conn is not None:
        try:
            row = conn.execute("SELECT setting_value FROM runtime_settings WHERE setting_key=?", (MARKET_AUTOPILOT_KEY,)).fetchone()
            value = str(row[0]) if row else sentinel
        except Exception:
            return False
    else:
        value = get_runtime_setting(MARKET_AUTOPILOT_KEY, sentinel)
    return _truthy(value) if value != sentinel else False


def set_market_autopilot_enabled(enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean.")
    set_runtime_setting(MARKET_AUTOPILOT_KEY, "true" if enabled else "false")
    return enabled


def get_active_trading_mode() -> str:
    value = get_runtime_setting(ACTIVE_TRADING_MODE_KEY, "paper").strip().lower()
    return value if value in {"paper", "real"} else "paper"


def set_active_trading_mode(mode: str) -> str:
    value = str(mode or "").strip().lower()
    if value not in {"paper", "real"}:
        raise ValueError("mode must be paper or real.")
    set_runtime_setting(ACTIVE_TRADING_MODE_KEY, value)
    return value
