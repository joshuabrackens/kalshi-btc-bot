"""FastAPI runtime and dashboard API for the Kalshi BTC 15-minute PAPER bot.

This service records simulated trades/signals only. The market lookup endpoints
read public market data; no endpoint in this file submits an order.
"""
from __future__ import annotations

import hmac
import csv
import io
import zipfile
import json
import hashlib
import logging
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from signal_engine import MarketContext, evaluate_signal
from candle_feed import fetch_signal_candles, CandleFeedError
from db_store import connect_database, ensure_schema, backend_name, database_location, record_decision_audit
from performance_analytics import build_performance_analytics
from trade_learning import build_learning_report, sync_settled_trade_learning
from payload_safety import sanitize_sensitive_payload
from market_category_registry import category_profiles, classify_market
from market_category_reporting import build_research_category_performance
from market_router import scan_market_catalog
from trading_mode import LiveModeLocked, live_readiness_report, request_mode, trading_mode_status
from multi_user_security import multi_user_readiness
from market_discovery import discover
from kalshi_api import get_market, get_orderbook, get_events, get_multivariate_events, KalshiAPIError
from market_catalog_scanner import MarketCatalogScanner
from market_decision_pipeline import MarketDecisionPipeline
from admin_session import COOKIE_NAME, SESSION_TTL_SECONDS, create_admin_cookie, validate_admin_cookie
from kalshi_credentials import (CredentialVaultError, credential_status, save_credentials, load_credentials, delete_credentials, verify_credentials_read_only, normalize_environment)
from runtime_controls import market_autopilot_enabled, set_market_autopilot_enabled

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("BOT_DB_PATH", str(APP_DIR / "bot_journal.sqlite3"))).expanduser()

def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    if not pd.notna(value) or value == float("inf") or value == float("-inf"):
        value = default
    return max(minimum, value)


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 86400) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(maximum, value))


# Use explicit feed-prefixed cursors so the inventory includes both ordinary events and
# dynamically created multivariate/combo events. Cursor values are treated as opaque and
# never logged. The standard-event endpoint supplies official category metadata.
_CATALOG_MVE_START = "__kalshi_catalog_mve_start__"
_CATALOG_EVENTS_PREFIX = "__kalshi_catalog_events__:"
_CATALOG_MVE_PREFIX = "__kalshi_catalog_mve__:"


def _get_market_catalog_page(*, status: str = "open", limit: int = 200, cursor: str | None = None) -> dict[str, Any]:
    if cursor == _CATALOG_MVE_START or (isinstance(cursor, str) and cursor.startswith(_CATALOG_MVE_PREFIX)):
        upstream_cursor = None if cursor == _CATALOG_MVE_START else cursor[len(_CATALOG_MVE_PREFIX):]
        payload = get_multivariate_events(limit=limit, cursor=upstream_cursor, with_nested_markets=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise KalshiAPIError("Kalshi multivariate event catalog returned an invalid response.")
        result = dict(payload)
        result["_catalog_source"] = "multivariate_events"
        raw_cursor = payload.get("cursor")
        result["cursor"] = _CATALOG_MVE_PREFIX + raw_cursor if isinstance(raw_cursor, str) and raw_cursor else ""
        return result

    upstream_cursor = cursor[len(_CATALOG_EVENTS_PREFIX):] if isinstance(cursor, str) and cursor.startswith(_CATALOG_EVENTS_PREFIX) else cursor
    payload = get_events(status=status, limit=limit, cursor=upstream_cursor, with_nested_markets=True)
    # Retain compatibility with injectable providers and legacy test pages that return
    # direct market rows. Real production GET /events returns an `events` array.
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        return payload
    result = dict(payload)
    result["_catalog_source"] = "events"
    raw_cursor = payload.get("cursor")
    if isinstance(raw_cursor, str) and raw_cursor:
        result["cursor"] = _CATALOG_EVENTS_PREFIX + raw_cursor
    else:
        # The standard feed is exhausted; switch to the independent multivariate feed.
        # This sentinel has no secret and cannot be confused with a server-issued cursor.
        result["cursor"] = _CATALOG_MVE_START
    return result


# The scanner inventories standard and multivariate open-event markets. Its separate
# research pipeline evaluates a bounded rotating pool; inventory itself never orders.
market_scanner = MarketCatalogScanner(_get_market_catalog_page)
market_decision_pipeline = MarketDecisionPipeline(
    catalog_snapshot=market_scanner.analysis_snapshot,
    market_fetcher=get_market,
    orderbook_fetcher=get_orderbook,
)


@contextmanager
def db_connect() -> Iterator[Any]:
    """Open the configured shared database (PostgreSQL or single-service SQLite)."""
    conn = connect_database()
    try:
        ensure_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    """Initialize storage and optional background services using FastAPI lifespan."""
    startup()
    auto_scan_default = os.getenv("RENDER", "").strip().lower() == "true"
    # The admin dashboard switch is persisted in the shared DB. Restore scheduler
    # threads on service restart if that switch is already ON, even when optional
    # environment auto-start flags are false. Each scheduler still checks the master
    # switch every loop, so this does not activate trading by itself.
    saved_autopilot = market_autopilot_enabled()
    market_scanner.start_scheduler(enabled=_env_bool("MARKET_SCANNER_AUTO_START", auto_scan_default) or saved_autopilot)
    market_decision_pipeline.start_scheduler(enabled=_env_bool("MARKET_ANALYSIS_AUTO_START", auto_scan_default) or saved_autopilot)
    yield


app = FastAPI(
    title="Kalshi BTC 15-Minute Bot",
    description=(
        "Paper-trading runtime, decision audit, and research analytics. Public read-only routes "
        "expose diagnostics and a paginated public market inventory. Admin-only credential vault "
        "stores keys encrypted; real order placement remains unavailable in this build."
    ),
    version="0.3.0",
    lifespan=app_lifespan,
)


class Candle(BaseModel):
    timestamp: str | None = None
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)


class MarketInput(BaseModel):
    market_ticker: str | None = Field(default=None, max_length=120)
    strike_price: float = Field(gt=0)
    seconds_remaining: int = Field(ge=0, le=86400)
    up_ask_cents: float | None = Field(default=None, gt=0, lt=100)
    down_ask_cents: float | None = Field(default=None, gt=0, lt=100)
    spread_cents: float | None = Field(default=None, ge=0, le=100)
    estimated_fee_cents: float = Field(default=0, ge=0, le=100)


class EvaluateRequest(BaseModel):
    # Cap request work/memory for the public HTTP surface. This route is also
    # admin-gated because it appends diagnostic evaluations to the shared journal.
    candles_1m: list[Candle] = Field(min_length=20, max_length=500)
    candles_5m: list[Candle] = Field(min_length=20, max_length=500)
    candles_15m: list[Candle] = Field(min_length=20, max_length=500)
    market: MarketInput
    minimum_score: int = Field(default=65, ge=0, le=100)
    max_entry_cents: float = Field(default=92, gt=0, lt=100)


class PaperTradeRequest(BaseModel):
    market_ticker: str = Field(min_length=1, max_length=120)
    direction: str
    contracts: int = Field(ge=1, le=10)
    entry_cents: float = Field(gt=0, lt=100)
    fee_cents_per_contract: float = Field(default=0, ge=0, le=10)
    stop_loss_cents: float | None = Field(default=None, gt=0, lt=100)
    timeframe: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=500)


class PaperSettleRequest(BaseModel):
    outcome: str
    notes: str | None = Field(default=None, max_length=500)


class TradingModeRequest(BaseModel):
    requested_mode: Literal["paper", "real"]


def startup() -> None:
    with db_connect():
        pass
    # The worker is opt-in and runs only in paper mode.
    if _env_bool("PAPER_WORKER_ENABLED", False):
        from paper_worker import run_forever
        thread = threading.Thread(target=run_forever, name="paper-trading-worker", daemon=True)
        thread.start()


def _require_manual_api_auth(request: Request) -> None:
    """Fail closed on manual endpoints that can write journal/decision data.

    The automatic worker and signed TradingView webhook do not use these routes.
    A secret must be explicitly configured, and each manual HTTP caller must send
    it in X-Paper-Admin-Secret; otherwise a public deployment could be journal-spammed.
    """
    expected = os.getenv("PAPER_ADMIN_API_SECRET", "")
    if not expected:
        raise HTTPException(403, detail="Manual write API is disabled; configure PAPER_ADMIN_API_SECRET to enable it.")
    supplied = request.headers.get("x-paper-admin-secret", "")
    if not hmac.compare_digest(supplied.encode("utf-8", errors="replace"), expected.encode("utf-8", errors="replace")):
        raise HTTPException(401, detail="Invalid manual API credential.")


@app.get("/")
def home():
    dashboard_path = APP_DIR / "dashboard.html"
    if not dashboard_path.is_file():
        raise HTTPException(status_code=404, detail="dashboard.html is missing from the application directory.")
    return FileResponse(dashboard_path, media_type="text/html")


def _external_signal_max_age_seconds() -> int:
    """Match the worker's accepted external-alert age window."""
    fallback = _env_int("GAINZALGO_MAX_SIGNAL_AGE_SECONDS", 300, 30, 900)
    return _env_int("TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS", fallback, 30, 900)


def _current_window_decision(alerts: list[dict[str, Any]], now: datetime | None = None,
                             signal_source: str | None = None) -> dict[str, Any]:
    """Return only the effective signal for the current 15-minute window.

    Old alerts are deliberately never carried forward. For the first three minutes
    of each UTC-aligned window, the status explains that the indicator may still be
    responding. This grace period affects dashboard messaging only; it does not
    extend the worker's signal-freshness limit or create a synthetic alert/trade.
    """
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    window_seconds = 15 * 60
    window_start = datetime.fromtimestamp(int(now_utc.timestamp() // window_seconds) * window_seconds, timezone.utc)
    window_end = window_start + timedelta(seconds=window_seconds)
    elapsed = max(0, int((now_utc - window_start).total_seconds()))
    grace_seconds = _env_int("INDICATOR_RESPONSE_GRACE_SECONDS", 180, 0, 900)
    grace_remaining = max(0, grace_seconds - elapsed)
    max_age = _external_signal_max_age_seconds()
    source = (signal_source or os.getenv("PAPER_SIGNAL_SOURCE", "internal")).strip().lower()
    alert_mode = source in {"hybrid", "gainzalgo"}

    for alert in alerts:
        received_at = alert.get("received_at")
        try:
            created = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            created = created.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue

        age_seconds = max(0, int((now_utc - created).total_seconds()))
        if not alert_mode or created < window_start or created >= window_end or age_seconds > max_age:
            continue
        direction = _normalize_webhook_direction({"direction": alert.get("direction")})
        if direction not in {"UP", "DOWN", "WAIT"}:
            continue

        explicit_wait = direction == "WAIT"
        processing_status = str(alert.get("processing_status") or "queued").strip().lower()
        processing_note = str(alert.get("processing_note") or "").strip()
        processed_at = alert.get("processed_at")
        if explicit_wait:
            reason = "The indicator explicitly reported WAIT. This is a status update only; no paper trade is opened from it."
        elif processing_status in {"waiting_for_market", "waiting_for_orderbook"}:
            reason = processing_note or (
                "The worker has received the alert but is waiting for verified market data before completing evaluation."
            )
        elif processed_at and processing_status:
            reason = f"The worker recorded processing status '{processing_status}'. " + (processing_note or "See the saved decision audit for the final reason.")
        elif processing_status in {"queued", "pending", ""}:
            reason = "TradingView alert received, but the worker has not yet recorded an evaluation status. Check that the Background Worker uses the same DATABASE_URL and PAPER_SIGNAL_SOURCE=hybrid."
        else:
            reason = processing_note or f"A fresh {direction} alert is available in the current 15-minute window. It still must pass all worker safety checks; receipt does not mean a trade was placed."
        return {
            "status": "WAIT THIS WINDOW" if explicit_wait else f"{direction} SIGNAL RECEIVED",
            "source": "indicator_wait" if explicit_wait else "fresh_directional_alert",
            "phase": "indicator_wait" if explicit_wait else "signal_received",
            "automatic_fallback": False,
            "direction": direction,
            "alert_id": alert.get("id"),
            "alert_received_at": created.isoformat(),
            "alert_age_seconds": age_seconds,
            "symbol": alert.get("symbol"),
            "timeframe": alert.get("timeframe"),
            "processing_status": processing_status,
            "processing_note": processing_note or None,
            "processed_at": processed_at,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "window_elapsed_seconds": elapsed,
            "indicator_response_grace_seconds": grace_seconds,
            "indicator_grace_remaining_seconds": 0,
            "max_age_seconds": max_age,
            "reason": reason,
        }

    if not alert_mode:
        phase = "internal_mode"
        reason = f"Signal source is {source}. External-signal fallback is only used in hybrid/GainzAlgo mode."
        status = "NO EXTERNAL SIGNAL REQUIRED"
    elif grace_remaining > 0:
        phase = "awaiting_indicator"
        reason = (
            f"No fresh UP or DOWN signal has arrived for the current 15-minute window yet. "
            f"The indicator usually takes about 3 minutes to respond; allow another {grace_remaining} seconds. "
            "The current display remains WAIT THIS WINDOW, and no older signal is carried forward."
        )
        status = "WAIT THIS WINDOW"
    else:
        phase = "no_fresh_signal"
        reason = (
            "No fresh UP or DOWN signal has arrived within the expected 3-minute response period for this 15-minute window. "
            "Keep waiting for a fresh current-window signal; the bot will not reuse a prior UP/DOWN signal. "
            "This status is not a fabricated TradingView alert and does not open or close a paper position."
        )
        status = "WAIT THIS WINDOW"

    return {
        "status": status,
        "source": "automatic_fallback" if alert_mode else "internal_mode",
        "phase": phase,
        "automatic_fallback": alert_mode,
        "direction": "WAIT" if alert_mode else None,
        "alert_received_at": None,
        "alert_age_seconds": None,
        "symbol": None,
        "timeframe": None,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_elapsed_seconds": elapsed,
        "indicator_response_grace_seconds": grace_seconds,
        "indicator_grace_remaining_seconds": grace_remaining,
        "max_age_seconds": max_age,
        "reason": reason,
    }


def _strategy_source_label(source: Any, notes: Any = None) -> str:
    """Classify trade source without guessing missing historical metadata."""
    explicit = str(source or "").strip().lower()
    if explicit:
        return explicit
    note = str(notes or "")
    if "TradingView-primary hybrid paper entry" in note:
        return "hybrid"
    if "TradingView/GainzAlgo paper entry" in note:
        return "tradingview"
    if note.startswith("AUTO PAPER ENTRY"):
        return "internal_legacy"
    if "Internal strategy paper entry" in note:
        return "internal"
    return "legacy_unknown"


def _aggregate_strategy_performance(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """Group settled results by source and P&L sign, not exit reason text."""
    grouped: dict[str, dict[str, Any]] = {}
    for source, notes, settled_at, pnl_cents in rows:
        label = _strategy_source_label(source, notes)
        group = grouped.setdefault(label, {
            "source": label, "total_trades": 0, "settled_trades": 0,
            "wins": 0, "losses": 0, "break_even": 0, "open_positions": 0,
            "net_pnl_cents": 0.0,
        })
        group["total_trades"] += 1
        if settled_at is None:
            group["open_positions"] += 1
            continue
        group["settled_trades"] += 1
        pnl = float(pnl_cents or 0.0)
        group["net_pnl_cents"] += pnl
        if pnl > 0: group["wins"] += 1
        elif pnl < 0: group["losses"] += 1
        else: group["break_even"] += 1
    result: list[dict[str, Any]] = []
    for label in sorted(grouped):
        group = grouped[label]
        settled = group["settled_trades"]
        group["win_rate_percent"] = round(100 * group["wins"] / settled, 2) if settled else None
        group["net_pnl_cents"] = round(group["net_pnl_cents"], 4)
        group["net_pnl_dollars"] = round(group["net_pnl_cents"] / 100, 4)
        group["sample_note"] = (
            "No settled trades yet." if settled == 0 else
            "Small sample; do not tune rules from this group alone." if settled < 20 else
            "More observations available; keep validating across market conditions."
        )
        result.append(group)
    return result


def _dashboard_signal_from_row(row: tuple[Any, ...] | None) -> dict[str, Any] | None:
    """Normalize a persisted signal row for history/position-context display."""
    if not row:
        return None
    (signal_id, created_at, ticker, raw_direction, score, btc_price, strike, seconds_remaining,
     max_entry, decision_status, proposed_direction, entry_ask, spread, trend_bias,
     trend_score, trend_age, payload_json) = row
    try:
        payload = json.loads(payload_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    reasons = payload.get("reasons", [])
    warnings = payload.get("warnings", [])
    chart_trend = payload.get("chart_trend_at_decision")
    if not isinstance(chart_trend, dict):
        chart_trend = {}
    fusion = payload.get("fusion")
    if not isinstance(fusion, dict):
        fusion = {}
    external_signal = payload.get("external_signal") if isinstance(payload.get("external_signal"), dict) else {}
    external_signal_id = external_signal.get("id", payload.get("external_signal_id"))
    try:
        external_signal_id = int(external_signal_id) if external_signal_id is not None else None
    except (TypeError, ValueError, OverflowError):
        external_signal_id = None
    chosen_direction = str(raw_direction or "").upper()
    proposed_text = str(proposed_direction or "").upper()
    if chosen_direction not in {"BUY_UP", "BUY_DOWN", "UP", "DOWN"} and proposed_text in {"UP", "DOWN", "BUY_UP", "BUY_DOWN"}:
        chosen_direction = proposed_text
    try:
        created_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((datetime.now(timezone.utc) - created_dt.astimezone(timezone.utc)).total_seconds()))
        stale_after = max(180, 3 * _env_int("PAPER_WORKER_INTERVAL_SECONDS", 15, 1, 300))
        is_stale = age_seconds > stale_after
    except (TypeError, ValueError, OverflowError):
        age_seconds, is_stale = None, True
    try:
        score_value = int(score) if score is not None else None
    except (TypeError, ValueError, OverflowError):
        score_value = None
    try:
        trend_score_value = int(trend_score) if trend_score is not None else None
    except (TypeError, ValueError, OverflowError):
        trend_score_value = None
    return {
        "id": signal_id, "created_at": created_at, "market_ticker": ticker,
        "external_signal_id": external_signal_id,
        "direction": chosen_direction or "WAIT", "logged_direction": raw_direction,
        "proposed_direction": proposed_text or None, "decision_status": decision_status or payload.get("decision_status"),
        "score": score_value, "btc_price": btc_price, "strike_price": strike,
        "seconds_remaining": seconds_remaining, "max_entry_cents": max_entry,
        "distance_from_strike": payload.get("distance_from_strike"),
        "source": payload.get("source"),
        "win_probability_percent": payload.get("win_probability_percent"),
        "estimated_probability_percent": payload.get("estimated_probability_percent"),
        "entry_ask_cents": entry_ask if entry_ask is not None else payload.get("entry_ask_cents", fusion.get("entry_ask_cents")),
        "spread_cents": spread if spread is not None else payload.get("spread_cents", fusion.get("spread_cents")),
        "trend_bias": trend_bias or chart_trend.get("candidate_bias") or chart_trend.get("trend_bias"),
        "trend_evidence_score": trend_score_value if trend_score_value is not None else chart_trend.get("evidence_score"),
        "trend_snapshot_age_seconds": trend_age if trend_age is not None else chart_trend.get("age_seconds"),
        "chart_trend_at_decision": chart_trend,
        "fusion": fusion,
        "reasons": reasons if isinstance(reasons, list) else [],
        "warnings": warnings if isinstance(warnings, list) else [],
        "strategies": payload.get("strategies", payload.get("strategy_diagnostics", {})) if isinstance(payload.get("strategies", payload.get("strategy_diagnostics", {})), dict) else {},
        "disclaimer": payload.get("disclaimer", "Historical evaluation details; not a current-window signal or calibrated win probability."),
        "age_seconds": age_seconds, "is_stale": is_stale,
    }


@app.get("/api/dashboard")
def dashboard_data() -> dict[str, Any]:
    with db_connect() as conn:
        totals = conn.execute(
            """SELECT
                COUNT(*),
                SUM(CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN settled_at IS NOT NULL AND pnl_cents > 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN settled_at IS NOT NULL AND pnl_cents < 0 THEN 1 ELSE 0 END),
                COALESCE(SUM(CASE WHEN settled_at IS NOT NULL THEN pnl_cents ELSE 0 END), 0),
                SUM(CASE WHEN settled_at IS NULL THEN 1 ELSE 0 END),
                COALESCE(SUM(CASE WHEN settled_at IS NULL THEN contracts * (entry_cents + fee_cents_per_contract) ELSE 0 END), 0)
            FROM paper_trades"""
        ).fetchone()
        rows = conn.execute(
            """SELECT id, created_at, settled_at, market_ticker, direction,
                      contracts, entry_cents, outcome, pnl_cents, timeframe,
                      entry_signal_source, external_signal_id, entry_signal_log_id,
                      entry_signal_score, entry_btc_price, notes
               FROM paper_trades ORDER BY id DESC LIMIT 20"""
        ).fetchall()
        performance_rows = conn.execute(
            "SELECT entry_signal_source, notes, settled_at, pnl_cents FROM paper_trades"
        ).fetchall()
        signal_row = conn.execute(
            """SELECT created_at, market_ticker, direction, score, btc_price,
                      strike_price, seconds_remaining, max_entry_cents, payload_json,
                      trend_evidence_score
               FROM signal_log ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        directional_signal_row = conn.execute(
            """SELECT id,created_at,market_ticker,direction,score,btc_price,strike_price,seconds_remaining,max_entry_cents,
                      decision_status,proposed_direction,entry_ask_cents,spread_cents,trend_bias,trend_evidence_score,
                      trend_snapshot_age_seconds,payload_json
               FROM signal_log
               WHERE UPPER(COALESCE(direction,'')) IN ('BUY_UP','BUY_DOWN','UP','DOWN')
                  OR UPPER(COALESCE(proposed_direction,'')) IN ('BUY_UP','BUY_DOWN','UP','DOWN')
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        active_position_row = conn.execute(
            """SELECT id,created_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,
                      entry_btc_price,timeframe,entry_signal_source,external_signal_id,entry_signal_log_id,
                      entry_signal_score,notes,stop_loss_cents,take_profit_target_cents,take_profit_activated_at,
                      trailing_stop_btc,last_trend_check_at
               FROM paper_trades WHERE settled_at IS NULL ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        active_entry_signal_row = None
        if active_position_row:
            if active_position_row[11] is not None:
                active_entry_signal_row = conn.execute(
                    """SELECT id,created_at,market_ticker,direction,score,btc_price,strike_price,seconds_remaining,max_entry_cents,
                              decision_status,proposed_direction,entry_ask_cents,spread_cents,trend_bias,trend_evidence_score,
                              trend_snapshot_age_seconds,payload_json
                       FROM signal_log WHERE id=? LIMIT 1""", (active_position_row[11],)
                ).fetchone()
            if active_entry_signal_row is None and active_position_row[10] is not None:
                active_entry_signal_row = conn.execute(
                    """SELECT id,created_at,market_ticker,direction,score,btc_price,strike_price,seconds_remaining,max_entry_cents,
                              decision_status,proposed_direction,entry_ask_cents,spread_cents,trend_bias,trend_evidence_score,
                              trend_snapshot_age_seconds,payload_json
                       FROM signal_log WHERE external_signal_id=? ORDER BY id DESC LIMIT 1""", (active_position_row[10],)
                ).fetchone()
        external_alert_rows = conn.execute(
            """SELECT id, created_at, direction, symbol, timeframe, payload_json,
                      processed_at, processing_status, processing_note
               FROM gainzalgo_signals ORDER BY id DESC LIMIT 10"""
        ).fetchall()
        external_alert_count = int(conn.execute(
            "SELECT COUNT(*) FROM gainzalgo_signals"
        ).fetchone()[0] or 0)
        last_webhook_event = conn.execute(
            """SELECT received_at, status_code, status, reason, symbol, timeframe, direction
               FROM webhook_events ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        worker_row = conn.execute(
            """SELECT last_seen_at, signal_source, mode, last_action, last_reason
               FROM worker_heartbeat WHERE worker_name = 'paper_worker' LIMIT 1"""
        ).fetchone()
        trend_monitor_row = conn.execute(
            "SELECT updated_at, status, snapshot_json FROM btc_trend_monitor WHERE monitor_key=? LIMIT 1",
            ("btc_15m",),
        ).fetchone()
        adaptive_policy_row = conn.execute(
            """SELECT effective_target_cents,adjustment_count,last_sample_count,last_sample_net_pnl_cents,
                      last_reason,updated_at FROM adaptive_policy_state WHERE policy_key=? LIMIT 1""",
            ("btc_hybrid_entry_target",),
        ).fetchone()

    trend_monitor = None
    if trend_monitor_row:
        try:
            trend_monitor = json.loads(trend_monitor_row[2] or "{}")
            if not isinstance(trend_monitor, dict):
                trend_monitor = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            trend_monitor = {}
        trend_monitor.setdefault("updated_at", trend_monitor_row[0])
        trend_monitor.setdefault("status", trend_monitor_row[1])
        try:
            tm_updated = datetime.fromisoformat(str(trend_monitor.get("updated_at") or "").replace("Z", "+00:00"))
            if tm_updated.tzinfo is None:
                tm_updated = tm_updated.replace(tzinfo=timezone.utc)
            trend_monitor_age = max(0, int((datetime.now(timezone.utc) - tm_updated.astimezone(timezone.utc)).total_seconds()))
            trend_monitor["age_seconds"] = trend_monitor_age
            trend_monitor["is_stale"] = trend_monitor_age > max(120, 4 * _env_int("BTC_TREND_MONITOR_INTERVAL_SECONDS", 30, 15, 300))
        except (TypeError, ValueError, OverflowError):
            trend_monitor["age_seconds"] = None
            trend_monitor["is_stale"] = True

    worker_age = None
    if worker_row:
        try:
            heartbeat_time = datetime.fromisoformat(str(worker_row[0]).replace("Z", "+00:00"))
            if heartbeat_time.tzinfo is None:
                heartbeat_time = heartbeat_time.replace(tzinfo=timezone.utc)
            worker_age = max(0, int((datetime.now(timezone.utc) - heartbeat_time.astimezone(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            worker_age = None
    worker_stale_after = max(180, 3 * _env_int("PAPER_WORKER_INTERVAL_SECONDS", 60))
    worker_is_fresh = worker_age is not None and worker_age <= worker_stale_after

    total, settled, wins, losses, pnl, opened, exposure = totals
    strategy_performance = _aggregate_strategy_performance(performance_rows)
    signal = None
    if signal_row:
        keys = ["created_at", "market_ticker", "direction", "score", "btc_price",
                "strike_price", "seconds_remaining", "max_entry_cents"]
        signal = dict(zip(keys, signal_row[:8]))
        try:
            payload = json.loads(signal_row[8] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        signal["entry_ask_cents"] = payload.get("entry_ask_cents")
        signal["distance_from_strike"] = payload.get("distance_from_strike")
        signal["reasons"] = payload.get("reasons", []) if isinstance(payload.get("reasons", []), list) else []
        signal["warnings"] = payload.get("warnings", []) if isinstance(payload.get("warnings", []), list) else []
        signal["disclaimer"] = payload.get(
            "disclaimer", "Score is heuristic only; it is not a calibrated win probability."
        )
        signal["strategies"] = payload.get("strategies", payload.get("strategy_diagnostics", {}))
        chart_context = payload.get("chart_trend_at_decision") if isinstance(payload.get("chart_trend_at_decision"), dict) else {}
        signal["chart_trend_at_decision"] = chart_context
        signal["trend_evidence_score"] = (
            payload.get("trend_evidence_score")
            if payload.get("trend_evidence_score") is not None else
            chart_context.get("evidence_score")
            if chart_context.get("evidence_score") is not None else
            signal_row[9]
        )
        signal["decision_status"] = payload.get("decision_status")
        signal["entry_price_gate"] = payload.get("entry_price_gate") if isinstance(payload.get("entry_price_gate"), dict) else {}
        try:
            created = datetime.fromisoformat(str(signal["created_at"]).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = max(0, int((datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()))
            interval = _env_int("PAPER_WORKER_INTERVAL_SECONDS", 60)
            signal["age_seconds"] = age
            signal["is_stale"] = age > max(180, 3 * interval)
        except (TypeError, ValueError, OverflowError):
            signal["age_seconds"] = None
            signal["is_stale"] = True

    # External TradingView/GainzAlgo alerts are stored separately from evaluated
    # signals. Expose them independently so webhook traffic does not look stale
    # simply because /evaluate has not been called recently. Never return secrets
    # or full arbitrary webhook payloads to the dashboard.
    external_alerts = []
    for row in external_alert_rows:
        alert_id, received_at, direction, symbol, timeframe, payload_json, processed_at, processing_status, processing_note = row
        try:
            raw_payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_payload = {}
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        allowed_detail_keys = (
            "action", "signal", "message", "strategy", "interval", "exchange",
            "ticker", "market_ticker", "price", "close", "bar_time", "timestamp"
        )
        details = {
            key: raw_payload[key] for key in allowed_detail_keys
            if key in raw_payload and isinstance(raw_payload[key], (str, int, float, bool))
        }
        alert = {
            "id": alert_id,
            "received_at": received_at,
            "direction": direction,
            "symbol": symbol or None,
            "timeframe": timeframe or None,
            "details": details,
            "processed_at": processed_at,
            "processing_status": processing_status or "queued",
            "processing_note": processing_note,
        }
        try:
            created = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            alert["age_seconds"] = max(0, int((datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            alert["age_seconds"] = None
        external_alerts.append(alert)

    latest_directional_signal = _dashboard_signal_from_row(directional_signal_row)
    active_position = None
    if active_position_row:
        (position_id, position_created_at, position_ticker, position_direction, position_contracts,
         position_entry_cents, position_fee_cents, position_entry_btc, position_timeframe,
         position_source, position_external_id, position_signal_log_id, position_signal_score,
         position_notes, position_stop_cents, position_tp_target, position_tp_activated,
         position_trailing_stop, position_last_trend_check) = active_position_row
        try:
            opened_dt = datetime.fromisoformat(str(position_created_at).replace("Z", "+00:00"))
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.replace(tzinfo=timezone.utc)
            position_age_seconds = max(0, int((datetime.now(timezone.utc) - opened_dt.astimezone(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            position_age_seconds = None
        contracts_n = int(position_contracts or 0)
        entry_n = float(position_entry_cents or 0)
        fee_n = float(position_fee_cents or 0)
        entry_signal = _dashboard_signal_from_row(active_entry_signal_row)
        active_position = {
            "id": position_id, "created_at": position_created_at, "age_seconds": position_age_seconds,
            "market_ticker": position_ticker, "direction": str(position_direction or "").upper(),
            "contracts": contracts_n, "entry_cents": entry_n, "fee_cents_per_contract": fee_n,
            "entry_cost_dollars": round(contracts_n * (entry_n + fee_n) / 100, 4),
            "entry_btc_price": position_entry_btc, "timeframe": position_timeframe,
            "entry_signal_source": _strategy_source_label(position_source, position_notes),
            "external_signal_id": position_external_id, "entry_signal_log_id": position_signal_log_id,
            "entry_signal_score": position_signal_score, "notes": position_notes,
            "stop_loss_cents": position_stop_cents, "take_profit_target_cents": position_tp_target,
            "take_profit_activated_at": position_tp_activated, "trailing_stop_btc": position_trailing_stop,
            "last_trend_check_at": position_last_trend_check,
            "entry_signal": entry_signal,
            "entry_signal_context_available": entry_signal is not None,
            "context_note": ("Entry rationale is linked to the saved signal evaluation." if entry_signal else
                             "No linked signal-log row was available for this older position; the trade notes are shown without guessing a rationale."),
        }

    hard_entry_cap = min(99.0, max(1.0, _env_float("PAPER_MAX_ENTRY_CENTS", 92.0, 1.0)))
    configured_entry_target = min(hard_entry_cap, max(1.0, min(99.0, _env_float("PAPER_ENTRY_TARGET_MAX_CENTS", 80.0, 1.0))))
    adaptive_policy_enabled = (
        os.getenv("PAPER_SIGNAL_SOURCE", "internal").strip().lower() == "hybrid"
        and _env_bool("PAPER_ADAPTIVE_ENTRY_TARGET_ENABLED", True)
    )
    adaptive_entry_target = configured_entry_target
    adaptive_adjustment_count = 0
    adaptive_entry_reason = (
        "Bounded adaptation is enabled in hybrid paper mode; it waits for at least 20 settled hybrid trades before considering a conservative price-target adjustment."
        if adaptive_policy_enabled else "Adaptive entry targeting is inactive; configured entry target is in effect."
    )
    if adaptive_policy_row:
        adaptive_adjustment_count = int(adaptive_policy_row[1] or 0)
        adaptive_entry_reason = str(adaptive_policy_row[4] or adaptive_entry_reason)
        if adaptive_policy_enabled:
            try:
                adaptive_entry_target = min(configured_entry_target, float(adaptive_policy_row[0]))
            except (TypeError, ValueError, OverflowError):
                adaptive_entry_target = configured_entry_target

    return {
        "health": {
            "api_ok": True,
            "database_backend": backend_name(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "worker_enabled": _env_bool("PAPER_WORKER_ENABLED", False) or worker_is_fresh,
            "worker_status": "running" if worker_is_fresh else ("stale" if worker_row else "not_observed"),
            "worker_last_seen_at": worker_row[0] if worker_row else None,
            "worker_last_seen_age_seconds": worker_age,
            "worker_signal_source": worker_row[1] if worker_row else None,
            "worker_mode": worker_row[2] if worker_row else None,
            "worker_last_action": worker_row[3] if worker_row else None,
            "worker_last_reason": worker_row[4] if worker_row else None,
            "signal_source": os.getenv("PAPER_SIGNAL_SOURCE", "internal").strip().lower(),
            "trend_stop_enabled": _env_bool("PAPER_TREND_STOP_LOSS_ENABLED", False),
            "trend_stop_shadow_mode": _env_bool("PAPER_TREND_STOP_SHADOW_MODE", False),
            "kalshi_api": "Public market lookup routes available; check service logs for worker status",
        },
        "risk": {
            "max_trade_dollars": round(_env_float("MAX_TRADE_DOLLARS", 1.00, 0.01), 2),
            "max_daily_loss_dollars": round(_env_float("MAX_DAILY_LOSS_DOLLARS", 10.00, 0.01), 2),
            "open_exposure_dollars": round(float(exposure or 0) / 100, 2),
            "configured_hard_max_entry_cents": round(hard_entry_cap, 2),
            "configured_entry_target_max_cents": round(configured_entry_target, 2),
            "entry_target_max_cents": round(adaptive_entry_target, 2),
            "adaptive_entry_target_enabled": adaptive_policy_enabled,
            "adaptive_entry_target_adjustment_count": adaptive_adjustment_count,
            "adaptive_entry_target_reason": adaptive_entry_reason,
        },
        "stats": {
            "total_trades": int(total or 0),
            "settled_trades": int(settled or 0),
            "wins": int(wins or 0),
            "losses": int(losses or 0),
            "open_positions": int(opened or 0),
            "win_rate_percent": round(100 * (wins or 0) / settled, 2) if settled else None,
            "net_pnl_dollars": round(float(pnl or 0) / 100, 2),
        },
        "strategy_performance": strategy_performance,
        "btc_trend_monitor": trend_monitor,
        "latest_signal": signal,
        "latest_directional_signal": latest_directional_signal,
        "active_position": active_position,
        "latest_external_alert": external_alerts[0] if external_alerts else None,
        "external_alert_count": external_alert_count,
        "recent_external_alerts": external_alerts,
        "current_window_decision": _current_window_decision(
            external_alerts, now=datetime.now(timezone.utc),
            signal_source=os.getenv("PAPER_SIGNAL_SOURCE", "internal"),
        ),
        "latest_webhook_event": ({
            "received_at": last_webhook_event[0], "status_code": last_webhook_event[1],
            "status": last_webhook_event[2], "reason": last_webhook_event[3],
            "symbol": last_webhook_event[4] or None, "timeframe": last_webhook_event[5] or None,
            "direction": last_webhook_event[6] or None,
        } if last_webhook_event else None),
        "trades": [
            {"id": r[0], "created_at": r[1], "settled_at": r[2], "market_ticker": r[3],
             "direction": r[4], "contracts": r[5], "entry_cents": r[6], "outcome": r[7],
             "pnl_cents": r[8], "timeframe": r[9],
             "entry_signal_source": _strategy_source_label(r[10], r[15]),
             "external_signal_id": r[11], "entry_signal_log_id": r[12],
             "entry_signal_score": r[13], "entry_btc_price": r[14],
             "market_implied_probability_percent": round(float(r[6]), 2) if r[6] is not None else None,
             "pnl_dollars": round(float(r[8]) / 100, 2) if r[8] is not None else None}
            for r in rows
        ],
        "mode": "SIMULATION_ONLY",
        "real_orders_enabled": False,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "time_utc": datetime.now(timezone.utc).isoformat(), "mode": "SIMULATION_ONLY"}


def candles_to_df(candles: list[Candle]) -> pd.DataFrame:
    # Pydantic v2 is used by current FastAPI; model_dump is the supported serializer.
    return pd.DataFrame([c.model_dump() for c in candles])


@app.post("/evaluate")
def evaluate(request: EvaluateRequest, http_request: Request) -> dict[str, Any]:
    """Admin-only manual evaluation; writes diagnostics but never opens a trade."""
    _require_manual_api_auth(http_request)
    try:
        market = MarketContext(
            strike_price=request.market.strike_price,
            seconds_remaining=request.market.seconds_remaining,
            up_ask_cents=request.market.up_ask_cents,
            down_ask_cents=request.market.down_ask_cents,
            spread_cents=request.market.spread_cents,
            estimated_fee_cents=request.market.estimated_fee_cents,
        )
        signal = evaluate_signal(
            candles_to_df(request.candles_1m), candles_to_df(request.candles_5m),
            candles_to_df(request.candles_15m), market,
            minimum_score=request.minimum_score, max_entry_cents=request.max_entry_cents,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    payload = signal.to_dict()
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        payload_json = json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Signal engine returned data that could not be serialized safely.") from exc
    with db_connect() as conn:
        conn.execute(
            """INSERT INTO signal_log
               (created_at, market_ticker, direction, score, btc_price,
                strike_price, seconds_remaining, max_entry_cents, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (created_at, request.market.market_ticker, payload["direction"], payload["score"],
             payload["btc_price"], payload["strike_price"], payload["seconds_remaining"],
             payload.get("max_entry_cents"), payload_json),
        )
    # The dashboard can now show entry_ask_cents separately from the configured cap.
    return {"created_at": created_at, **payload, "mode": "SIMULATION_ONLY", "trade_placed": False}


@app.get("/journal/recent")
def recent_signals(limit: int = 25) -> dict[str, Any]:
    if not 1 <= limit <= 250:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 250")
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT id, created_at, market_ticker, direction, score, btc_price,
                      strike_price, seconds_remaining, max_entry_cents, payload_json
               FROM signal_log ORDER BY id DESC LIMIT ?""", (limit,)
        ).fetchall()
    signals = []
    for row in rows:
        keys = ["id", "created_at", "market_ticker", "direction", "score", "btc_price",
                "strike_price", "seconds_remaining", "max_entry_cents"]
        item = dict(zip(keys, row[:9]))
        try:
            payload = json.loads(row[9] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        item["entry_ask_cents"] = payload.get("entry_ask_cents") if isinstance(payload, dict) else None
        item["reasons"] = payload.get("reasons", []) if isinstance(payload, dict) else []
        item["warnings"] = payload.get("warnings", []) if isinstance(payload, dict) else []
        signals.append(item)
    return {"signals": signals}


@app.get("/api/alerts/recent")
def recent_external_alerts(limit: int = 25) -> dict[str, Any]:
    """Return recent received external alerts, with secrets and raw payload omitted."""
    if not 1 <= limit <= 250:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 250")
    allowed_detail_keys = (
        "action", "signal", "message", "strategy", "interval", "exchange",
        "ticker", "market_ticker", "price", "close", "bar_time", "timestamp"
    )
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT id, created_at, direction, symbol, timeframe, payload_json,
                      processed_at, processing_status, processing_note
               FROM gainzalgo_signals ORDER BY id DESC LIMIT ?""", (limit,)
        ).fetchall()
    alerts = []
    for alert_id, received_at, direction, symbol, timeframe, payload_json, processed_at, processing_status, processing_note in rows:
        try:
            raw_payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_payload = {}
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        details = {key: raw_payload[key] for key in allowed_detail_keys
                   if key in raw_payload and isinstance(raw_payload[key], (str, int, float, bool))}
        alerts.append({"id": alert_id, "received_at": received_at, "direction": direction,
                       "symbol": symbol or None, "timeframe": timeframe or None, "details": details,
                       "processed_at": processed_at, "processing_status": processing_status or "queued",
                       "processing_note": processing_note})
    return {"count_returned": len(alerts), "alerts": alerts,
            "note": "These are received webhook alerts, not evaluated trade signals or placed orders."}


_MARKET_DATA_DIAGNOSTIC_LOCK = threading.Lock()
_MARKET_DATA_DIAGNOSTIC_CACHE: dict[str, Any] | None = None
_MARKET_DATA_DIAGNOSTIC_CACHE_AT = 0.0
_MARKET_DATA_DIAGNOSTIC_TTL_SECONDS = 60.0


def _diagnostic_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _diagnostic_price_cents(market: dict[str, Any], side: str, kind: str) -> float | None:
    dollars = market.get(f"{side}_{kind}_dollars")
    cents = market.get(f"{side}_{kind}")
    try:
        if dollars is not None:
            value = float(dollars) * 100.0
        elif cents is not None:
            value = float(cents)
        else:
            return None
        return value if pd.notna(value) and 0 <= value <= 100 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _diagnostic_top_bid(book: dict[str, Any], side: str) -> float | None:
    if not isinstance(book, dict):
        return None
    orderbook = book.get("orderbook_fp", book.get("orderbook", book))
    if not isinstance(orderbook, dict):
        return None
    bids: list[float] = []
    for key in (side, f"{side}_dollars"):
        levels = orderbook.get(key)
        if not isinstance(levels, list):
            continue
        for level in levels:
            try:
                price = float(level[0])
                if key.endswith("_dollars"):
                    price *= 100.0
                if pd.notna(price) and 0 <= price <= 100:
                    bids.append(price)
            except (TypeError, ValueError, IndexError, OverflowError):
                continue
    return max(bids) if bids else None


def _diagnostic_parse_candidate(candidate: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    market = candidate.get("raw", candidate)
    if not isinstance(market, dict):
        return None
    ticker = str(market.get("ticker") or candidate.get("ticker") or "")
    if not ticker.upper().startswith("KXBTC15M-"):
        return None
    title = " ".join(str(market.get(key, "")) for key in ("title", "subtitle", "event_ticker")).lower()
    if "up" not in title or ("btc" not in title and "bitcoin" not in title):
        return None
    if str(market.get("status", candidate.get("status", ""))).lower() in {"settled", "finalized", "closed", "inactive"}:
        return None
    strike = None
    for key in ("floor_strike", "strike_price", "custom_strike", "strike"):
        try:
            if market.get(key) is not None:
                strike = float(market[key])
                break
        except (TypeError, ValueError, OverflowError):
            pass
    if strike is None or not pd.notna(strike) or strike <= 0:
        return None
    close_at = _diagnostic_timestamp(market.get("close_time") or market.get("expiration_time")
                                     or candidate.get("close_time") or candidate.get("expiration_time"))
    if close_at is None:
        return None
    seconds_remaining = int((close_at - now).total_seconds())
    if not 0 < seconds_remaining <= 900:
        return None
    return {"ticker": ticker, "title": title, "strike_price": strike,
            "close_time": close_at.isoformat(), "seconds_remaining": seconds_remaining}


def _probe_market_data() -> dict[str, Any]:
    """Manually probe the same public data sources used by the paper worker.

    This endpoint never trades. The 60-second cache prevents repeated clicks from
    creating a burst of public API requests or worsening HTTP 429 rate limits.
    """
    checked_at = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "checked_at_utc": checked_at,
        "mode": "SIMULATION_ONLY",
        "trade_placed": False,
        "cache_ttl_seconds": int(_MARKET_DATA_DIAGNOSTIC_TTL_SECONDS),
        "sources": {},
    }

    # BTC candle source used by signal_engine during hybrid evaluation.
    candles_source: dict[str, Any] = {"provider": "Binance public REST", "status": "error"}
    try:
        bundle = fetch_signal_candles()
        interval_by_key = {"candles_1m": "1m", "candles_5m": "5m", "candles_15m": "15m"}
        intervals: dict[str, Any] = {}
        for key, interval in interval_by_key.items():
            frame = bundle.get(key)
            if frame is None or getattr(frame, "empty", True) or "timestamp" not in frame.columns or "close" not in frame.columns:
                intervals[interval] = {"status": "error", "reason": "missing candles or required columns"}
                continue
            latest_at = _diagnostic_timestamp(frame["timestamp"].iloc[-1])
            if latest_at is None:
                intervals[interval] = {"status": "error", "reason": "latest candle timestamp is invalid or lacks timezone"}
                continue
            # candle_feed timestamps are Binance candle OPEN times, not close times.
            # Derive approximate close time so displayed age matches real freshness.
            interval_seconds = {"1m": 60, "5m": 300, "15m": 900}[interval]
            latest_close_at = latest_at + timedelta(seconds=interval_seconds)
            now_at_check = datetime.now(timezone.utc)
            age_since_close = max(0.0, (now_at_check - latest_close_at).total_seconds())
            age_since_open = max(0.0, (now_at_check - latest_at).total_seconds())
            try:
                close_price = float(frame["close"].iloc[-1])
            except (TypeError, ValueError, OverflowError):
                close_price = None
            if close_price is None or not pd.notna(close_price) or close_price <= 0:
                intervals[interval] = {"status": "error", "reason": "latest candle close is invalid"}
                continue
            # Match candle_feed's closed-candle stale guard (two intervals plus a small buffer).
            max_age = {"1m": 130.0, "5m": 610.0, "15m": 1810.0}[interval]
            intervals[interval] = {
                "status": "ok" if age_since_close <= max_age else "stale",
                "latest_candle_open_at": latest_at.isoformat(),
                "approximate_candle_close_at": latest_close_at.isoformat(),
                "age_seconds": round(age_since_close, 2),
                "seconds_since_candle_open": round(age_since_open, 2),
                "close_price": round(close_price, 4),
                "max_age_seconds": max_age,
            }
        candles_ok = bool(intervals) and all(item.get("status") == "ok" for item in intervals.values())
        candles_source = {"provider": "Binance public REST", "status": "ok" if candles_ok else "degraded",
                          "symbol": "BTCUSDT", "intervals": intervals}
    except Exception as exc:
        candles_source["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    result["sources"]["btc_candles"] = candles_source

    # Kalshi market metadata and order book, using the same discovery/client modules as the worker.
    kalshi_source: dict[str, Any] = {"provider": "Kalshi public REST", "status": "error"}
    try:
        found = discover()
        candidates = found.get("candidates", []) if isinstance(found, dict) else []
        now = datetime.now(timezone.utc)
        valid = [_diagnostic_parse_candidate(c, now) for c in candidates if isinstance(c, dict)]
        valid = [item for item in valid if item is not None]
        valid.sort(key=lambda item: item["seconds_remaining"])
        kalshi_source["candidate_count"] = int(found.get("candidate_count", len(candidates)) or 0) if isinstance(found, dict) else len(candidates)
        if not valid:
            kalshi_source.update({"status": "not_ready", "reason": "No open KXBTC15M market passed ticker, title, strike, and current-window expiry checks."})
        else:
            selected = valid[0]
            details_payload = get_market(selected["ticker"])
            market = details_payload.get("market", details_payload) if isinstance(details_payload, dict) else {}
            # Re-validate the fetched detail response rather than trusting only discovery data.
            detail_candidate = _diagnostic_parse_candidate({"raw": market}, datetime.now(timezone.utc))
            if detail_candidate is None or detail_candidate["ticker"] != selected["ticker"]:
                kalshi_source.update({"status": "not_ready", "reason": "Detailed market response did not pass the same current-window checks."})
            else:
                book = get_orderbook(selected["ticker"])
                # Kalshi's orderbook exposes bids on YES and NO, not direct asks.
                # Derive asks from the opposite-side best bid to avoid mixing a
                # potentially stale market-summary ask with the current order book.
                yes_bid = _diagnostic_top_bid(book, "yes")
                no_bid = _diagnostic_top_bid(book, "no")
                yes_ask = (100.0 - no_bid) if no_bid is not None else None
                no_ask = (100.0 - yes_bid) if yes_bid is not None else None
                quotes = {"yes_bid_cents": yes_bid, "yes_ask_cents": yes_ask,
                          "no_bid_cents": no_bid, "no_ask_cents": no_ask}
                quotes_complete = all(value is not None for value in quotes.values())
                quotes_valid = bool(
                    quotes_complete
                    and 0 < yes_bid < 100 and 0 < no_bid < 100
                    and 0 < yes_ask < 100 and 0 < no_ask < 100
                    and yes_bid <= yes_ask and no_bid <= no_ask
                )
                summary_yes_ask = _diagnostic_price_cents(market, "yes", "ask")
                summary_no_ask = _diagnostic_price_cents(market, "no", "ask")
                summary_differs = (
                    summary_yes_ask is not None and yes_ask is not None and abs(summary_yes_ask - yes_ask) > 1.0
                ) or (
                    summary_no_ask is not None and no_ask is not None and abs(summary_no_ask - no_ask) > 1.0
                )
                kalshi_source.update({
                    "status": "ok" if quotes_valid else "degraded",
                    "ticker": selected["ticker"], "title": detail_candidate["title"],
                    "strike_price": detail_candidate["strike_price"],
                    "close_time": detail_candidate["close_time"],
                    "seconds_remaining": detail_candidate["seconds_remaining"],
                    "quote_source": "current orderbook bids; asks derived as 100 minus opposite-side best bid",
                    "quotes": quotes,
                    "market_summary_asks_cents": {"yes_ask_cents": summary_yes_ask, "no_ask_cents": summary_no_ask},
                    "market_summary_asks_differ_from_orderbook": bool(summary_differs),
                    "quotes_valid": bool(quotes_valid),
                    "reason": (
                        "Orderbook-derived quotes are valid; market summary asks differ and are ignored."
                        if quotes_valid and summary_differs else
                        None if quotes_valid else
                        "Orderbook is missing usable best bids or implied asks are invalid/crossed."
                    ),
                })
    except Exception as exc:
        kalshi_source["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    result["sources"]["kalshi_market"] = kalshi_source
    result["ready_for_evaluation"] = candles_source.get("status") == "ok" and kalshi_source.get("status") == "ok"
    result["note"] = "Diagnostic only: checks public market data and closed BTC candles. It never opens, closes, or modifies a paper trade. A healthy feed does not guarantee a profitable signal."
    return result


@app.get("/diagnostics/market-data")
def market_data_diagnostics() -> dict[str, Any]:
    """Manually check fresh Binance candle and Kalshi market/order-book data.

    Results are cached for 60 seconds to limit external API traffic. This route is
    observational only and cannot open/close a position.
    """
    global _MARKET_DATA_DIAGNOSTIC_CACHE, _MARKET_DATA_DIAGNOSTIC_CACHE_AT
    with _MARKET_DATA_DIAGNOSTIC_LOCK:
        now = time.monotonic()
        if (_MARKET_DATA_DIAGNOSTIC_CACHE is not None
                and now - _MARKET_DATA_DIAGNOSTIC_CACHE_AT < _MARKET_DATA_DIAGNOSTIC_TTL_SECONDS):
            cached = dict(_MARKET_DATA_DIAGNOSTIC_CACHE)
            cached["cache"] = "cached"
            cached["cache_age_seconds"] = round(now - _MARKET_DATA_DIAGNOSTIC_CACHE_AT, 2)
            return cached
        report = _probe_market_data()
        _MARKET_DATA_DIAGNOSTIC_CACHE = dict(report)
        _MARKET_DATA_DIAGNOSTIC_CACHE_AT = time.monotonic()
        report["cache"] = "fresh"
        report["cache_age_seconds"] = 0.0
        return report


@app.get("/status")
def status() -> dict[str, Any]:
    with db_connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
    enabled = _env_bool("PAPER_WORKER_ENABLED", False)
    with db_connect() as conn:
        heartbeat = conn.execute("SELECT last_seen_at,last_action,last_reason FROM worker_heartbeat WHERE worker_name='paper_worker' LIMIT 1").fetchone()
    return {
        "status": "starter-ready", "signals_logged": count, "database_backend": backend_name(), "database_path": database_location(),
        "worker_heartbeat": ({"last_seen_at": heartbeat[0], "last_action": heartbeat[1], "last_reason": heartbeat[2]} if heartbeat else None),
        "live_market_data": "on-demand during signal evaluation: Binance closed 1m/5m/15m BTC candles plus Kalshi market/order-book quotes; idle wait cycles do not continuously poll feeds.",
        "market_data_diagnostics": "/diagnostics/market-data (manual public-feed check; cached for 60 seconds)",
        "telegram_alerts": "not connected",
        "paper_trading": "automatic paper worker enabled" if enabled else "manual ledger; automatic worker disabled",
        "paper_worker_enabled": enabled,
        "real_orders": "not implemented; no real order path exists",
        "mode": "SIMULATION_ONLY",
    }


@app.post("/paper/open")
def open_paper_trade(request: PaperTradeRequest, http_request: Request) -> dict[str, Any]:
    """Admin-only manual simulated position; never sends an order to Kalshi."""
    _require_manual_api_auth(http_request)
    direction = request.direction.strip().upper()
    if direction not in {"UP", "DOWN"}:
        raise HTTPException(status_code=422, detail="direction must be UP or DOWN")
    ticker = request.market_ticker.strip()
    if not ticker:
        raise HTTPException(status_code=422, detail="market_ticker cannot be blank")

    today = datetime.now(timezone.utc).date().isoformat()
    max_trade_dollars = _env_float("MAX_TRADE_DOLLARS", 1.00, 0.01)
    max_daily_loss_dollars = _env_float("MAX_DAILY_LOSS_DOLLARS", 10.00, 0.01)
    total_cost_cents = request.contracts * (request.entry_cents + request.fee_cents_per_contract)
    if total_cost_cents > max_trade_dollars * 100 + 1e-9:
        raise HTTPException(status_code=422, detail=(
            f"Trade blocked: total entry cost plus fees ({total_cost_cents / 100:.2f} USD) "
            f"exceeds MAX_TRADE_DOLLARS ({max_trade_dollars:.2f} USD)."
        ))

    with db_connect() as conn:
        daily_pnl_cents = float(conn.execute(
            """SELECT COALESCE(SUM(pnl_cents), 0) FROM paper_trades
               WHERE settled_at IS NOT NULL AND substr(settled_at, 1, 10) = ?""", (today,)
        ).fetchone()[0])
        daily_count = int(conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE substr(created_at, 1, 10) = ?", (today,)
        ).fetchone()[0])
        daily_cap = _env_int("PAPER_MAX_TRADES_PER_DAY", 96, 1, 96)
        if daily_count >= daily_cap:
            raise HTTPException(status_code=429, detail="Paper-trade daily cap reached.")
        if daily_pnl_cents <= -(max_daily_loss_dollars * 100):
            raise HTTPException(status_code=429, detail="Paper trading halted: daily realized loss limit reached.")
        duplicate = conn.execute(
            """SELECT id FROM paper_trades
               WHERE market_ticker = ? AND direction = ? AND settled_at IS NULL""", (ticker, direction)
        ).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail=f"Unsettled paper trade already exists (id {duplicate[0]}).")
        created_at = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """INSERT INTO paper_trades
               (created_at, market_ticker, direction, contracts, entry_cents,
                fee_cents_per_contract, notes, stop_loss_cents, timeframe)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (created_at, ticker, direction, request.contracts, request.entry_cents,
             request.fee_cents_per_contract, request.notes, request.stop_loss_cents,
             request.timeframe or ("15m contract; TV timeframe unknown" if "15M" in ticker.upper() else None)),
        )
        trade_id = cur.lastrowid
        record_decision_audit(
            conn, event_type="paper_trade_opened", trade_id=trade_id, market_ticker=ticker,
            direction=direction, decision="paper_opened", reason="Manual simulated paper entry recorded through paper-only API.",
            entry_ask_cents=request.entry_cents, created_at=created_at,
            payload={"contracts": request.contracts, "fee_cents_per_contract": request.fee_cents_per_contract,
                     "total_entry_cost_cents": round(total_cost_cents, 4), "origin": "manual_paper_api", "mode": "SIMULATION_ONLY"},
        )

    return {
        "paper_trade_id": trade_id, "mode": "SIMULATION_ONLY", "status": "open",
        "market_ticker": ticker, "direction": direction, "contracts": request.contracts,
        "entry_cents": request.entry_cents, "stop_loss_cents": request.stop_loss_cents,
        "timeframe": request.timeframe or ("15m contract; TV timeframe unknown" if "15M" in ticker.upper() else None),
        "created_at": created_at,
        "max_gross_cost_cents": round(request.contracts * request.entry_cents, 4),
        "total_entry_cost_including_fees_dollars": round(total_cost_cents / 100, 4),
        "note": "No live order was placed. stop_loss_cents is stored for reference only; this endpoint does not monitor prices or execute automatic exits.",
    }


@app.post("/paper/{trade_id}/settle")
def settle_paper_trade(trade_id: int, request: PaperSettleRequest, http_request: Request) -> dict[str, Any]:
    """Admin-only manual settlement of a simulated position; no real order is sent."""
    _require_manual_api_auth(http_request)
    outcome = request.outcome.strip().upper()
    if outcome not in {"UP", "DOWN"}:
        raise HTTPException(status_code=422, detail="outcome must be UP or DOWN")
    with db_connect() as conn:
        row = conn.execute(
            """SELECT id, direction, contracts, entry_cents, fee_cents_per_contract,
                      settled_at, market_ticker FROM paper_trades WHERE id = ?""", (trade_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Paper trade not found.")
        if row[5]:
            raise HTTPException(status_code=409, detail="Paper trade is already settled.")
        _, direction, contracts, entry_cents, fee_cents, _, ticker = row
        gross_payout = 100 * int(contracts) if direction == outcome else 0
        cost = float(entry_cents) * int(contracts)
        fees = float(fee_cents) * int(contracts)
        pnl = gross_payout - cost - fees
        settled_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE paper_trades SET settled_at = ?, outcome = ?, pnl_cents = ?,
                      notes = COALESCE(?, notes) WHERE id = ?""",
            (settled_at, outcome, pnl, request.notes, trade_id),
        )
        record_decision_audit(
            conn, event_type="paper_trade_settled", trade_id=int(trade_id), market_ticker=ticker,
            direction=str(direction), decision="settled", reason="Manually settled simulated paper position from supplied UP/DOWN outcome.",
            entry_ask_cents=float(entry_cents), created_at=settled_at,
            payload={"outcome": outcome, "net_pnl_cents": round(pnl, 4), "origin": "manual_paper_api", "mode": "SIMULATION_ONLY"},
        )
        try:
            sync_settled_trade_learning(conn, batch_size=1000)
        except Exception as learning_exc:
            # Learning is retrospective only; a journal-learning failure must not
            # undo or alter the already-recorded simulated settlement.
            logging.getLogger(__name__).warning("Trade-learning sync after manual paper settlement failed: %s", learning_exc)
    return {
        "paper_trade_id": trade_id, "market_ticker": ticker, "outcome": outcome,
        "gross_payout_cents": gross_payout, "entry_cost_cents": round(cost, 4),
        "fees_cents": round(fees, 4), "net_pnl_cents": round(pnl, 4),
        "net_pnl_dollars": round(pnl / 100, 4), "mode": "SIMULATION_ONLY",
    }


@app.get("/paper/trades")
def paper_trades(limit: int = 50) -> dict[str, Any]:
    if not 1 <= limit <= 250:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 250")
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT id, created_at, settled_at, market_ticker, direction, contracts,
                      entry_cents, fee_cents_per_contract, outcome, pnl_cents, notes, stop_loss_cents, timeframe,
                      entry_signal_source, external_signal_id, entry_signal_log_id, entry_signal_score, entry_btc_price
               FROM paper_trades ORDER BY id DESC LIMIT ?""", (limit,)
        ).fetchall()
    keys = ["id", "created_at", "settled_at", "market_ticker", "direction", "contracts",
            "entry_cents", "fee_cents_per_contract", "outcome", "pnl_cents", "notes", "stop_loss_cents", "timeframe",
            "entry_signal_source", "external_signal_id", "entry_signal_log_id", "entry_signal_score", "entry_btc_price"]
    trades = []
    for row in rows:
        trade = dict(zip(keys, row))
        trade["entry_signal_source"] = _strategy_source_label(trade.get("entry_signal_source"), trade.get("notes"))
        trade["market_implied_probability_percent"] = round(float(trade["entry_cents"]), 2) if trade.get("entry_cents") is not None else None
        trades.append(trade)
    return {"trades": trades}


@app.get("/paper/stats")
def paper_stats() -> dict[str, Any]:
    with db_connect() as conn:
        total, settled, wins, pnl = conn.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END),
                      SUM(CASE WHEN settled_at IS NOT NULL AND pnl_cents > 0 THEN 1 ELSE 0 END),
                      COALESCE(SUM(CASE WHEN settled_at IS NOT NULL THEN pnl_cents ELSE 0 END), 0)
               FROM paper_trades"""
        ).fetchone()
        performance_rows = conn.execute(
            "SELECT entry_signal_source, notes, settled_at, pnl_cents FROM paper_trades"
        ).fetchall()
    settled, wins = int(settled or 0), int(wins or 0)
    return {
        "mode": "SIMULATION_ONLY", "total_paper_trades": int(total or 0),
        "settled_trades": settled, "winning_trades": wins,
        "win_rate_percent": round(100 * wins / settled, 2) if settled else None,
        "net_pnl_cents": round(float(pnl), 4), "net_pnl_dollars": round(float(pnl) / 100, 4),
        "strategy_performance": _aggregate_strategy_performance(performance_rows),
        "important": "Evidence scores are not probabilities. Source-level metrics are descriptive and small samples should not drive strategy changes.",
    }


@app.get("/api/decision-audit")
def decision_audit(limit: int = 100) -> dict[str, Any]:
    """Recent append-only decision history. All content is paper-only and read-only."""
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT id,created_at,event_type,alert_id,signal_log_id,trade_id,market_ticker,direction,
                      decision,reason,entry_ask_cents,spread_cents,trend_bias,trend_evidence_score,
                      trend_snapshot_age_seconds,internal_score,payload_json
               FROM decision_audit ORDER BY id DESC LIMIT ?""", (limit,)
        ).fetchall()
    keys = ["id", "created_at", "event_type", "alert_id", "signal_log_id", "trade_id", "market_ticker",
            "direction", "decision", "reason", "entry_ask_cents", "spread_cents", "trend_bias",
            "trend_evidence_score", "trend_snapshot_age_seconds", "internal_score", "payload_json"]
    events = []
    for row in rows:
        item = dict(zip(keys, row))
        try:
            payload = json.loads(item.pop("payload_json", "{}") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        item["payload"] = payload if isinstance(payload, dict) else {}
        events.append(item)
    return {"mode": "SIMULATION_ONLY", "count": len(events), "events": events,
            "note": "Append-only diagnostics; audit entries do not submit, open, close, or enable real orders."}


@app.get("/api/diagnostics/replay-export")
def download_replay_export(request: Request) -> Response:
    """Admin-only curated CSV bundle for historical outcome replay/audit.

    No credential table, private key, runtime secret, or raw webhook payload is
    exported. This lets the owner evaluate actual historical decisions without
    granting this service direct access to the user's local analysis environment.
    """
    _require_admin_session(request)
    row_cap = _env_int("REPLAY_EXPORT_MAX_ROWS", 10000, 100, 50000)
    files: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {}
    with db_connect() as conn:
        files["paper_trades.csv"] = (
            ["id", "created_at", "settled_at", "market_ticker", "direction", "contracts", "entry_cents",
             "fee_cents_per_contract", "outcome", "pnl_cents", "notes", "timeframe", "entry_signal_source",
             "entry_signal_id", "entry_signal_log_id", "entry_signal_score"],
            conn.execute(
                """SELECT id,created_at,settled_at,market_ticker,direction,contracts,entry_cents,
                          fee_cents_per_contract,outcome,pnl_cents,notes,timeframe,entry_signal_source,
                          external_signal_id,entry_signal_log_id,entry_signal_score
                   FROM paper_trades ORDER BY id ASC LIMIT ?""", (row_cap,)
            ).fetchall(),
        )
        files["trade_learning.csv"] = (
            ["id", "trade_id", "created_at", "settled_at", "market_ticker", "direction", "contracts",
             "entry_cents", "trade_outcome", "pnl_result", "pnl_cents", "signal_source", "entry_score",
             "trend_bias", "trend_evidence_score", "trend_alignment", "entry_price_bucket", "evidence_bucket",
             "volume_confirmation", "momentum_alignment", "chase_warning", "feature_snapshot_json", "review_json"],
            conn.execute(
                """SELECT id,trade_id,created_at,settled_at,market_ticker,direction,contracts,entry_cents,
                          trade_outcome,pnl_result,pnl_cents,signal_source,entry_score,trend_bias,trend_evidence_score,
                          trend_alignment,entry_price_bucket,evidence_bucket,volume_confirmation,momentum_alignment,
                          chase_warning,feature_snapshot_json,review_json
                   FROM trade_learning ORDER BY trade_id ASC LIMIT ?""", (row_cap,)
            ).fetchall(),
        )
        files["signal_decisions.csv"] = (
            ["id", "created_at", "market_ticker", "direction", "score", "decision_status", "proposed_direction",
             "entry_ask_cents", "spread_cents", "trend_bias", "trend_evidence_score", "trend_snapshot_age_seconds",
             "counterfactual_outcome", "counterfactual_pnl_cents", "counterfactual_settled_at", "counterfactual_check_note"],
            conn.execute(
                """SELECT id,created_at,market_ticker,direction,score,decision_status,proposed_direction,
                          entry_ask_cents,spread_cents,trend_bias,trend_evidence_score,trend_snapshot_age_seconds,
                          counterfactual_outcome,counterfactual_pnl_cents,counterfactual_settled_at,counterfactual_check_note
                   FROM signal_log ORDER BY id ASC LIMIT ?""", (row_cap,)
            ).fetchall(),
        )
        files["market_research_decisions.csv"] = (
            ["id", "evaluated_at", "market_ticker", "category", "decision", "selected_side",
             "model_probability_yes", "expected_net_edge_cents", "model_id", "model_version", "payload_json"],
            conn.execute(
                """SELECT id,evaluated_at,market_ticker,category,decision,selected_side,model_probability_yes,
                          expected_net_edge_cents,model_id,model_version,payload_json
                   FROM market_research_decisions ORDER BY id ASC LIMIT ?""", (row_cap,)
            ).fetchall(),
        )
        files["market_research_prediction_outcomes.csv"] = (
            ["id", "market_ticker", "category", "model_id", "model_version", "evaluated_at", "settled_at",
             "model_probability_yes", "actual_outcome_yes", "outcome_source", "recorded_at"],
            conn.execute(
                """SELECT id,market_ticker,category,model_id,model_version,evaluated_at,settled_at,
                          model_probability_yes,actual_outcome_yes,outcome_source,recorded_at
                   FROM market_research_prediction_outcomes ORDER BY id ASC LIMIT ?""", (row_cap,)
            ).fetchall(),
        )
        files["market_research_paper_trades.csv"] = (
            ["id", "created_at", "settled_at", "market_ticker", "category", "side", "contracts", "entry_cents",
             "estimated_fee_cents", "estimated_slippage_cents", "model_probability_yes", "model_id", "model_version",
             "expected_net_edge_cents", "status", "result", "pnl_cents", "source_snapshot_json"],
            conn.execute(
                """SELECT id,created_at,settled_at,market_ticker,category,side,contracts,entry_cents,
                          estimated_fee_cents,estimated_slippage_cents,model_probability_yes,model_id,model_version,
                          expected_net_edge_cents,status,result,pnl_cents,source_snapshot_json
                   FROM market_research_paper_trades ORDER BY id ASC LIMIT ?""", (row_cap,)
            ).fetchall(),
        )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for filename, (headers, rows) in files.items():
            text = io.StringIO(newline="")
            writer = csv.writer(text)
            writer.writerow(headers)
            writer.writerows(rows)
            zipped.writestr(filename, text.getvalue().encode("utf-8"))
        zipped.writestr("README.txt", (
            "Historical replay export generated by the BTC bot. Contains curated trade and decision data only.\n"
            "It excludes API credentials, private key files, environment secrets, and raw webhook payloads.\n"
            "Rows are capped per file by REPLAY_EXPORT_MAX_ROWS. Upload this ZIP for actual historical review;\n"
            "local synthetic tests cannot establish the result of your production trades.\n"
        ))
    return Response(content=archive.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=btc_bot_historical_replay_export.zip",
                             "Cache-Control": "no-store"})


@app.get("/api/analytics")
def performance_analytics() -> dict[str, Any]:
    """Paper-trade cohorts, decision counts, counterfactual rejected-signal outcomes, and exit-shadow review."""
    with db_connect() as conn:
        trade_rows = conn.execute(
            """SELECT id,created_at,settled_at,market_ticker,direction,contracts,entry_cents,
                      fee_cents_per_contract,outcome,pnl_cents,entry_signal_source,entry_signal_log_id,
                      entry_signal_score,trend_stop_shadow_triggered_at,trend_stop_shadow_pnl_cents,
                      external_signal_id,timeframe
               FROM paper_trades ORDER BY id DESC LIMIT 10000"""
        ).fetchall()
        linked_rows = conn.execute("SELECT id,payload_json FROM signal_log ORDER BY id DESC LIMIT 10000").fetchall()
        audit_rows = conn.execute(
            """SELECT id,created_at,event_type,alert_id,signal_log_id,trade_id,market_ticker,direction,
                      decision,reason,entry_ask_cents,spread_cents,trend_bias,trend_evidence_score,
                      trend_snapshot_age_seconds,internal_score,payload_json
               FROM decision_audit ORDER BY id DESC LIMIT 250"""
        ).fetchall()
        cf_rows = conn.execute(
            """SELECT id,created_at,market_ticker,proposed_direction,decision_status,entry_ask_cents,
                      trend_bias,trend_evidence_score,counterfactual_outcome,counterfactual_pnl_cents,
                      counterfactual_settled_at,counterfactual_check_note
               FROM signal_log WHERE counterfactual_settled_at IS NOT NULL ORDER BY id DESC LIMIT 5000"""
        ).fetchall()
        pending_cf = conn.execute(
            """SELECT COUNT(*) FROM signal_log WHERE proposed_direction IN ('UP','DOWN')
                AND entry_ask_cents > 0 AND entry_ask_cents < 100
                AND decision_status IN ('vetoed','skipped','wait_for_better_price','wait','blocked','ignored')
                AND counterfactual_settled_at IS NULL"""
        ).fetchone()[0]

    trade_keys = ["id", "created_at", "settled_at", "market_ticker", "direction", "contracts", "entry_cents",
                  "fee_cents_per_contract", "outcome", "pnl_cents", "entry_signal_source", "entry_signal_log_id",
                  "entry_signal_score", "trend_stop_shadow_triggered_at", "trend_stop_shadow_pnl_cents",
                  "external_signal_id", "timeframe"]
    trades = [dict(zip(trade_keys, row)) for row in trade_rows]
    linked_payloads: dict[int, Any] = {}
    for signal_id, payload_json in linked_rows:
        try:
            linked_payloads[int(signal_id)] = json.loads(payload_json or "{}")
        except (ValueError, TypeError, json.JSONDecodeError):
            linked_payloads[int(signal_id)] = {}

    event_keys = ["id", "created_at", "event_type", "alert_id", "signal_log_id", "trade_id", "market_ticker",
                  "direction", "decision", "reason", "entry_ask_cents", "spread_cents", "trend_bias",
                  "trend_evidence_score", "trend_snapshot_age_seconds", "internal_score", "payload_json"]
    events = []
    for row in audit_rows:
        item = dict(zip(event_keys, row))
        try: item["payload"] = json.loads(item.pop("payload_json", "{}") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError): item["payload"] = {}
        if not isinstance(item["payload"], dict): item["payload"] = {}
        events.append(item)
    cf_keys = ["id", "created_at", "market_ticker", "proposed_direction", "decision_status", "entry_ask_cents",
               "trend_bias", "trend_evidence_score", "counterfactual_outcome", "counterfactual_pnl_cents",
               "counterfactual_settled_at", "counterfactual_check_note"]
    counterfactuals = [dict(zip(cf_keys, row)) for row in cf_rows]
    result = build_performance_analytics(trades, linked_payloads, events, counterfactuals)
    result["rejected_signal_counterfactuals_pending"] = int(pending_cf or 0)
    result["counterfactual_method"] = (
        "Only rejected/no-entry decision records with a valid observed ask and a finally settled official market result are included. "
        "Each uses one hypothetical contract held to settlement and includes the recorded entry fee; exits/stops and market impact are excluded."
    )
    result["mode"] = "SIMULATION_ONLY"
    return result


@app.get("/api/learning")
def trade_learning_report() -> dict[str, Any]:
    """Post-trade feedback report. It only records outcome snapshots/reads insights; it never tunes live rules."""
    with db_connect() as conn:
        return build_learning_report(conn)


async def _read_json_request_limited(request: Request, max_bytes: int) -> Any:
    """Read at most max_bytes from a JSON request, including chunked bodies without Content-Length."""
    declared = request.headers.get("content-length", "").strip()
    if declared:
        try:
            declared_size = int(declared)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(status_code=400, detail="Invalid content length.") from None
        if declared_size < 0:
            raise HTTPException(status_code=400, detail="Invalid content length.")
        if declared_size > max_bytes:
            raise HTTPException(status_code=413, detail="Request exceeds the allowed size.")
    body = bytearray()
    async for chunk in request.stream():
        if not isinstance(chunk, (bytes, bytearray)):
            raise HTTPException(status_code=400, detail="Invalid request body.")
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(status_code=413, detail="Request exceeds the allowed size.")
        body.extend(chunk)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON request.") from None


def _admin_secret_configured() -> str:
    secret = os.getenv("KALSHI_VAULT_ADMIN_SECRET", "").strip()
    if len(secret) < 32 or "REPLACE_WITH" in secret.upper():
        raise HTTPException(status_code=503, detail="Credential-vault controls are unavailable: configure a random KALSHI_VAULT_ADMIN_SECRET of at least 32 characters (not the example placeholder).")
    return secret


def _is_admin_session(request: Request) -> bool:
    try:
        secret = _admin_secret_configured()
    except HTTPException:
        return False
    return validate_admin_cookie(request.cookies.get(COOKIE_NAME), secret)


def _require_admin_session(request: Request) -> None:
    if not _is_admin_session(request):
        raise HTTPException(status_code=401, detail="Admin session required. Unlock the dashboard controls first.")


@app.get("/api/admin/session")
def api_admin_session(request: Request) -> dict[str, Any]:
    configured_secret = os.getenv("KALSHI_VAULT_ADMIN_SECRET", "").strip()
    configured = len(configured_secret) >= 32 and "REPLACE_WITH" not in configured_secret.upper()
    return {"admin_secret_configured": configured, "authenticated": _is_admin_session(request),
            "session_ttl_seconds": SESSION_TTL_SECONDS, "kalshi_credentials_returned": False}


@app.post("/api/admin/login")
async def api_admin_login(request: Request) -> dict[str, Any]:
    body = await _read_json_request_limited(request, 2048)
    if not isinstance(body, dict) or not isinstance(body.get("secret"), str) or len(body["secret"]) > 512:
        raise HTTPException(status_code=400, detail="Invalid login request.")
    expected = _admin_secret_configured()
    if not hmac.compare_digest(body["secret"].encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Admin secret did not match.")
    response = JSONResponse({"authenticated": True, "expires_in_seconds": SESSION_TTL_SECONDS})
    response.set_cookie(COOKIE_NAME, create_admin_cookie(expected), max_age=SESSION_TTL_SECONDS,
                        httponly=True, secure=os.getenv("RENDER", "").strip().lower() == "true",
                        samesite="strict", path="/")
    return response


@app.post("/api/admin/logout")
def api_admin_logout(request: Request):
    from fastapi.responses import JSONResponse
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE_NAME, path="/", secure=os.getenv("RENDER", "").strip().lower() == "true",
                          httponly=True, samesite="strict")
    return response


@app.get("/api/kalshi-credentials")
def api_kalshi_credentials_status(request: Request) -> dict[str, Any]:
    _require_admin_session(request)
    with db_connect() as conn:
        return credential_status(conn)


@app.put("/api/kalshi-credentials")
async def api_save_kalshi_credentials(request: Request) -> dict[str, Any]:
    _require_admin_session(request)
    body = await _read_json_request_limited(request, 20_000)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid credential request.")
    try:
        with db_connect() as conn:
            return save_credentials(conn, body.get("environment"), body.get("api_key_id"), body.get("private_key_pem"))
    except CredentialVaultError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.post("/api/kalshi-credentials/verify")
async def api_verify_kalshi_credentials(request: Request) -> dict[str, Any]:
    _require_admin_session(request)
    try:
        body = await _read_json_request_limited(request, 2048)
        environment = normalize_environment(body.get("environment") if isinstance(body, dict) else None)
        with db_connect() as conn:
            credentials = load_credentials(conn, environment)
        return verify_credentials_read_only(credentials)
    except CredentialVaultError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid verification request.") from None


@app.delete("/api/kalshi-credentials/{environment}")
def api_delete_kalshi_credentials(environment: str, request: Request) -> dict[str, Any]:
    _require_admin_session(request)
    try:
        env = normalize_environment(environment)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    with db_connect() as conn:
        delete_credentials(conn, env)
    return {"deleted": True, "environment": env, "credential_returned": False}


@app.get("/api/trading-mode")
def api_trading_mode() -> dict[str, Any]:
    """Expose the effective mode and fail-closed live switch state."""
    return trading_mode_status()


@app.post("/api/trading-mode")
def api_request_trading_mode(request: TradingModeRequest) -> dict[str, Any]:
    """Dashboard mode control. Live mode is rejected; no order API is called."""
    try:
        return request_mode(request.requested_mode)
    except LiveModeLocked as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "live_mode_locked",
                "message": str(exc),
                "effective_mode": exc.status.get("effective_mode", "blocked"),
                "live_execution_enabled": False,
                "live_blockers": exc.status.get("live_blockers", []),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/live-readiness")
def api_live_readiness() -> dict[str, Any]:
    """Read-only live-risk readiness checklist; it cannot enable real execution."""
    return live_readiness_report()


@app.get("/api/multi-user-readiness")
def api_multi_user_readiness() -> dict[str, Any]:
    """Describe remaining production requirements before any multi-user access."""
    return multi_user_readiness()


@app.get("/api/market-scan")
def api_market_scan() -> dict[str, Any]:
    """Return progress/results for the background all-open-market inventory scan."""
    return market_scanner.snapshot()


@app.post("/api/market-scan")
def api_start_market_scan() -> dict[str, Any]:
    """Start or resume a bounded full catalog scan; no trading action is possible."""
    return market_scanner.start(force=True)


@app.get("/api/market-automation")
def api_market_automation(request: Request) -> dict[str, Any]:
    """Expose dashboard master autopilot state; never exposes credentials or turns live mode on."""
    enabled = market_autopilot_enabled()
    provider_ok = bool(market_decision_pipeline.snapshot().get("provider_configured"))
    paper_gate = _env_bool("MARKET_AUTOPAPER_ENABLED", False)
    safe_mode = os.getenv("TRADING_MODE", "paper").strip().lower() == "paper" and not _env_bool("LIVE_TRADING_ENABLED", False)
    return {
        "autopilot_enabled": enabled,
        "scanner_enabled": enabled,
        "provider_configured": provider_ok,
        "autopaper_environment_gate": paper_gate,
        "paper_mode_safe": safe_mode,
        "admin_authenticated": _is_admin_session(request),
        "real_orders_enabled": False,
        "mode": "PAPER_RESEARCH_ONLY",
        "note": "The master autopilot toggle controls automatic market catalog scanning and eligible research-paper entries only. It never places real orders.",
    }


@app.post("/api/market-automation")
async def api_set_market_automation(request: Request) -> dict[str, Any]:
    """Enable or pause automatic catalog scanning + research paper entries via a gated admin control."""
    body = await _read_json_request_limited(request, 2048)
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        raise HTTPException(status_code=400, detail="Request must contain a boolean 'enabled' value.")
    enabled = body["enabled"]
    if not _is_admin_session(request):
        raise HTTPException(status_code=401, detail="Unlock dashboard secure controls before changing autopilot.")
    if enabled:
        provider_ok = bool(market_decision_pipeline.snapshot().get("provider_configured"))
        if not provider_ok:
            raise HTTPException(status_code=409, detail="Autopilot remains off: configure the HTTPS probability/evidence provider URL and shared bearer token first.")
        if not _env_bool("MARKET_AUTOPAPER_ENABLED", False):
            raise HTTPException(status_code=409, detail="Autopilot remains off: MARKET_AUTOPAPER_ENABLED must be true as a deployment safety gate after provider validation.")
        if os.getenv("TRADING_MODE", "paper").strip().lower() != "paper" or _env_bool("LIVE_TRADING_ENABLED", False):
            raise HTTPException(status_code=409, detail="Autopilot remains off: configure TRADING_MODE=paper and LIVE_TRADING_ENABLED=false.")
    try:
        set_market_autopilot_enabled(enabled)
    except Exception as exc:
        log.warning("Market autopilot runtime setting unavailable (%s).", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Could not persist the autopilot setting to the shared database; state remains fail-closed.") from None
    if enabled:
        # Start schedulers even if their environment auto-start flags were false.
        # Both loops consult the persistent switch on every cycle and fail closed.
        market_scanner.start_scheduler(enabled=True)
        market_decision_pipeline.start_scheduler(enabled=True)
        if market_scanner.snapshot().get("status") in {"idle", "failed", "partial"}:
            market_scanner.start(force=False)
    return api_market_automation(request)


@app.get("/api/market-analysis")
def api_market_analysis() -> dict[str, Any]:
    """Show recent edge/evidence checks and isolated research-paper performance."""
    result = market_decision_pipeline.snapshot()
    try:
        with db_connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*), SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN status='settled' AND pnl_cents>0 THEN 1 ELSE 0 END),
                          SUM(CASE WHEN status='settled' AND pnl_cents<0 THEN 1 ELSE 0 END),
                          SUM(CASE WHEN status='settled' THEN pnl_cents ELSE 0 END)
                   FROM market_research_paper_trades"""
            ).fetchone()
            total = int(row[0] or 0)
            settled = int(row[2] or 0)
            wins = int(row[3] or 0)
            result["research_paper_performance"] = {
                "total_trades": total, "open_trades": int(row[1] or 0), "settled_trades": settled,
                "wins": wins, "losses": int(row[4] or 0),
                "win_rate_percent": round(wins * 100 / settled, 2) if settled else None,
                "net_pnl_cents": round(float(row[5] or 0), 3),
                "note": "Separate research-only hold-to-settlement paper journal; not merged into the BTC 15-minute strategy stats.",
            }
            # The full recent ledger makes virtual entry, costs and official settlement visible
            # so an open paper position cannot be confused with a hypothetical forecast.
            position_rows = conn.execute(
                """SELECT id,created_at,settled_at,market_ticker,category,side,contracts,entry_cents,
                          estimated_fee_cents,estimated_slippage_cents,model_probability_yes,
                          expected_net_edge_cents,status,result,pnl_cents,source_snapshot_json,
                          model_id,model_version
                   FROM market_research_paper_trades ORDER BY id DESC LIMIT 50"""
            ).fetchall()
            research_paper_trades: list[dict[str, Any]] = []
            for position in position_rows:
                (trade_id, opened_at, settled_at, ticker, category, side, contracts, entry_cents,
                 fee_cents, slippage_cents, p_yes, edge, status, outcome, pnl, snapshot_json,
                 model_id, model_version) = position
                try:
                    saved = json.loads(snapshot_json or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    saved = {}
                decision = saved.get("decision") if isinstance(saved, dict) and isinstance(saved.get("decision"), dict) else {}
                selection = decision.get("paper_selection") if isinstance(decision.get("paper_selection"), dict) else {}
                selected_probability = None
                if p_yes is not None:
                    selected_probability = float(p_yes) * 100 if str(side).upper() == "YES" else (1.0 - float(p_yes)) * 100
                contracts_count = int(contracts or 0)
                entry = float(entry_cents or 0)
                fee = float(fee_cents or 0)
                slippage = float(slippage_cents or 0)
                research_paper_trades.append({
                    "id": int(trade_id),
                    "opened_at": opened_at,
                    "settled_at": settled_at,
                    "market_ticker": ticker,
                    "title": decision.get("title"),
                    "category": category,
                    "side": side,
                    "contracts": contracts_count,
                    "entry_cents": round(entry, 3),
                    "estimated_fee_cents": round(fee, 3),
                    "estimated_slippage_cents": round(slippage, 3),
                    "estimated_total_entry_cost_cents": round(contracts_count * (entry + fee + slippage), 3),
                    "model_probability_yes_percent": round(float(p_yes) * 100, 3) if p_yes is not None else None,
                    "selected_side_probability_percent": round(selected_probability, 3) if selected_probability is not None else None,
                    "expected_net_edge_cents": round(float(edge), 3) if edge is not None else None,
                    "selection_rank_score": selection.get("rank_score"),
                    "selection_rank_score_is_probability": False,
                    "selection_rank": selection.get("rank"),
                    "selection_status": decision.get("paper_selection_status"),
                    "model_id": model_id,
                    "model_version": model_version,
                    "status": status,
                    "result": outcome,
                    "net_pnl_cents": round(float(pnl), 3) if pnl is not None else None,
                    "pnl_is_final": str(status).lower() == "settled" and outcome in {"YES", "NO"},
                })
            result["research_paper_trades"] = research_paper_trades
            result["open_research_paper_positions"] = [item for item in research_paper_trades if item["status"] == "open"]
            settlement_monitor = market_decision_pipeline.settlement_monitor_status()
            result["research_paper_settlement_monitor"] = settlement_monitor
            result["research_paper_position_note"] = (
                "One simulated research position is allowed open at a time. Entry is recorded at the selected-side ask; "
                "estimated fee and slippage are deducted from final net P&L. The settlement-only monitor checks every "
                f"{settlement_monitor['interval_seconds']} seconds while its scheduler is running, including when the master "
                "autopilot is OFF; it does not call the model/evidence provider. No live order is sent."
            )
            category_rows = conn.execute(
                """SELECT category,status,settled_at,pnl_cents,source_snapshot_json,market_ticker
                   FROM market_research_paper_trades ORDER BY id DESC LIMIT 10000"""
            ).fetchall()
            result["category_performance"] = build_research_category_performance(category_rows)
            result["category_performance_note"] = (
                "Each display group has its own segregated research-paper counts. Empty groups have no invented win rate. "
                "Sports league labels are reporting-only; all opportunities still pass the base sports evidence and calibration gates."
            )
    except Exception:
        result["research_paper_performance"] = {"available": False, "note": "Research-paper statistics are temporarily unavailable."}
    return result


@app.post("/api/market-analysis/run")
def api_run_market_analysis(request: Request) -> dict[str, Any]:
    """Start one bounded authenticated research batch with at most one best-candidate paper entry."""
    _require_admin_session(request)
    # Start the settlement-only monitor as part of this explicit research workflow.
    # It keeps tracking this and later paper positions even after the master autopilot
    # remains OFF; that setting is never changed by this route.
    market_decision_pipeline.start_scheduler(enabled=True)
    # A manual click is one-shot authorization to open at most the best eligible
    # research-paper position for this batch. Environment safety gates still apply.
    return market_decision_pipeline.start(force=True, paper_entry_once=True)


def _clear_market_scan_cache_for_tests() -> None:
    """Reset in-memory scanner and decision state between isolated HTTP tests."""
    market_scanner.reset_for_tests()
    market_decision_pipeline.reset_for_tests()


@app.get("/api/category-readiness")
def category_readiness() -> dict[str, Any]:
    """Read-only roadmap for safe market-category expansion; never enables non-BTC trading."""
    return {
        "mode": "SIMULATION_ONLY",
        "current_active_strategy": "btc_15m",
        "profiles": category_profiles(),
        "classification_examples": [
            classify_market("KXBTC15M-EXAMPLE", "BTC price up in the next 15 minutes?"),
            classify_market("EXAMPLE-SPORTS", "Example sports event", "sports"),
            classify_market("UNKNOWN-EXAMPLE", "Unclassified event"),
        ],
        "safety_note": "This endpoint is informational only. Non-BTC categories are research-only and cannot be routed through the BTC paper strategy.",
    }


@app.get("/markets/btc-candidates")
def btc_candidates() -> dict[str, Any]:
    """List candidate open BTC/Bitcoin markets for manual verification."""
    try:
        return discover()
    except KalshiAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/markets/{ticker}")
def market_details(ticker: str) -> dict[str, Any]:
    """Fetch a specific market's public details; this is not an order route."""
    try:
        return get_market(ticker)
    except KalshiAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/markets/{ticker}/orderbook")
def market_orderbook(ticker: str) -> dict[str, Any]:
    """Fetch a public order book for inspection only."""
    try:
        return get_orderbook(ticker)
    except KalshiAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _record_webhook_event(status_code: int, status: str, reason: str, payload: dict[str, Any] | None = None) -> None:
    """Audit webhook delivery outcomes without ever persisting credentials or message bodies."""
    payload = payload if isinstance(payload, dict) else {}
    symbol = str(payload.get("symbol") or payload.get("ticker") or payload.get("market_ticker") or "")[:100]
    timeframe = str(payload.get("timeframe") or payload.get("interval") or payload.get("resolution") or "")[:40]
    direction = str(payload.get("direction") or payload.get("action") or payload.get("signal") or "")[:40]
    try:
        stamp = datetime.now(timezone.utc).isoformat()
        with db_connect() as conn:
            conn.execute(
                """INSERT INTO webhook_events
                   (received_at, status_code, status, reason, symbol, timeframe, direction)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (stamp, status_code, status, reason[:300], symbol, timeframe, direction),
            )
            record_decision_audit(
                conn, event_type="webhook_event", direction=direction or None, decision=status,
                reason=reason[:300], created_at=stamp,
                payload={"status_code": int(status_code), "symbol": symbol or None, "timeframe": timeframe or None,
                         "message_body_saved": False, "mode": "SIMULATION_ONLY"},
            )
    except Exception:
        # Never mask the actual webhook response if diagnostics logging fails.
        pass


def _normalize_webhook_direction(payload: dict[str, Any]) -> str:
    """Normalize directional alerts and informational WAIT states from TradingView/GainzAlgo."""
    candidates = []
    for key in ("direction", "action", "signal", "side", "alert", "message", "strategy_order_action"):
        value = payload.get(key)
        if value is not None:
            candidates.append(str(value).strip().upper())
    wait_labels = {"WAIT", "WAIT_THIS_WINDOW", "WAIT_WINDOW", "NO_TRADE", "NO_ENTRY", "STAND_ASIDE"}
    for value in candidates:
        normalized = value.replace("-", "_")
        compact = "_".join(normalized.split()).strip(".,:;[](){}\"'")
        # A WAIT is a first-class status event. It is never converted into a trade direction.
        if compact in wait_labels:
            return "WAIT"
        if compact in {"BUY_UP", "LONG_UP", "BULLISH", "BULL", "UP", "BUY", "LONG"}:
            return "UP" if compact in {"BUY_UP", "LONG_UP", "BULLISH", "BULL", "UP"} else "BUY"
        if compact in {"BUY_DOWN", "SHORT_DOWN", "BEARISH", "BEAR", "DOWN", "SELL", "SHORT"}:
            return "DOWN" if compact in {"BUY_DOWN", "SHORT_DOWN", "BEARISH", "BEAR", "DOWN"} else "SELL"
        # Free-text messages may include a timeframe/symbol around the state.
        tokens = {part.strip(".,:;[](){}\"'") for part in normalized.replace("/", " ").replace("-", "_").split()}
        if "WAIT" in tokens and ("WINDOW" in tokens or "THIS" in tokens or len(tokens) == 1):
            return "WAIT"
        if "NO" in tokens and ("TRADE" in tokens or "ENTRY" in tokens):
            return "WAIT"
        if tokens.intersection({"BUY_UP", "LONG_UP", "BULLISH", "BULL", "UP"}):
            return "UP"
        if tokens.intersection({"BUY_DOWN", "SHORT_DOWN", "BEARISH", "BEAR", "DOWN"}):
            return "DOWN"
        if tokens.intersection({"BUY", "LONG"}):
            return "BUY"
        if tokens.intersection({"SELL", "SHORT"}):
            return "SELL"
    return ""


def _webhook_fingerprint(payload: dict[str, Any], direction: str, symbol: str, timeframe: str) -> str | None:
    """Create an idempotency key only when the source supplies a stable event identity."""
    identity = None
    for key in ("alert_id", "alertId", "uuid", "event_id", "timestamp", "bar_time", "time", "timenow"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            identity = f"{key}:{str(value).strip()}"
            break
    if identity is None:
        # Do not collapse distinct legitimate same-direction alerts if TradingView
        # did not send an event timestamp or id.
        return None
    raw = "|".join((identity, direction.upper(), symbol.upper(), timeframe.lower()))
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


@app.post("/webhooks/gainzalgo")
async def gainzalgo_webhook(request: Request) -> dict[str, Any]:
    """Record incoming TradingView/GainzAlgo alerts for display and analysis; no trades."""
    max_body_bytes = _env_int("GAINZALGO_WEBHOOK_MAX_BODY_BYTES", 16384, 1024, 65536)
    content_length = request.headers.get("content-length") if getattr(request, "headers", None) is not None else None
    try:
        if content_length is not None and int(content_length) > max_body_bytes:
            _record_webhook_event(413, "rejected", "Webhook body exceeds the configured size limit.")
            raise HTTPException(status_code=413, detail="Webhook body exceeds the configured size limit.")
        # Real Starlette requests are read with a hard streaming limit. The .json()
        # fallback keeps simple deterministic request stubs usable in offline tests.
        if callable(getattr(request, "stream", None)):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > max_body_bytes:
                    _record_webhook_event(413, "rejected", "Webhook body exceeds the configured size limit.")
                    raise HTTPException(status_code=413, detail="Webhook body exceeds the configured size limit.")
            payload = json.loads(bytes(body))
        else:
            payload = await request.json()
            if len(json.dumps(payload, default=str).encode("utf-8")) > max_body_bytes:
                _record_webhook_event(413, "rejected", "Webhook body exceeds the configured size limit.")
                raise HTTPException(status_code=413, detail="Webhook body exceeds the configured size limit.")
    except HTTPException:
        raise
    except Exception as exc:
        _record_webhook_event(400, "rejected", "Expected a JSON alert body.")
        raise HTTPException(status_code=400, detail="Expected a JSON alert body.") from exc
    if not isinstance(payload, dict):
        _record_webhook_event(400, "rejected", "Expected a JSON object.")
        raise HTTPException(status_code=400, detail="Expected a JSON object.")

    expected_secret = os.getenv("GAINZALGO_WEBHOOK_SECRET", "")
    supplied_secret = str(payload.get("secret", payload.get("token", "")))
    if not expected_secret:
        _record_webhook_event(503, "rejected", "GAINZALGO_WEBHOOK_SECRET is not configured.", payload)
        raise HTTPException(status_code=503, detail="Webhook secret is not configured on the server.")
    if not hmac.compare_digest(supplied_secret.encode("utf-8", errors="replace"), expected_secret.encode("utf-8", errors="replace")):
        _record_webhook_event(401, "rejected", "Webhook secret did not match.", payload)
        raise HTTPException(status_code=401, detail="Invalid webhook secret.")

    direction_text = _normalize_webhook_direction(payload)
    if not direction_text:
        _record_webhook_event(422, "rejected", "Could not identify a recognized direction field.", payload)
        raise HTTPException(status_code=422, detail="Unrecognized direction. Send direction/action/signal as UP, DOWN, BUY, SELL, BUY_UP, BUY_DOWN, BULLISH, BEARISH, WAIT, WAIT_THIS_WINDOW, or NO_TRADE.")

    created_at = datetime.now(timezone.utc).isoformat()
    symbol = str(payload.get("symbol") or payload.get("ticker") or payload.get("market_ticker") or "")[:100]
    timeframe = str(payload.get("timeframe") or payload.get("interval") or payload.get("resolution") or "")[:40]
    # Strip sensitive fields before storing or exposing any alert detail.
    safe_payload = sanitize_sensitive_payload(payload)
    try:
        serialized = json.dumps(safe_payload, ensure_ascii=False, default=str, allow_nan=False)
    except (TypeError, ValueError):
        serialized = "{}"
    if len(serialized) > 10000:
        serialized = json.dumps({"truncated": True, "preview": serialized[:9000]}, ensure_ascii=False)

    fingerprint = _webhook_fingerprint(payload, direction_text, symbol, timeframe)
    duplicate_id = None
    with db_connect() as conn:
        insert_sql = """INSERT INTO gainzalgo_signals
            (created_at, direction, symbol, timeframe, payload_json, fingerprint)
            VALUES (?, ?, ?, ?, ?, ?)"""
        params = (created_at, direction_text, symbol, timeframe, serialized, fingerprint)
        if fingerprint:
            # Atomic conflict handling + a partial unique index prevent concurrent
            # duplicate deliveries from entering the worker queue twice.
            inserted = conn.execute(
                insert_sql + " ON CONFLICT(fingerprint) WHERE fingerprint IS NOT NULL DO NOTHING", params
            )
            if getattr(inserted, "rowcount", 0) == 0:
                existing = conn.execute(
                    "SELECT id FROM gainzalgo_signals WHERE fingerprint = ? LIMIT 1", (fingerprint,)
                ).fetchone()
                duplicate_id = int(existing[0]) if existing else None
        else:
            conn.execute(insert_sql, params)
    if duplicate_id is not None:
        _record_webhook_event(200, "duplicate", f"Duplicate delivery ignored; original alert id {duplicate_id}.", {
            "symbol": symbol, "timeframe": timeframe, "direction": direction_text,
        })
        return {"status": "duplicate_ignored", "source": "TradingView/GainzAlgo", "direction": direction_text,
                "symbol": symbol or None, "timeframe": timeframe or None, "created_at": created_at,
                "original_alert_id": duplicate_id, "mode": "SIMULATION_ONLY", "trade_placed": False}
    _record_webhook_event(200, "accepted", "Alert recorded successfully.", {
        "symbol": symbol, "timeframe": timeframe, "direction": direction_text,
    })
    return {"status": "recorded", "source": "TradingView/GainzAlgo", "direction": direction_text,
            "symbol": symbol or None, "timeframe": timeframe or None, "created_at": created_at,
            "mode": "SIMULATION_ONLY", "trade_placed": False}

