"""Paper-only Kalshi BTC 15m worker (shared DB + TradingView-primary hybrid).

The worker never submits orders. It reads public market data, records simulated
positions, and manages paper exits only. In a separate Render Background Worker,
set DATABASE_URL to the same PostgreSQL database as the web service.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from typing import Any

from candle_feed import fetch_signal_candles, fetch_closed_candles, CandleFeedError
from trend_stop import update_trend_stop
from signal_engine import MarketContext, evaluate_signal
from signal_fusion import evaluate_fusion
from btc_trend_monitor import build_trend_snapshot
from market_discovery import discover
from kalshi_api import get_market, get_orderbook, KalshiAPIError
from db_store import connect_database, ensure_schema, backend_name, record_decision_audit
from payload_safety import sanitize_sensitive_payload
from trade_learning import sync_settled_trade_learning, update_adaptive_entry_policy
from strategy_shadow import process_hold_to_settlement_shadow

log = logging.getLogger("paper_worker")

# In-process candle cache is warmed continuously in idle cycles, so a fresh
# TradingView alert can reuse the latest valid chart assessment immediately.
_LATEST_SIGNAL_BUNDLE: dict[str, Any] | None = None
_LATEST_SIGNAL_BUNDLE_AT: datetime | None = None


def _truthy(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _parse_epoch_or_iso(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        epoch = float(value)
        if epoch > 10_000_000_000:  # TradingView milliseconds
            epoch /= 1000.0
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    return _parse_time(value)


def _num(obj: dict, keys: tuple[str, ...], *, dollars_keys: tuple[str, ...] = ()) -> float | None:
    for key in dollars_keys:
        val = obj.get(key)
        if val is not None:
            try:
                result = float(val) * 100
                return result if math.isfinite(result) else None
            except (ValueError, TypeError, OverflowError):
                pass
    for key in keys:
        val = obj.get(key)
        if val is not None:
            try:
                result = float(val)
                return result if math.isfinite(result) else None
            except (ValueError, TypeError, OverflowError):
                pass
    return None


def _price(m: dict, side: str, kind: str) -> float | None:
    return _num(m, (f"{side}_{kind}",), dollars_keys=(f"{side}_{kind}_dollars",))


def _extract_market(raw: dict, rejection_counts: dict[str, int] | None = None) -> dict | None:
    """Validate a live KXBTC15M contract and optionally count safe rejection reasons."""
    def reject(reason: str) -> None:
        if rejection_counts is not None:
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        return None

    if not isinstance(raw, dict):
        return reject("invalid_market_payload")
    m = raw.get("market", raw)
    if not isinstance(m, dict):
        return reject("invalid_market_payload")
    ticker = m.get("ticker")
    status = str(m.get("status", "")).strip().lower()
    title = " ".join(str(m.get(k, "")) for k in ("title", "subtitle", "event_ticker")).lower()
    # KXBTC15M is Kalshi's dedicated BTC 15-minute up/down series. Its exact
    # series ticker is a stronger identifier than presentation text, which can
    # be absent or renamed by the API; still require a real strike and close time.
    if not ticker or not str(ticker).upper().startswith("KXBTC15M-"):
        return reject("wrong_series_ticker")
    strike = None
    for key in (
        "floor_strike", "floor_strike_dollars", "strike_price", "strike_price_dollars",
        "custom_strike", "custom_strike_dollars", "strike", "target_price", "target_price_dollars",
    ):
        try:
            if m.get(key) is not None:
                strike = float(m[key])
                if math.isfinite(strike) and strike > 0:
                    break
                strike = None
        except (TypeError, ValueError, OverflowError):
            strike = None
    if strike is None:
        return reject("missing_or_invalid_strike")
    close = _parse_time(m.get("close_time") or m.get("expiration_time") or m.get("expected_expiration_time"))
    if close is None:
        return reject("missing_or_invalid_close_time")
    seconds = int((close - _utcnow()).total_seconds())
    if seconds <= 0 or seconds > 900:
        return reject("outside_active_15m_window")
    if status in {"settled", "finalized", "closed", "inactive"}:
        return reject("closed_status")
    return {"raw": m, "ticker": str(ticker), "strike": strike, "seconds": seconds,
            # Summary asks can be inconsistent with a later order-book snapshot.
            # Executable asks are derived from the corresponding current order book.
            "market_summary_yes_ask": _price(m, "yes", "ask"),
            "market_summary_no_ask": _price(m, "no", "ask"),
            "status": status, "title": title}


def _top_bid(book: dict, side: str) -> float | None:
    if not isinstance(book, dict):
        return None
    ob = book.get("orderbook_fp", book.get("orderbook", book))
    if not isinstance(ob, dict):
        return None
    values: list[float] = []
    for key in (side, f"{side}_dollars"):
        levels = ob.get(key)
        if not isinstance(levels, list):
            continue
        for level in levels:
            try:
                price = float(level[0])
                if key.endswith("_dollars"):
                    price *= 100
                if math.isfinite(price) and 0 <= price <= 100:
                    values.append(price)
            except (ValueError, TypeError, IndexError, OverflowError):
                continue
    return max(values) if values else None


def _best_quotes(book: dict) -> dict[str, float | None]:
    """Normalize Kalshi's bid-only binary orderbook into best bid/implied ask prices.

    Kalshi provides bid levels for YES and NO. A YES ask is 100 - best NO bid,
    and a NO ask is 100 - best YES bid. Market-summary ask fields are not used
    for simulated entry pricing because they may be inconsistent with the book.
    """
    yes_bid = _top_bid(book, "yes")
    no_bid = _top_bid(book, "no")
    return {
        "yes_bid": yes_bid,
        "no_bid": no_bid,
        "yes_ask": (100.0 - no_bid) if no_bid is not None else None,
        "no_ask": (100.0 - yes_bid) if yes_bid is not None else None,
    }


def _connect():
    conn = connect_database()
    ensure_schema(conn)
    return conn


def _bundle_cache_age_seconds() -> float | None:
    if _LATEST_SIGNAL_BUNDLE is None or _LATEST_SIGNAL_BUNDLE_AT is None:
        return None
    return max(0.0, (_utcnow() - _LATEST_SIGNAL_BUNDLE_AT).total_seconds())


def _get_signal_bundle_for_evaluation() -> dict[str, Any]:
    """Return a recently pre-fetched candle bundle, otherwise refresh it now."""
    global _LATEST_SIGNAL_BUNDLE, _LATEST_SIGNAL_BUNDLE_AT
    max_age = max(15.0, min(180.0, _safe_float(os.getenv("BTC_TREND_MONITOR_MAX_CACHE_AGE_SECONDS", "45"), 45.0)))
    age = _bundle_cache_age_seconds()
    if _LATEST_SIGNAL_BUNDLE is not None and age is not None and age <= max_age:
        return _LATEST_SIGNAL_BUNDLE
    bundle = fetch_signal_candles()
    _LATEST_SIGNAL_BUNDLE = bundle
    _LATEST_SIGNAL_BUNDLE_AT = _utcnow()
    return bundle


def _refresh_btc_trend_monitor(conn) -> dict[str, Any] | None:
    """Continuously refresh and persist chart-only trend context. Never trades."""
    global _LATEST_SIGNAL_BUNDLE, _LATEST_SIGNAL_BUNDLE_AT
    interval = max(15.0, min(300.0, _safe_float(os.getenv("BTC_TREND_MONITOR_INTERVAL_SECONDS", "30"), 30.0)))
    row = conn.execute(
        "SELECT updated_at, snapshot_json FROM btc_trend_monitor WHERE monitor_key=? LIMIT 1",
        ("btc_15m",),
    ).fetchone()
    if row:
        previous_at = _parse_time(row[0])
        cache_age = _bundle_cache_age_seconds()
        cache_is_warm = (_LATEST_SIGNAL_BUNDLE is not None and cache_age is not None and
                         cache_age <= max(15.0, min(180.0, _safe_float(os.getenv("BTC_TREND_MONITOR_MAX_CACHE_AGE_SECONDS", "45"), 45.0))))
        # After a worker restart, a persisted snapshot does not contain the actual
        # candles. Fetch once to warm the in-memory cache even if the DB row is recent.
        if previous_at is not None and (_utcnow() - previous_at).total_seconds() < interval and cache_is_warm:
            try:
                return json.loads(row[1]) if row[1] else None
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    try:
        bundle = fetch_signal_candles()
        _LATEST_SIGNAL_BUNDLE = bundle
        _LATEST_SIGNAL_BUNDLE_AT = _utcnow()
        snapshot = build_trend_snapshot(bundle, now=_LATEST_SIGNAL_BUNDLE_AT)
        snapshot["cache_age_seconds"] = 0
        conn.execute(
            """INSERT INTO btc_trend_monitor (monitor_key, updated_at, status, snapshot_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(monitor_key) DO UPDATE SET updated_at=excluded.updated_at,
                 status=excluded.status, snapshot_json=excluded.snapshot_json""",
            ("btc_15m", snapshot["updated_at"], snapshot["status"], json.dumps(snapshot, separators=(",", ":"))),
        )
        conn.commit()
        return snapshot
    except Exception as exc:
        # Monitoring failure must never crash the worker or be interpreted as a BUY.
        note = f"{type(exc).__name__}: {str(exc)[:240]}"
        snapshot = {"status": "degraded", "monitor_only": True, "updated_at": _utcnow().isoformat(),
                    "provider": "Binance public REST", "symbol": "BTCUSDT", "error": note,
                    "disclaimer": "Trend monitor unavailable; the worker still requires fresh inputs and all trade checks."}
        try:
            conn.execute(
                """INSERT INTO btc_trend_monitor (monitor_key, updated_at, status, snapshot_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(monitor_key) DO UPDATE SET updated_at=excluded.updated_at,
                     status=excluded.status, snapshot_json=excluded.snapshot_json""",
                ("btc_15m", snapshot["updated_at"], snapshot["status"], json.dumps(snapshot, separators=(",", ":"))),
            )
            conn.commit()
        except Exception as store_exc:
            log.warning("Could not store BTC trend monitor state: %s", store_exc)
        log.warning("BTC trend monitor unavailable; no trade decision inferred: %s", note)
        return snapshot


def _limits(conn, max_trade_dollars: float, max_daily_loss_dollars: float):
    today = _utcnow().date().isoformat()
    realized = float(conn.execute(
        "SELECT COALESCE(SUM(pnl_cents),0) FROM paper_trades WHERE settled_at IS NOT NULL AND substr(settled_at,1,10)=?",
        (today,),
    ).fetchone()[0] or 0)
    open_cost = float(conn.execute(
        "SELECT COALESCE(SUM(contracts*(entry_cents+fee_cents_per_contract)),0) FROM paper_trades WHERE settled_at IS NULL"
    ).fetchone()[0] or 0)
    count = int(conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE substr(created_at,1,10)=?", (today,)
    ).fetchone()[0] or 0)
    halted = realized - open_cost <= -max_daily_loss_dollars * 100
    return realized, open_cost, count, halted


def _take_profit_target_cents(entry: float, entry_fee: float, exit_fee: float, target_return_percent: float) -> float:
    """Return a fee-aware take-profit bid target inside Kalshi's 0-100c contract range.

    When the requested net ROI is impossible at the entered price (the raw target
    exceeds 100c), cap the target at 100c rather than persisting an unreachable
    target that can never activate. Callers log this cap so the smaller achievable
    return is visible. This never implies that a bid at 100c is guaranteed to fill.
    """
    if not 0 < float(entry) < 100:
        raise ValueError("take-profit entry price must be between 0 and 100 cents")
    if float(entry_fee) < 0 or float(exit_fee) < 0:
        raise ValueError("take-profit fees cannot be negative")
    if not 0 < float(target_return_percent) <= 200:
        raise ValueError("PAPER_TAKE_PROFIT_PERCENT must be greater than 0 and at most 200")
    entry_cost = float(entry) + float(entry_fee)
    raw_target = round(float(entry) + float(entry_fee) + float(exit_fee) + entry_cost * float(target_return_percent) / 100, 2)
    return min(100.0, raw_target)


def _loss_streak_cooldown_status(
    settled_rows, *, limit: int, cooldown_minutes: int, now: datetime | None = None,
) -> dict[str, Any] | None:
    """Pause new paper entries after a configurable consecutive net-loss streak.

    settled_rows must be ordered newest first and contain (settled_at, pnl_cents).
    This guard only pauses new entries; open-position management still runs first.
    """
    if limit <= 0 or cooldown_minutes <= 0:
        return None
    rows = list(settled_rows or [])[:limit]
    if len(rows) < limit:
        return None
    streak = 0
    for settled_at, pnl_cents in rows:
        try:
            pnl = float(pnl_cents)
        except (TypeError, ValueError, OverflowError):
            break
        if not math.isfinite(pnl) or pnl >= 0:
            break
        streak += 1
    if streak < limit:
        return None
    last_settled = _parse_time(rows[0][0])
    if last_settled is None:
        return None
    current = now or _utcnow()
    elapsed_seconds = max(0.0, (current - last_settled).total_seconds())
    remaining_seconds = cooldown_minutes * 60 - elapsed_seconds
    if remaining_seconds <= 0:
        return None
    until = current + timedelta(seconds=remaining_seconds)
    return {
        "blocked": True,
        "consecutive_losses": streak,
        "cooldown_minutes": int(cooldown_minutes),
        "seconds_remaining": int(remaining_seconds),
        "cooldown_until": until.isoformat(),
        "reason": f"paper entry circuit breaker: {streak} consecutive net-loss trades; pausing new entries for {int(remaining_seconds // 60) + 1} more minute(s)",
    }


def _take_profit_trailing_floor(target_cents: float, peak_bid_cents: float, trail_percent: float) -> float:
    if not 0 < float(target_cents) <= 100:
        raise ValueError("take-profit target must be between 0 and 100 cents")
    if not float(target_cents) <= float(peak_bid_cents) <= 100:
        raise ValueError("take-profit peak bid must be between target and 100 cents")
    if not 0 < float(trail_percent) <= 50:
        raise ValueError("PAPER_TAKE_PROFIT_TRAIL_PERCENT must be greater than 0 and at most 50")
    return round(max(float(target_cents), float(peak_bid_cents) * (1 - float(trail_percent) / 100)), 2)



def _major_adverse_btc_move(candles, direction: str, atr_multiplier: float = 2.5, atr_period: int = 14) -> tuple[bool, str]:
    """Detect a large adverse move on the latest fully closed 1m candle.

    Uses the prior candles' ATR as a volatility baseline and deliberately
    excludes the latest candle from that baseline to avoid diluting a shock.
    This is a warning/exit trigger, not a prediction or fill guarantee.
    """
    if str(direction).upper() not in {"UP", "DOWN"}:
        return False, "invalid trade direction"
    if not math.isfinite(float(atr_multiplier)) or float(atr_multiplier) <= 0:
        return False, "invalid ATR multiplier"
    required = {"open", "high", "low", "close"}
    if candles is None or not required.issubset(set(getattr(candles, "columns", []))):
        return False, "candle data missing required OHLC columns"
    if len(candles) < max(atr_period + 2, 20):
        return False, "not enough closed candles for a major-move check"
    try:
        frame = candles.copy()
        for col in ("open", "high", "low", "close"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame = frame.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
        if len(frame) < max(atr_period + 2, 20):
            return False, "not enough valid closed candles for a major-move check"
        previous_close = float(frame["close"].iloc[-2])
        current_close = float(frame["close"].iloc[-1])
        current_open = float(frame["open"].iloc[-1])
        previous_closes = frame["close"].shift(1)
        true_range = pd.concat([
            frame["high"] - frame["low"],
            (frame["high"] - previous_closes).abs(),
            (frame["low"] - previous_closes).abs(),
        ], axis=1).max(axis=1)
        baseline = true_range.iloc[-(atr_period + 1):-1].dropna()
        atr = float(baseline.mean()) if len(baseline) else 0.0
        if not math.isfinite(atr) or atr <= 0:
            return False, "ATR baseline unavailable"
        if str(direction).upper() == "UP":
            adverse_move = max(previous_close - current_close, current_open - current_close, 0.0)
            label = "sharp BTC drop against UP position"
        else:
            adverse_move = max(current_close - previous_close, current_close - current_open, 0.0)
            label = "sharp BTC rally against DOWN position"
        threshold = atr * float(atr_multiplier)
        if adverse_move >= threshold:
            return True, f"{label}: adverse move {adverse_move:.2f} exceeded {atr_multiplier:.2f}x prior ATR ({threshold:.2f})"
        return False, f"no major adverse BTC move: {adverse_move:.2f} below {atr_multiplier:.2f}x prior ATR ({threshold:.2f})"
    except (ValueError, TypeError, IndexError, KeyError, OverflowError):
        return False, "major-move check unavailable due to invalid candle data"

def _safe_external_payload(raw_payload: str) -> dict[str, Any]:
    try:
        body = json.loads(raw_payload)
    except (TypeError, ValueError):
        return {}
    if not isinstance(body, dict):
        return {}
    sanitized = sanitize_sensitive_payload(body)
    return sanitized if isinstance(sanitized, dict) else {}


def _next_gainzalgo_signal(conn):
    """Get the oldest valid queued alert; stale/invalid alerts are marked, never traded."""
    max_age = max(30, min(900, int(os.getenv("TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS", os.getenv("GAINZALGO_MAX_SIGNAL_AGE_SECONDS", "300")))))
    now = _utcnow()
    rows = conn.execute(
        "SELECT id,created_at,direction,symbol,timeframe,payload_json,processing_status FROM gainzalgo_signals WHERE processed_at IS NULL ORDER BY id ASC LIMIT 30"
    ).fetchall()
    for row in rows:
        signal_id, created_at, raw_direction, symbol, timeframe, payload_json, processing_status = row
        received = _parse_time(created_at)
        age = (now - received).total_seconds() if received else float("inf")
        body = _safe_external_payload(payload_json)
        # If TradingView provides the originating bar timestamp, reject old bars too.
        source_ts = next((body.get(k) for k in ("bar_time", "timestamp", "alert_time", "time", "created_at") if body.get(k) is not None), None)
        parsed_source_ts = _parse_epoch_or_iso(source_ts)
        if parsed_source_ts is not None:
            source_age = (now - parsed_source_ts).total_seconds()
            if source_age < -10 or source_age > max_age:
                _mark_gainzalgo(conn, signal_id, "ignored", f"source alert timestamp is stale/invalid; age_seconds={source_age:.1f}")
                continue
        if age < 0 or age > max_age:
            _mark_gainzalgo(conn, signal_id, "ignored", f"received alert is stale/invalid; age_seconds={age:.1f}")
            continue
        symbol_text = str(symbol or "").upper()
        if not symbol_text or ("BTC" not in symbol_text and "BITCOIN" not in symbol_text):
            _mark_gainzalgo(conn, signal_id, "ignored", "symbol missing or not BTC")
            continue
        timeframe_text = str(timeframe or "").lower().replace(" ", "")
        if timeframe_text and timeframe_text not in {"1", "1m", "1min", "1minute", "1minutes", "15", "15m", "15min", "15minute", "15minutes"}:
            _mark_gainzalgo(conn, signal_id, "ignored", "unsupported timeframe; expected 1m or 15m")
            continue
        d = str(raw_direction or "").strip().upper().replace("-", "_")
        if d in {"WAIT", "WAIT_THIS_WINDOW", "WAIT_WINDOW", "NO_TRADE", "NO_ENTRY", "STAND_ASIDE"}:
            direction = "WAIT"
        elif d in {"BUY_UP", "UP", "LONG_UP", "BULLISH", "BULL", "UPWARD"}:
            direction = "UP"
        elif d in {"BUY_DOWN", "DOWN", "SHORT_DOWN", "BEARISH", "BEAR", "DOWNWARD"}:
            direction = "DOWN"
        elif d in {"BUY", "LONG"}:
            # Compatible with the user's existing TradingView config: BUY means upward.
            direction = "UP"
        elif d in {"SELL", "SHORT"}:
            direction = "DOWN"
        else:
            _mark_gainzalgo(conn, signal_id, "ignored", "unrecognized direction")
            continue
        if not timeframe_text:
            log.warning("Alert %s did not include timeframe; verify chart/alert configuration", signal_id)
        return {"id": signal_id, "created_at": created_at, "direction": direction,
                "symbol": symbol, "timeframe": timeframe_text or None, "payload_json": payload_json,
                "processing_status": processing_status or "queued", "age_seconds": round(age, 2)}, direction
    return None, None


def _audit_alert_transition(conn, signal_id: int, status: str, note: str) -> None:
    """Persist every accepted/skipped/ignored/pending alert status transition."""
    try:
        row = conn.execute(
            "SELECT created_at,direction,symbol,timeframe,payload_json FROM gainzalgo_signals WHERE id=? LIMIT 1",
            (signal_id,),
        ).fetchone()
        if not row:
            return
        created_at, direction, symbol, timeframe, raw_payload = row
        safe_payload = _safe_external_payload(raw_payload or "{}")
        record_decision_audit(
            conn, event_type="alert_status", alert_id=int(signal_id), direction=str(direction or "").upper() or None,
            decision=status, reason=note, created_at=_utcnow().isoformat(),
            payload={"alert_created_at": created_at, "symbol": symbol, "timeframe": timeframe,
                     "payload_keys": sorted(safe_payload.keys())},
        )
        conn.commit()
    except Exception as exc:
        log.warning("Could not write alert audit event for %s: %s", signal_id, exc)


def _mark_gainzalgo(conn, signal_id: int, status: str, note: str) -> None:
    cur = conn.execute(
        "UPDATE gainzalgo_signals SET processed_at=?,processing_status=?,processing_note=? WHERE id=? AND processed_at IS NULL",
        (_utcnow().isoformat(), status, str(note)[:500], signal_id),
    )
    conn.commit()
    if getattr(cur, "rowcount", 0):
        _audit_alert_transition(conn, signal_id, status, note)


def _decision_fields_from_payload(direction: str, payload: dict[str, Any], decision_time: str | None = None) -> dict[str, Any]:
    external = payload.get("external_signal") if isinstance(payload.get("external_signal"), dict) else {}
    fusion = payload.get("fusion") if isinstance(payload.get("fusion"), dict) else {}
    trend = payload.get("chart_trend_at_decision") if isinstance(payload.get("chart_trend_at_decision"), dict) else {}
    proposed = str(external.get("direction") or payload.get("proposed_direction") or direction or "").strip().upper()
    if proposed in {"BUY_UP", "LONG_UP", "BULLISH"}: proposed = "UP"
    elif proposed in {"BUY_DOWN", "SHORT_DOWN", "BEARISH"}: proposed = "DOWN"
    if proposed not in {"UP", "DOWN"}:
        proposed = None
    entry_ask = fusion.get("entry_ask_cents", payload.get("entry_ask_cents"))
    spread = fusion.get("spread_cents", payload.get("spread_cents"))
    bias = trend.get("candidate_bias") or trend.get("trend_bias")
    evidence = trend.get("evidence_score")
    try: entry_ask = float(entry_ask) if entry_ask is not None and math.isfinite(float(entry_ask)) else None
    except (ValueError, TypeError, OverflowError): entry_ask = None
    try: spread = float(spread) if spread is not None and math.isfinite(float(spread)) else None
    except (ValueError, TypeError, OverflowError): spread = None
    try: evidence = int(float(evidence)) if evidence is not None and math.isfinite(float(evidence)) else None
    except (ValueError, TypeError, OverflowError): evidence = None
    try:
        trend_age = trend.get("age_seconds", trend.get("trend_snapshot_age_seconds"))
        trend_age = float(trend_age) if trend_age is not None and math.isfinite(float(trend_age)) else None
    except (ValueError, TypeError, OverflowError): trend_age = None
    if trend_age is None:
        snapshot_at = _parse_time(trend.get("updated_at"))
        decision_at = _parse_time(decision_time)
        if snapshot_at is not None and decision_at is not None:
            trend_age = max(0.0, (decision_at - snapshot_at).total_seconds())
    if payload.get("decision_status"):
        decision = str(payload["decision_status"])
    elif fusion:
        if bool(fusion.get("approved")): decision = "approved"
        elif bool(fusion.get("vetoed_by_internal")): decision = "vetoed"
        else: decision = "skipped"
    elif str(direction).upper() == "WAIT":
        decision = str(payload.get("entry_price_gate", {}).get("decision") or "wait")
    else:
        decision = "candidate"
    reasons = payload.get("reasons") if isinstance(payload.get("reasons"), list) else []
    fusion_reasons = fusion.get("reasons") if isinstance(fusion.get("reasons"), list) else []
    reason = str(payload.get("final_decision_reason") or "; ".join(fusion_reasons) or "; ".join(str(x) for x in reasons[:8]))
    alert_id = external.get("id")
    try: alert_id = int(alert_id) if alert_id is not None else None
    except (ValueError, TypeError, OverflowError): alert_id = None
    return {"external_signal_id": alert_id, "decision_status": decision, "proposed_direction": proposed,
            "entry_ask_cents": entry_ask, "spread_cents": spread, "trend_bias": str(bias).upper() if bias else None,
            "trend_evidence_score": evidence, "trend_snapshot_age_seconds": trend_age, "decision_reason": reason}


def _optional_positive_price(value: Any) -> float | None:
    """Return a positive finite reference price, or None when it is unavailable."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _optional_nonnegative_seconds(value: Any) -> int | None:
    """Normalize known remaining time; preserve unknown time as NULL instead of zero."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return int(number) if math.isfinite(number) and number >= 0 else None


def _record_signal_log(conn, *, created_at: str, ticker: str | None, direction: str, score: int,
                       btc_price: float | None, strike: float | None, seconds_remaining: int | None,
                       max_entry_cents: float | None, payload: dict[str, Any]) -> int | None:
    """Persist an evaluation, preserving unavailable market values as SQL NULL."""
    fields = _decision_fields_from_payload(direction, payload, created_at)
    btc_value = _optional_positive_price(btc_price)
    strike_value = _optional_positive_price(strike)
    seconds_value = _optional_nonnegative_seconds(seconds_remaining)
    encoded = json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False)
    cur = conn.execute(
        """INSERT INTO signal_log (created_at,market_ticker,direction,score,btc_price,strike_price,
           seconds_remaining,max_entry_cents,payload_json,external_signal_id,decision_status,proposed_direction,
           entry_ask_cents,spread_cents,trend_bias,trend_evidence_score,trend_snapshot_age_seconds)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (created_at, ticker, direction, int(score), btc_value, strike_value, seconds_value,
         max_entry_cents, encoded, fields["external_signal_id"], fields["decision_status"], fields["proposed_direction"],
         fields["entry_ask_cents"], fields["spread_cents"], fields["trend_bias"], fields["trend_evidence_score"],
         fields["trend_snapshot_age_seconds"]),
    )
    conn.commit()
    signal_log_id = cur.lastrowid
    try:
        record_decision_audit(
            conn, event_type="signal_evaluation", alert_id=fields["external_signal_id"],
            signal_log_id=signal_log_id, market_ticker=ticker, direction=fields["proposed_direction"] or direction,
            decision=fields["decision_status"], reason=fields["decision_reason"],
            entry_ask_cents=fields["entry_ask_cents"], spread_cents=fields["spread_cents"],
            trend_bias=fields["trend_bias"], trend_evidence_score=fields["trend_evidence_score"],
            trend_snapshot_age_seconds=fields["trend_snapshot_age_seconds"], internal_score=int(score),
            created_at=created_at,
            payload={"logged_direction": direction, "external_signal_id": fields["external_signal_id"],
                     "decision_status": fields["decision_status"], "reasons": payload.get("reasons", [])[:10],
                     "fusion": payload.get("fusion", {}), "chart_trend_at_decision": payload.get("chart_trend_at_decision", {})},
        )
        conn.commit()
    except Exception as exc:
        log.warning("Could not write signal audit event: %s", exc)
    return signal_log_id


def _update_signal_decision_status(conn, signal_log_id: int | None, status: str, reason: str) -> None:
    """Update an evaluation when a later risk check changes the final action."""
    if signal_log_id is None:
        return
    try:
        row = conn.execute(
            "SELECT market_ticker,proposed_direction,entry_ask_cents,spread_cents,trend_bias,trend_evidence_score,trend_snapshot_age_seconds,score,external_signal_id FROM signal_log WHERE id=? LIMIT 1",
            (signal_log_id,),
        ).fetchone()
        if not row:
            return
        ticker, direction, ask, spread, bias, trend_score, trend_age, internal_score, alert_id = row
        conn.execute("UPDATE signal_log SET decision_status=? WHERE id=?", (status, signal_log_id))
        record_decision_audit(
            conn, event_type="decision_status_update", alert_id=alert_id, signal_log_id=signal_log_id,
            market_ticker=ticker, direction=direction, decision=status, reason=reason,
            entry_ask_cents=ask, spread_cents=spread, trend_bias=bias, trend_evidence_score=trend_score,
            trend_snapshot_age_seconds=trend_age, internal_score=internal_score,
            payload={"updated_from_later_guard": True},
        )
        conn.commit()
    except Exception as exc:
        log.warning("Could not update decision audit status for signal %s: %s", signal_log_id, exc)


def _log_wait(conn, reason: str, *, ticker: str | None = None, external: dict | None = None,
              internal_payload: dict | None = None) -> None:
    """Keep a visible audit entry when an alert is received but no trade is opened."""
    external = external or {}
    internal_payload = internal_payload or {}
    # Missing data stays missing. Do not invent a $0.01 BTC price, copy the
    # strike into the BTC field, or show unknown time remaining as zero.
    btc = _optional_positive_price(internal_payload.get("btc_price"))
    strike = _optional_positive_price(internal_payload.get("strike_price"))
    seconds = _optional_nonnegative_seconds(internal_payload.get("seconds_remaining"))
    score = int(internal_payload.get("score") or 0)
    payload = {
        **internal_payload,
        "btc_price": btc, "strike_price": strike, "seconds_remaining": seconds,
        "direction": "WAIT", "score": score,
        "source": "hybrid" if external else internal_payload.get("source", "internal"),
        "external_signal": {k: external.get(k) for k in ("id", "created_at", "direction", "symbol", "timeframe", "age_seconds")},
        "reasons": list(internal_payload.get("reasons", [])) + [reason],
        "final_decision_reason": reason,
        "disclaimer": "WAIT means no paper entry was opened; evidence score is not a win probability.",
    }
    try:
        _record_signal_log(conn, created_at=_utcnow().isoformat(), ticker=ticker, direction="WAIT", score=score,
                           btc_price=btc, strike=strike, seconds_remaining=seconds,
                           max_entry_cents=_entry_price_limits()[1], payload=payload)
    except (ValueError, TypeError) as exc:
        log.warning("Could not journal wait decision: %s", exc)


def _quarter_hour_window_start(value: datetime) -> datetime:
    """Return the UTC start of the 15-minute window containing value."""
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    utc = value.astimezone(timezone.utc)
    return utc.replace(minute=(utc.minute // 15) * 15, second=0, microsecond=0)


def _entry_price_limits() -> tuple[float, float]:
    """Return (hard entry cap, preferred soft target) in cents.

    PAPER_MAX_ENTRY_CENTS is the only fixed price ceiling. The default 80c
    PAPER_ENTRY_TARGET_MAX_CENTS is a soft reference for reporting and learning;
    it must not block an otherwise approved directional signal. The learning
    policy may tighten that preference, but can never relax the hard cap.
    """
    hard_cap = max(1.0, min(99.0, _safe_float(os.getenv("PAPER_MAX_ENTRY_CENTS", "92"), 92)))
    target = max(1.0, min(99.0, _safe_float(os.getenv("PAPER_ENTRY_TARGET_MAX_CENTS", "80"), 80)))
    return hard_cap, min(hard_cap, target)


def _entry_price_gate(current_ask_cents: float, hard_cap_cents: float, target_cap_cents: float) -> tuple[str, str]:
    """Allow any valid ask up to the hard cap; target_cap is advisory only.

    A price above the preferred target can still enter when the signal direction,
    major-flaw checks, quote/spread checks, signal freshness and risk guards pass.
    The 92c default hard cap stays in force because direction alone is not a
    calibrated probability or a guarantee of positive expected value.
    """
    try:
        ask = float(current_ask_cents)
        hard_cap = float(hard_cap_cents)
        target = min(hard_cap, float(target_cap_cents))
    except (TypeError, ValueError, OverflowError):
        return "invalid", "Entry price or configured limit is invalid."
    if (not all(math.isfinite(v) for v in (ask, hard_cap, target))
            or not (0 < ask < 100) or not (0 < hard_cap < 100) or not (0 < target < 100)):
        return "invalid", "Current order-book ask or configured price limit is invalid; no entry."
    if ask > hard_cap:
        return "wait", f"Current ask {ask:.2f}c exceeds the hard entry cap {hard_cap:.2f}c; no entry at this quote. It may be reconsidered only while the alert remains fresh and in the same 15-minute window."
    if ask > target:
        return "enter", (f"Current ask {ask:.2f}c is above the preferred target {target:.2f}c but remains within the hard cap {hard_cap:.2f}c. "
                            "Price alone will not block an otherwise approved direction; expensive contracts leave less upside and are not evidence of positive expectancy.")
    return "enter", f"Current ask {ask:.2f}c is at or below the preferred target {target:.2f}c and hard cap {hard_cap:.2f}c."


def _set_gainzalgo_pending(conn, signal_id: int, status: str, note: str) -> None:
    """Record a pending status without setting processed_at, so a fresh alert can retry."""
    cur = conn.execute(
        "UPDATE gainzalgo_signals SET processing_status=?,processing_note=? WHERE id=? AND processed_at IS NULL",
        (str(status)[:80], str(note)[:500], signal_id),
    )
    conn.commit()
    if getattr(cur, "rowcount", 0):
        _audit_alert_transition(conn, signal_id, status, note)


def _resolve_counterfactual_signal_outcomes(conn, *, max_checks: int = 5) -> int:
    """Settle one-contract, hold-to-settlement counterfactuals for rejected decisions.

    This is evaluation-only: it never creates, closes, or modifies paper positions.
    Results include entry fee and explicitly omit stop/exit behavior so they remain
    labeled counterfactual rather than actual strategy P&L.
    """
    now = _utcnow()
    candidates = conn.execute(
        """SELECT id,created_at,market_ticker,proposed_direction,entry_ask_cents,seconds_remaining,
                  decision_status,counterfactual_last_checked_at,external_signal_id,trend_bias,trend_evidence_score
           FROM signal_log
           WHERE counterfactual_settled_at IS NULL
             AND upper(market_ticker) LIKE 'KXBTC15M-%'
             AND proposed_direction IN ('UP','DOWN')
             AND entry_ask_cents > 0 AND entry_ask_cents < 100
             AND decision_status IN ('vetoed','skipped','wait_for_better_price','wait','blocked','ignored')
             AND NOT EXISTS (SELECT 1 FROM paper_trades p WHERE p.entry_signal_log_id=signal_log.id)
           ORDER BY CASE WHEN counterfactual_last_checked_at IS NULL THEN 0 ELSE 1 END,
                    counterfactual_last_checked_at ASC, id ASC LIMIT 100"""
    ).fetchall()
    checked = 0
    fee = max(0.0, _safe_float(os.getenv("PAPER_FEE_CENTS_PER_CONTRACT", "2.0"), 2.0))
    for row in candidates:
        if checked >= max(0, min(20, int(max_checks))):
            break
        (signal_id, created_at, ticker, side, ask, seconds_remaining, decision_status,
         last_checked, alert_id, trend_bias, trend_score) = row
        created = _parse_time(created_at)
        if created is None or not ticker or seconds_remaining is None:
            continue
        # Wait until this contract should have closed plus a short result-publication buffer.
        if (now - created).total_seconds() < max(0, float(seconds_remaining)) + 45:
            continue
        if last_checked:
            last = _parse_time(last_checked)
            if last is not None and (now - last).total_seconds() < 120:
                continue
        checked += 1
        note = "Market is not finally settled yet; will retry later."
        try:
            market_payload = get_market(str(ticker))
            market = market_payload.get("market", market_payload) if isinstance(market_payload, dict) else {}
            market_status = str(market.get("status", "")).lower() if isinstance(market, dict) else ""
            result = str(market.get("result", "")).lower() if isinstance(market, dict) else ""
            if market_status in {"settled", "finalized"} and result in {"yes", "no"}:
                outcome = "UP" if result == "yes" else "DOWN"
                pnl = (100.0 if str(side).upper() == outcome else 0.0) - float(ask) - fee
                settled_at = now.isoformat()
                conn.execute(
                    """UPDATE signal_log SET counterfactual_outcome=?,counterfactual_pnl_cents=?,
                       counterfactual_settled_at=?,counterfactual_last_checked_at=?,counterfactual_check_note=? WHERE id=?""",
                    (outcome, pnl, settled_at, settled_at,
                     "Hypothetical 1-contract hold-to-settlement; entry ask and entry fee included; excludes exits/stops.", signal_id),
                )
                record_decision_audit(
                    conn, event_type="counterfactual_settled", alert_id=alert_id, signal_log_id=signal_id,
                    market_ticker=str(ticker), direction=str(side), decision="counterfactual_win" if pnl > 0 else "counterfactual_loss" if pnl < 0 else "counterfactual_break_even",
                    reason="Rejected/no-entry signal evaluated against official settlement; not an actual trade.",
                    entry_ask_cents=float(ask), trend_bias=trend_bias, trend_evidence_score=trend_score,
                    created_at=settled_at, payload={"official_outcome": outcome, "hypothetical_pnl_cents": round(pnl, 4),
                                                    "decision_status": decision_status, "method": "one contract held to official settlement, entry fee included; no exit/stop fees"},
                )
                conn.commit()
                log.info("Counterfactual for signal_log_id=%s settled outcome=%s hypothetical_pnl=%.2fc; no trade modified", signal_id, outcome, pnl)
                continue
            note = f"Official result not available yet (status={market_status or 'unknown'}; result={result or 'unknown'})."
        except Exception as exc:
            note = f"Counterfactual lookup failed: {type(exc).__name__}: {str(exc)[:180]}"
        try:
            conn.execute(
                "UPDATE signal_log SET counterfactual_last_checked_at=?,counterfactual_check_note=? WHERE id=?",
                (now.isoformat(), note, signal_id),
            )
            conn.commit()
        except Exception as store_exc:
            log.warning("Could not persist counterfactual check status for %s: %s", signal_id, store_exc)
    return checked


def _safe_float(value: Any, default: float) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _limits_or_defaults():
    return (
        max(0.01, _safe_float(os.getenv("MAX_TRADE_DOLLARS", "1.00"), 1.00)),
        max(0.01, _safe_float(os.getenv("MAX_DAILY_LOSS_DOLLARS", "10.00"), 10.00)),
    )


def _manage_open(conn) -> None:
    """Monitor open paper positions; observed bids are simulated, not guaranteed fills."""
    rows = conn.execute(
        """SELECT id,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,stop_loss_cents,
           entry_btc_price,high_water_btc,low_water_btc,trailing_stop_btc,last_stop_candle_timestamp,last_trend_check_at,
           trend_stop_shadow_triggered_at,take_profit_target_cents,take_profit_activated_at,take_profit_peak_bid_cents,
           take_profit_last_reversal_candle_timestamp FROM paper_trades WHERE settled_at IS NULL ORDER BY id"""
    ).fetchall()
    trend_enabled = _truthy("PAPER_TREND_STOP_LOSS_ENABLED", False)
    shadow_enabled = _truthy("PAPER_TREND_STOP_SHADOW_MODE", False)
    fixed_stop_enabled = _truthy("PAPER_STOP_LOSS_ENABLED", False)
    tp_enabled = _truthy("PAPER_TAKE_PROFIT_ENABLED", False)
    tp_pct = _safe_float(os.getenv("PAPER_TAKE_PROFIT_PERCENT", "20"), 20)
    trail_pct = _safe_float(os.getenv("PAPER_TAKE_PROFIT_TRAIL_PERCENT", "5"), 5)
    reversal_enabled = _truthy("PAPER_TAKE_PROFIT_REVERSAL_EXIT_ENABLED", True)
    profit_protection_enabled = _truthy("PAPER_TAKE_PROFIT_PROFIT_PROTECTION_ENABLED", False)
    major_move_atr_multiplier = _safe_float(os.getenv("PAPER_TAKE_PROFIT_MAJOR_MOVE_ATR_MULTIPLIER", "2.5"), 2.5)
    exit_fee = max(0.0, _safe_float(os.getenv("PAPER_EXIT_FEE_CENTS_PER_CONTRACT", "2.0"), 2.0))
    check_seconds = max(30.0, min(180.0, _safe_float(os.getenv("PAPER_TREND_STOP_CHECK_SECONDS", "45"), 45)))
    atr_mult = _safe_float(os.getenv("PAPER_TREND_STOP_ATR_MULTIPLIER", "2.5"), 2.5)
    lookback = max(8, min(100, int(_safe_float(os.getenv("PAPER_TREND_STOP_SWING_LOOKBACK", "30"), 30))))

    for row in rows:
        (trade_id, ticker, direction, contracts, entry, entry_fee, static_stop,
         entry_btc, high_water, low_water, trailing_stop, last_candle_ts, last_check_at,
         shadow_triggered_at, stored_tp_target, tp_activated_at, stored_tp_peak,
         tp_last_reversal_ts) = row
        try:
            market_payload = get_market(str(ticker))
            market = market_payload.get("market", market_payload)
            market_status = str(market.get("status", "")).lower() if isinstance(market, dict) else ""
            result = str(market.get("result", "")).lower() if isinstance(market, dict) else ""
            if market_status in {"settled", "finalized"} and result in {"yes", "no"}:
                outcome = "UP" if result == "yes" else "DOWN"
                gross = 100 * int(contracts) if direction == outcome else 0
                pnl = gross - float(entry) * int(contracts) - float(entry_fee) * int(contracts)
                conn.execute(
                    "UPDATE paper_trades SET settled_at=?,outcome=?,pnl_cents=?,notes=COALESCE(notes,'') || ? WHERE id=?",
                    (_utcnow().isoformat(), outcome, pnl, " | auto-settled from official Kalshi result; paper only", trade_id),
                )
                conn.commit()
                try:
                    record_decision_audit(
                        conn, event_type="paper_trade_settled", trade_id=int(trade_id), market_ticker=str(ticker),
                        direction=str(direction), decision="settled", reason="Settled from official Kalshi market result.",
                        entry_ask_cents=float(entry), internal_score=None, created_at=_utcnow().isoformat(),
                        payload={"official_result": result, "outcome": outcome, "net_pnl_cents": round(pnl, 4), "contracts": int(contracts)},
                    )
                    conn.commit()
                except Exception as audit_exc:
                    log.warning("Could not audit settlement of trade %s: %s", trade_id, audit_exc)
                log.info("Paper trade %s settled from official result=%s outcome=%s PnL=%.2fc", trade_id, result, outcome, pnl)
                continue

            orderbook = get_orderbook(str(ticker))
            bid = _top_bid(orderbook, "yes" if direction == "UP" else "no")
            if bid is None:
                log.warning("Paper trade %s has no valid exit-side bid; leaving position open", trade_id)
                continue

            now = _utcnow()
            last_check = _parse_time(last_check_at)
            due = last_check is None or (now - last_check).total_seconds() >= check_seconds
            tp_armed = bool(tp_activated_at)
            tp_peak = float(stored_tp_peak) if stored_tp_peak is not None else None
            tp_target = float(stored_tp_target) if stored_tp_target is not None else None
            tp_triggered = False
            tp_reason = ""
            profit_protection_triggered = False
            profit_protection_reason = ""
            if tp_enabled:
                if tp_target is None:
                    tp_target = _take_profit_target_cents(float(entry), float(entry_fee), exit_fee, tp_pct)
                    conn.execute("UPDATE paper_trades SET take_profit_target_cents=? WHERE id=?", (tp_target, trade_id))
                    conn.commit()
                    if tp_target >= 100:
                        log.warning("Paper trade %s take-profit target capped at Kalshi maximum 100c; requested %.1f%% net ROI may be unattainable after fees", trade_id, tp_pct)
                if tp_target <= 100 and not tp_armed and float(bid) >= tp_target:
                    tp_armed = True
                    tp_peak = float(bid)
                    tp_activated_at = now.isoformat()
                    conn.execute("UPDATE paper_trades SET take_profit_activated_at=?,take_profit_peak_bid_cents=? WHERE id=?",
                                 (tp_activated_at, tp_peak, trade_id))
                    conn.commit()
                    log.info("Paper trade %s take-profit armed at %.2fc target %.2fc", trade_id, bid, tp_target)
                elif tp_armed and tp_target <= 100:
                    tp_peak = max(float(tp_peak if tp_peak is not None else tp_target), float(bid))
                    conn.execute("UPDATE paper_trades SET take_profit_peak_bid_cents=? WHERE id=?", (tp_peak, trade_id))
                    conn.commit()
                    floor = _take_profit_trailing_floor(tp_target, tp_peak, trail_pct)
                    if float(bid) <= floor:
                        tp_triggered = True
                        tp_reason = f"observed bid {float(bid):.2f}c at/below trailing floor {floor:.2f}c; peak {tp_peak:.2f}c"

            trend_triggered = False
            trend_reason = ""
            fresh_candles = None
            candle_ts = None
            if due and (trend_enabled or shadow_enabled or (tp_enabled and tp_armed and (reversal_enabled or profit_protection_enabled))):
                conn.execute("UPDATE paper_trades SET last_trend_check_at=? WHERE id=?", (now.isoformat(), trade_id))
                conn.commit()
                try:
                    tp_safety_check = tp_enabled and tp_armed and (reversal_enabled or profit_protection_enabled)
                    bundle = _get_signal_bundle_for_evaluation() if tp_safety_check else None
                    candles = bundle["candles_1m"] if bundle else fetch_closed_candles("1m", 100)
                    candle_ts = str(candles["timestamp"].iloc[-1]) if "timestamp" in candles.columns else str(candles.index[-1])
                    fresh_candles = bundle
                    if entry_btc is not None and (trend_enabled or shadow_enabled):
                        last_close = float(candles["close"].iloc[-1])
                        if candle_ts != last_candle_ts:
                            update = update_trend_stop(
                                candles, direction,
                                high_water_btc=float(high_water) if high_water is not None else float(entry_btc),
                                low_water_btc=float(low_water) if low_water is not None else float(entry_btc),
                                trailing_stop_btc=float(trailing_stop) if trailing_stop is not None else None,
                                atr_multiplier=atr_mult, swing_lookback=lookback,
                            )
                            conn.execute(
                                """UPDATE paper_trades SET high_water_btc=?,low_water_btc=?,trailing_stop_btc=?,
                                   last_stop_candle_timestamp=? WHERE id=?""",
                                (update["high_water_btc"], update["low_water_btc"], update["trailing_stop_btc"], candle_ts, trade_id),
                            )
                            conn.commit()
                            high_water, low_water, trailing_stop = update["high_water_btc"], update["low_water_btc"], update["trailing_stop_btc"]
                            trend_reason = str(update.get("reason", "trend stop updated"))
                            trend_triggered = bool(update.get("triggered"))
                        elif trailing_stop is not None:
                            trend_triggered = ((direction == "UP" and last_close <= float(trailing_stop))
                                               or (direction == "DOWN" and last_close >= float(trailing_stop)))
                            trend_reason = f"last closed BTC candle {last_close:.2f} breached stored chart stop {float(trailing_stop):.2f}"
                    if tp_enabled and tp_armed and tp_safety_check and fresh_candles and candle_ts != tp_last_reversal_ts and tp_target is not None and tp_target <= 100:
                        safety_reasons: list[str] = []
                        if profit_protection_enabled:
                            major_move, major_move_reason = _major_adverse_btc_move(
                                candles, direction, major_move_atr_multiplier
                            )
                            if major_move:
                                safety_reasons.append(major_move_reason)
                        if reversal_enabled:
                            parsed_market = _extract_market(market_payload)
                            if parsed_market:
                                quote = _best_quotes(orderbook)
                                yes_bid, no_bid = quote["yes_bid"], quote["no_bid"]
                                yes_ask, no_ask = quote["yes_ask"], quote["no_ask"]
                                spread_values = []
                                if yes_bid is not None and yes_ask is not None:
                                    spread_values.append(yes_ask - yes_bid)
                                if no_bid is not None and no_ask is not None:
                                    spread_values.append(no_ask - no_bid)
                                spread_now = max(spread_values) if len(spread_values) == 2 else None
                                if yes_ask is None or no_ask is None or spread_now is None or spread_now < 0:
                                    log.warning("Paper trade %s reversal check skipped: invalid orderbook-derived quotes", trade_id)
                                else:
                                    reversal = evaluate_signal(
                                        fresh_candles["candles_1m"], fresh_candles["candles_5m"], fresh_candles["candles_15m"],
                                        MarketContext(strike_price=parsed_market["strike"], seconds_remaining=parsed_market["seconds"],
                                                      up_ask_cents=yes_ask, down_ask_cents=no_ask,
                                                      spread_cents=spread_now, estimated_fee_cents=float(entry_fee)),
                                        minimum_score=int(_safe_float(os.getenv("PAPER_MINIMUM_SCORE", "65"), 65)),
                                        max_entry_cents=_entry_price_limits()[1],
                                        max_spread_cents=_safe_float(os.getenv("PAPER_MAX_SPREAD_CENTS", "5"), 5),
                                    ).to_dict()
                                    veto, veto_reasons = _major_flaw_veto(direction, reversal, max(2, min(12, int(_safe_float(os.getenv("PAPER_HYBRID_VETO_MIN_POINTS", "4"), 4)))))
                                    if veto:
                                        safety_reasons.extend("major opposing setup: " + reason for reason in veto_reasons)
                        conn.execute("UPDATE paper_trades SET take_profit_last_reversal_candle_timestamp=? WHERE id=?", (candle_ts, trade_id))
                        conn.commit()
                        if safety_reasons:
                            protection_note = "; ".join(safety_reasons)
                            if profit_protection_enabled:
                                profit_protection_triggered = True
                                profit_protection_reason = protection_note
                                break_even_bid = float(entry) + float(entry_fee) + exit_fee
                                if float(bid) < break_even_bid:
                                    profit_protection_reason += (
                                        f"; WARNING: observed bid {float(bid):.2f}c is below fee-adjusted break-even "
                                        f"({break_even_bid:.2f}c), so a loss cannot be prevented at this quote"
                                    )
                                else:
                                    profit_protection_reason += (
                                        f"; observed bid {float(bid):.2f}c is at/above fee-adjusted break-even "
                                        f"({break_even_bid:.2f}c)"
                                    )
                                log.warning("Paper trade %s profit-protection exit triggered: %s", trade_id, profit_protection_reason)
                            elif not tp_triggered:
                                tp_triggered = True
                                tp_reason = "major opposing setup after take-profit activation: " + protection_note
                                log.info("Paper trade %s reversal exit triggered: %s", trade_id, tp_reason)
                except (CandleFeedError, KalshiAPIError, ValueError, TypeError, KeyError, IndexError) as exc:
                    log.warning("Paper trade %s trend/profit-protection check unavailable: %s", trade_id, exc)

            fixed_triggered = fixed_stop_enabled and static_stop is not None and float(bid) <= float(static_stop)
            if trend_triggered and shadow_enabled:
                shadow_now = _utcnow().isoformat()
                shadow_pnl = (float(bid) - float(entry) - float(entry_fee) - exit_fee) * int(contracts)
                cur = conn.execute(
                    """UPDATE paper_trades SET trend_stop_shadow_triggered_at=?,trend_stop_shadow_trigger_bid_cents=?,
                       trend_stop_shadow_pnl_cents=?,trend_stop_shadow_reason=?,trend_stop_shadow_candle_timestamp=?
                       WHERE id=? AND trend_stop_shadow_triggered_at IS NULL""",
                    (shadow_now, float(bid), shadow_pnl, trend_reason, candle_ts, trade_id),
                )
                conn.commit()
                if cur.rowcount:
                    log.info("Paper trade %s trend-stop SHADOW only at bid %.2fc; hypothetical PnL %.2fc", trade_id, bid, shadow_pnl)

            if (trend_triggered and trend_enabled) or fixed_triggered or profit_protection_triggered or tp_triggered:
                pnl = (float(bid) - float(entry) - float(entry_fee) - exit_fee) * int(contracts)
                if trend_triggered and trend_enabled:
                    outcome, note = "TREND_STOP_LOSS", f" | simulated chart-stop exit at bid {bid:.2f}c; {trend_reason}"
                elif fixed_triggered:
                    outcome, note = "STOP_LOSS", f" | simulated fixed-stop exit at bid {bid:.2f}c"
                elif profit_protection_triggered:
                    outcome, note = "PROFIT_PROTECTION", (
                        f" | simulated profit-protection exit at observed Kalshi bid {bid:.2f}c; "
                        f"target had previously been reached; {profit_protection_reason}; "
                        f"net P&L {pnl:.2f}c after fees; exits are not guaranteed at the observed quote"
                    )
                else:
                    outcome, note = "TAKE_PROFIT", f" | simulated trailing take-profit at bid {bid:.2f}c; {tp_reason}; target={tp_target}c"
                conn.execute("UPDATE paper_trades SET settled_at=?,outcome=?,pnl_cents=?,notes=COALESCE(notes,'') || ? WHERE id=?",
                             (_utcnow().isoformat(), outcome, pnl, note, trade_id))
                conn.commit()
                try:
                    record_decision_audit(
                        conn, event_type="paper_trade_exited", trade_id=int(trade_id), market_ticker=str(ticker),
                        direction=str(direction), decision=outcome.lower(), reason=note.strip(),
                        entry_ask_cents=float(entry), created_at=_utcnow().isoformat(),
                        payload={"simulated_exit_bid_cents": float(bid), "net_pnl_cents": round(pnl, 4),
                                 "exit_reason": outcome, "profit_protection_triggered": bool(profit_protection_triggered),
                                 "paper_only": True},
                    )
                    conn.commit()
                except Exception as audit_exc:
                    log.warning("Could not audit exit of trade %s: %s", trade_id, audit_exc)
                log.warning("Paper trade %s simulated close=%s bid=%.2fc net PnL=%.2fc; no real order placed", trade_id, outcome, bid, pnl)
        except Exception as exc:
            log.exception("Failed to monitor paper trade %s; keeping it open: %s", trade_id, exc)


def _major_flaw_veto(direction: str, internal_payload: dict[str, Any], minimum_opposing_points: int = 4) -> tuple[bool, list[str]]:
    """Compatibility helper for take-profit reversal monitoring."""
    wanted = 1 if str(direction).upper() == "UP" else -1
    strategies = internal_payload.get("strategies") or {}
    reasons: list[str] = []
    trends = (strategies.get("multi_timeframe_trend") or {}).get("timeframes") or {}
    totals = strategies.get("evidence_totals") or {}
    up_points, down_points = int(totals.get("up_points", 0) or 0), int(totals.get("down_points", 0) or 0)
    wanted_points = up_points if wanted > 0 else down_points
    opposing_points = down_points if wanted > 0 else up_points
    margin = opposing_points - wanted_points
    if trends.get("5m") == -wanted and trends.get("15m") == -wanted and margin >= max(2, int(minimum_opposing_points)):
        reasons.append(f"Both 5m and 15m trends oppose the position and opposing evidence leads by {margin} points.")
    box = strategies.get("box_theory") or {}
    structure = strategies.get("market_structure") or {}
    volume = strategies.get("volume_confirmation") or {}
    if box.get("direction") == -wanted and box.get("state") in {"bullish_breakout", "bearish_breakout"} and bool(volume.get("confirmed")) and structure.get("direction") == -wanted:
        reasons.append("A volume-confirmed breakout agrees with opposing market structure.")
    return bool(reasons), reasons


def _mark_queued_alerts_during_cooldown(conn, source: str, cooldown_reason: str, *, max_alerts: int = 30) -> int:
    """Record why queued TradingView alerts cannot be acted on during a risk cooldown.

    Alerts are time-sensitive: do not leave them pending to be replayed after the
    cooldown ends, and never let this helper bypass the circuit breaker.
    """
    if str(source or "").strip().lower() not in {"gainzalgo", "hybrid"}:
        return 0
    limit = max(1, min(100, int(max_alerts)))
    marked = 0
    for _ in range(limit):
        queued, direction = _next_gainzalgo_signal(conn)
        if not queued:
            break
        if direction == "WAIT":
            note = "Indicator explicitly reported WAIT; no entry was opened. " + str(cooldown_reason)[:300]
            _mark_gainzalgo(conn, queued["id"], "wait", note)
        else:
            note = (
                "Blocked by paper risk cooldown: " + str(cooldown_reason)[:300]
                + ". This time-sensitive alert will not be replayed after cooldown or outside its original 15-minute window."
            )
            _mark_gainzalgo(conn, queued["id"], "skipped", note)
        marked += 1
    return marked


def run_once() -> dict[str, Any]:
    """Run one paper-only cycle. Any error or missing critical data fails closed."""
    if os.getenv("TRADING_MODE", "paper").strip().lower() != "paper":
        return {"action": "blocked", "reason": "TRADING_MODE must be paper"}
    if _truthy("LIVE_TRADING_ENABLED", False):
        return {"action": "blocked", "reason": "Worker refuses to run when live trading is enabled"}

    source = os.getenv("PAPER_SIGNAL_SOURCE", "internal").strip().lower()
    if source not in {"internal", "gainzalgo", "hybrid"}:
        return {"action": "blocked", "reason": "PAPER_SIGNAL_SOURCE must be internal, gainzalgo, or hybrid"}
    # Separate Render services cannot share a local SQLite file. Require an explicit
    # shared database unless this worker is launched inside the web service process.
    if source in {"gainzalgo", "hybrid"} and not os.getenv("DATABASE_URL") and not _truthy("PAPER_SHARED_SQLITE_OK", False):
        return {"action": "blocked", "reason": "External/hybrid mode requires the same DATABASE_URL in web service and worker; SQLite files are local to each service. For a deliberately single-process local test only, set PAPER_SHARED_SQLITE_OK=true."}

    max_trade = max(0.01, _safe_float(os.getenv("MAX_TRADE_DOLLARS", "1.00"), 1.00))
    max_loss = max(0.01, _safe_float(os.getenv("MAX_DAILY_LOSS_DOLLARS", "10.00"), 10.00))
    fee = max(0.0, _safe_float(os.getenv("PAPER_FEE_CENTS_PER_CONTRACT", "2.0"), 2.0))
    exit_fee = max(0.0, _safe_float(os.getenv("PAPER_EXIT_FEE_CENTS_PER_CONTRACT", "2.0"), 2.0))
    stop_pct = max(0.1, min(99.0, _safe_float(os.getenv("PAPER_STOP_LOSS_PERCENT", "35"), 35)))
    min_score = max(0, min(100, int(_safe_float(os.getenv("PAPER_MINIMUM_SCORE", "65"), 65))))
    hard_max_entry, max_entry = _entry_price_limits()
    max_spread = max(0.0, min(100.0, _safe_float(os.getenv("PAPER_MAX_SPREAD_CENTS", "5"), 5)))
    tp_enabled = _truthy("PAPER_TAKE_PROFIT_ENABLED", False)
    tp_pct = _safe_float(os.getenv("PAPER_TAKE_PROFIT_PERCENT", "20"), 20)
    trail_pct = _safe_float(os.getenv("PAPER_TAKE_PROFIT_TRAIL_PERCENT", "5"), 5)
    if tp_enabled:
        try:
            _take_profit_target_cents(50, fee, exit_fee, tp_pct)
            _take_profit_trailing_floor(60, 65, trail_pct)
        except ValueError as exc:
            return {"action": "blocked", "reason": str(exc)}

    conn = None
    adaptive_policy: dict[str, Any] | None = None
    try:
        conn = _connect()
        _manage_open(conn)
        # A separate, paper-only shadow duplicates entries but holds its own
        # virtual copy until official settlement. Any provider error here is
        # isolated from the primary engine and cannot block normal risk logic.
        if _truthy("PAPER_STRATEGY_SHADOW_ENABLED", False):
            try:
                shadow_result = process_hold_to_settlement_shadow(
                    conn, get_market,
                    max_market_checks=max(0, min(4, int(_safe_float(os.getenv("PAPER_STRATEGY_SHADOW_MARKET_CHECKS_PER_CYCLE", "1"), 1)))),
                    retry_seconds=max(30, min(3600, int(_safe_float(os.getenv("PAPER_STRATEGY_SHADOW_RETRY_SECONDS", "60"), 60)))),
                )
                if shadow_result.get("created") or shadow_result.get("newly_settled") or shadow_result.get("errors"):
                    log.info("Strategy shadow comparison: %s", shadow_result)
            except Exception as shadow_exc:
                log.warning("Hold-to-settlement strategy shadow failed independently; primary engine unaffected: %s", shadow_exc)
        # Record every newly settled trade before recomputing the bounded
        # paper-only entry-price policy. The learner can only tighten the target;
        # it never changes the 92c hard cap, dollar limits, or live-trading state.
        try:
            learned_count = sync_settled_trade_learning(conn, batch_size=250)
            if learned_count:
                log.info("Trade-learning journal recorded %s newly settled outcome(s)", learned_count)
        except Exception as learning_exc:
            log.warning("Trade-learning journal sync failed; adaptive policy keeps its last safe state: %s", learning_exc)
        if source == "hybrid":
            try:
                adaptive_policy = update_adaptive_entry_policy(
                    conn, configured_target_cents=max_entry, hard_cap_cents=hard_max_entry,
                    enabled=_truthy("PAPER_ADAPTIVE_ENTRY_TARGET_ENABLED", True),
                )
                effective_target = float(adaptive_policy.get("effective_target_cents", max_entry))
                max_entry = min(max_entry, effective_target)
                if adaptive_policy.get("status") == "tightened":
                    log.warning("Adaptive paper entry target tightened to %.2fc based on settled hybrid outcomes: %s",
                                max_entry, adaptive_policy.get("reason", "sample criteria met"))
            except Exception as adaptive_exc:
                # Learning analytics are fail-safe: a policy error does not alter
                # the user's configured 80c target or any other gate.
                log.warning("Adaptive entry policy unavailable; using configured target %.2fc: %s", max_entry, adaptive_exc)
        # An independent paper-only circuit breaker prevents repeated new entries
        # during a loss cluster. Open positions were already managed above.
        streak_limit = max(0, min(20, int(_safe_float(os.getenv("PAPER_CONSECUTIVE_LOSS_LIMIT", "4"), 4))))
        cooldown_minutes = max(0, min(1440, int(_safe_float(os.getenv("PAPER_LOSS_STREAK_COOLDOWN_MINUTES", "15"), 15))))
        if streak_limit and cooldown_minutes:
            recent_settled = conn.execute(
                "SELECT settled_at,pnl_cents FROM paper_trades WHERE settled_at IS NOT NULL ORDER BY id DESC LIMIT ?",
                (streak_limit,),
            ).fetchall()
            cooldown = _loss_streak_cooldown_status(recent_settled, limit=streak_limit, cooldown_minutes=cooldown_minutes)
            if cooldown:
                blocked_alerts = 0
                try:
                    blocked_alerts = _mark_queued_alerts_during_cooldown(
                        conn, source, cooldown["reason"], max_alerts=30,
                    )
                except Exception as alert_exc:
                    log.warning("Could not persist queued-alert cooldown status: %s", alert_exc)
                log.warning(
                    "Paper entry circuit breaker active: %s; marked %s queued external alert(s) without opening trades",
                    cooldown["reason"], blocked_alerts,
                )
                return {"action": "risk_cooldown", **cooldown, "blocked_alerts": blocked_alerts,
                        "mode": "SIMULATION_ONLY", "trade_placed": False}

        # Keep chart trends precomputed even when no TradingView alert is waiting.
        # Retain this exact snapshot for the entry decision later in this cycle.
        trend_snapshot = _refresh_btc_trend_monitor(conn)
        use_external = source in {"gainzalgo", "hybrid"}
        queued, queued_direction = _next_gainzalgo_signal(conn) if use_external else (None, None)
        # WAIT is an informational state update. Record it promptly—even if daily
        # entry limits are reached or a paper position is already open—and never
        # send it through market entry logic.
        if queued and queued_direction == "WAIT":
            wait_body = _safe_external_payload(queued.get("payload_json", "{}"))
            wait_detail = next((str(wait_body.get(k)).strip() for k in ("wait_reason", "reason", "message")
                                if wait_body.get(k) is not None and str(wait_body.get(k)).strip()), "")
            note = "Indicator reports WAIT THIS WINDOW; no paper trade was opened."
            if wait_detail and wait_detail.upper() not in {"WAIT", "WAIT_THIS_WINDOW", "WAIT THIS WINDOW"}:
                note += " Detail: " + wait_detail[:300]
            _mark_gainzalgo(conn, queued["id"], "wait", note)
            return {"action": "wait", "reason": note, "source": source,
                    "signal_id": queued["id"], "trade_placed": False, "mode": "SIMULATION_ONLY"}

        realized, open_cost, daily_count, halted = _limits(conn, max_trade, max_loss)
        if halted:
            reason = "daily loss budget reached including open-position risk"
            if queued:
                _mark_gainzalgo(conn, queued["id"], "skipped", reason)
            return {"action": "blocked", "reason": reason,
                    "realized_pnl_cents": round(realized, 2), "open_risk_cents": round(open_cost, 2)}
        daily_cap = max(1, min(96, int(_safe_float(os.getenv("PAPER_MAX_TRADES_PER_DAY", "96"), 96))))
        if daily_count >= daily_cap:
            reason = "paper daily trade cap reached"
            if queued:
                _mark_gainzalgo(conn, queued["id"], "skipped", reason)
            return {"action": "wait", "reason": reason}

        if conn.execute("SELECT 1 FROM paper_trades WHERE settled_at IS NULL LIMIT 1").fetchone():
            if queued:
                _mark_gainzalgo(conn, queued["id"], "skipped", "an open paper position already exists")
            return {"action": "monitoring", "reason": "an open paper position already exists"}
        if use_external and not queued:
            return {"action": "wait", "reason": f"waiting for a fresh TradingView/GainzAlgo alert; source={source}", "source": source}

        found = discover()
        candidates = found.get("candidates", []) if isinstance(found, dict) else []
        parsed: list[dict] = []
        rejection_counts: dict[str, int] = {}
        for candidate in candidates:
            ticker = candidate.get("ticker") if isinstance(candidate, dict) else None
            if not ticker:
                rejection_counts["missing_candidate_ticker"] = rejection_counts.get("missing_candidate_ticker", 0) + 1
                continue
            try:
                extracted = _extract_market(get_market(str(ticker)), rejection_counts)
                if extracted:
                    parsed.append(extracted)
            except (KalshiAPIError, ValueError, TypeError) as exc:
                rejection_counts["market_lookup_error"] = rejection_counts.get("market_lookup_error", 0) + 1
                log.warning("Market lookup failed for %s: %s", ticker, exc)
        if not parsed:
            log.warning(
                "BTC market discovery found no eligible candidate: candidate_count=%s rejection_counts=%s",
                len(candidates), json.dumps(rejection_counts, sort_keys=True),
            )
            reason = "No candidate had an explicit valid strike and active 15m close time in the KXBTC15M series."
            if queued:
                # Keep a fresh external alert unprocessed while discovery can retry,
                # and make the pending state visible in the audit trail.
                _set_gainzalgo_pending(conn, queued["id"], "waiting_for_market", reason)
                _log_wait(conn, reason, external=queued,
                          internal_payload={"decision_status": "skipped", "chart_trend_at_decision": {
                              "status": (trend_snapshot or {}).get("status"),
                              "updated_at": (trend_snapshot or {}).get("updated_at"),
                              "candidate_bias": (trend_snapshot or {}).get("candidate_bias"),
                              "evidence_score": (trend_snapshot or {}).get("evidence_score"),
                              "age_seconds": (trend_snapshot or {}).get("age_seconds")}})
            return {"action": "wait", "reason": reason}
        if queued:
            # A price-waiting alert is only eligible in the same 15-minute window
            # in which it was received. Never carry it into the next contract.
            alert_time = _parse_time(str(queued.get("created_at") or ""))
            if alert_time is None or _quarter_hour_window_start(alert_time) != _quarter_hour_window_start(_utcnow()):
                note = "alert belongs to a prior or unverified 15-minute window; it cannot be reused for a later contract"
                _mark_gainzalgo(conn, queued["id"], "ignored", note)
                return {"action": "wait", "reason": note, "signal_id": queued["id"], "trade_placed": False,
                        "mode": "SIMULATION_ONLY"}
        market = min(parsed, key=lambda item: item["seconds"])
        min_seconds = max(30, int(_safe_float(os.getenv("PAPER_MIN_SECONDS_REMAINING", "30"), 30)))
        if market["seconds"] < min_seconds:
            if queued:
                _mark_gainzalgo(conn, queued["id"], "skipped", "less than the minimum time remained for entry")
                _log_wait(conn, "Too little time remains for a fresh entry.", ticker=market["ticker"], external=queued)
            return {"action": "wait", "reason": "too little time remains for a fresh entry", "ticker": market["ticker"]}

        orderbook = get_orderbook(market["ticker"])
        quote = _best_quotes(orderbook)
        yes_bid, no_bid = quote["yes_bid"], quote["no_bid"]
        yes_ask, no_ask = quote["yes_ask"], quote["no_ask"]
        if any(x is None for x in (yes_ask, no_ask, yes_bid, no_bid)):
            reason = "current orderbook is missing a usable YES/NO best bid to derive asks"
            if queued:
                _set_gainzalgo_pending(conn, queued["id"], "waiting_for_orderbook", reason)
                _log_wait(conn, reason, ticker=market["ticker"], external=queued,
                          internal_payload={"decision_status": "skipped", "chart_trend_at_decision": {
                              "status": (trend_snapshot or {}).get("status"),
                              "updated_at": (trend_snapshot or {}).get("updated_at"),
                              "candidate_bias": (trend_snapshot or {}).get("candidate_bias"),
                              "evidence_score": (trend_snapshot or {}).get("evidence_score"),
                              "age_seconds": (trend_snapshot or {}).get("age_seconds")}})
            return {"action": "wait", "reason": reason, "ticker": market["ticker"]}
        spread_values = [float(yes_ask) - float(yes_bid), float(no_ask) - float(no_bid)]
        spread = max(spread_values)
        if (not all(math.isfinite(v) for v in [float(yes_ask), float(no_ask), float(yes_bid), float(no_bid), spread])
                or float(yes_ask) <= 0 or float(yes_ask) >= 100
                or float(no_ask) <= 0 or float(no_ask) >= 100
                or spread < 0):
            if queued:
                _mark_gainzalgo(conn, queued["id"], "skipped", "invalid/crossed Kalshi quote")
                _log_wait(conn, "Invalid or crossed Kalshi bid/ask quote; no entry.", ticker=market["ticker"], external=queued)
            return {"action": "wait", "reason": "invalid or crossed Kalshi bid/ask quote", "ticker": market["ticker"]}
        now_text = _utcnow().isoformat()
        warnings: list[str] = []
        internal_payload: dict[str, Any] | None = None
        entry_btc = 0.0
        entry_candle_timestamp = None
        final_direction = "WAIT"
        score = 0
        entry_signal_score: int | None = None
        entry_signal_log_id: int | None = None
        entry = None

        if use_external:
            direction = queued_direction
            entry = float(yes_ask if direction == "UP" else no_ask)
            if entry <= 0 or entry >= 100:
                note = f"invalid side ask {entry:.2f}c; no entry"
                _mark_gainzalgo(conn, queued["id"], "skipped", note)
                _log_wait(conn, note, ticker=market["ticker"], external=queued)
                return {"action": "wait", "reason": note, "ticker": market["ticker"], "entry_cents": entry}
            if spread > max_spread:
                note = f"spread {spread:.2f}c exceeds max spread {max_spread:.2f}c"
                _mark_gainzalgo(conn, queued["id"], "skipped", note)
                _log_wait(conn, note, ticker=market["ticker"], external=queued,
                          internal_payload={"entry_ask_cents": entry, "spread_cents": spread,
                                            "decision_status": "skipped", "chart_trend_at_decision": {
                                                "status": (trend_snapshot or {}).get("status"),
                                                "updated_at": (trend_snapshot or {}).get("updated_at"),
                                                "candidate_bias": (trend_snapshot or {}).get("candidate_bias"),
                                                "evidence_score": (trend_snapshot or {}).get("evidence_score"),
                                                "timeframes": (trend_snapshot or {}).get("timeframes")}})
                return {"action": "wait", "reason": note, "ticker": market["ticker"], "spread_cents": spread}

            price_action, price_note = _entry_price_gate(entry, hard_max_entry, max_entry)
            if price_action != "enter":
                note = price_note
                _set_gainzalgo_pending(conn, queued["id"], "waiting_for_better_price", note)
                max_age_config = os.getenv("TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS", os.getenv("GAINZALGO_MAX_SIGNAL_AGE_SECONDS", "300"))
                max_age = max(30, min(900, int(_safe_float(max_age_config, 300))))
                remaining = max(0.0, max_age - float(queued.get("age_seconds", 0) or 0))
                wait_payload = {
                    "direction": "WAIT", "decision_status": "wait_for_better_price", "score": 0,
                    "source": "TradingView-primary hybrid" if source == "hybrid" else "TradingView/GainzAlgo",
                    "external_signal": {"id": queued["id"], "created_at": queued["created_at"],
                                         "direction": direction, "symbol": queued.get("symbol"),
                                         "timeframe": queued.get("timeframe"), "age_seconds": queued.get("age_seconds")},
                    "entry_ask_cents": entry, "spread_cents": spread,
                    "fusion": {"approved": False, "vetoed_by_internal": False, "direction": "WAIT",
                               "entry_ask_cents": entry, "spread_cents": spread, "reasons": [note]},
                    "chart_trend_at_decision": {
                        "status": (trend_snapshot or {}).get("status"),
                        "updated_at": (trend_snapshot or {}).get("updated_at"),
                        "candidate_bias": (trend_snapshot or {}).get("candidate_bias"),
                        "evidence_score": (trend_snapshot or {}).get("evidence_score"),
                        "evidence_points": (trend_snapshot or {}).get("evidence_points"),
                        "timeframes": (trend_snapshot or {}).get("timeframes"),
                        "age_seconds": (trend_snapshot or {}).get("age_seconds"),
                    },
                    "reasons": [note], "final_decision_reason": note,
                    "adaptive_learning_policy": adaptive_policy,
                    "disclaimer": "No paper entry opened at this price; hypothetical outcome is evaluated separately from actual trading.",
                }
                wait_payload["entry_price_gate"].update({"hard_cap_cents": hard_max_entry, "preferred_target_cents": max_entry})
                _record_signal_log(conn, created_at=_utcnow().isoformat(), ticker=market["ticker"], direction="WAIT",
                                   score=0, btc_price=_optional_positive_price((trend_snapshot or {}).get("btc_price")),
                                   strike=market["strike"], seconds_remaining=market["seconds"],
                                   max_entry_cents=hard_max_entry, payload=wait_payload)
                return {"action": "wait_for_better_price", "reason": note, "ticker": market["ticker"],
                        "direction": direction, "entry_ask_cents": entry, "entry_target_max_cents": max_entry,
                        "hard_entry_cap_cents": hard_max_entry,
                        "signal_age_seconds": queued.get("age_seconds"), "signal_freshness_seconds_remaining": round(remaining, 1),
                        "trade_placed": False, "mode": "SIMULATION_ONLY"}

            if source == "hybrid":
                bundle = _get_signal_bundle_for_evaluation()
                internal_signal = evaluate_signal(
                    bundle["candles_1m"], bundle["candles_5m"], bundle["candles_15m"],
                    MarketContext(strike_price=market["strike"], seconds_remaining=market["seconds"],
                                  up_ask_cents=yes_ask, down_ask_cents=no_ask, spread_cents=spread,
                                  estimated_fee_cents=fee),
                    minimum_score=min_score, max_entry_cents=hard_max_entry, max_spread_cents=max_spread,
                )
                internal_payload = internal_signal.to_dict()
                entry_btc = float(internal_signal.btc_price)
                if "timestamp" in bundle["candles_1m"].columns:
                    entry_candle_timestamp = str(bundle["candles_1m"]["timestamp"].iloc[-1])
                else:
                    entry_candle_timestamp = _utcnow().isoformat()
                fused = evaluate_fusion(
                    direction=direction, alert_created_at=queued["created_at"], symbol=queued.get("symbol"),
                    timeframe=queued.get("timeframe"), internal_payload=internal_payload,
                    market_ticker=market["ticker"], seconds_remaining=market["seconds"],
                    up_ask_cents=yes_ask, down_ask_cents=no_ask, spread_cents=spread,
                    fee_cents_per_contract=fee, max_entry_cents=hard_max_entry, max_spread_cents=max_spread,
                    max_alert_age_seconds=max(30, min(900, int(_safe_float(os.getenv("TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS", "300"), 300)))),
                    min_seconds_remaining=min_seconds, minimum_opposing_points=max(2, min(12, int(_safe_float(os.getenv("PAPER_HYBRID_VETO_MIN_POINTS", "4"), 4)))),
                    trend_snapshot=trend_snapshot,
                    max_trend_snapshot_age_seconds=max(15.0, min(180.0, _safe_float(os.getenv("BTC_TREND_MONITOR_MAX_CACHE_AGE_SECONDS", "45"), 45.0))),
                    require_trend_snapshot=True,
                    minimum_trend_conflict_score=max(0, min(100, int(_safe_float(os.getenv("PAPER_HYBRID_TREND_VETO_MIN_SCORE", "65"), 65)))),
                    minimum_trend_conflict_margin=max(1, min(11, int(_safe_float(os.getenv("PAPER_HYBRID_TREND_VETO_MIN_MARGIN", "4"), 4)))),
                ).to_dict()
                final_direction = fused["direction"]
                score = int(internal_payload.get("score", 0) or 0)
                payload = {
                    **internal_payload,
                    "direction": final_direction,
                    "score": score,
                    "source": "TradingView-primary hybrid",
                    "external_signal": {"id": queued["id"], "created_at": queued["created_at"],
                                         "direction": direction, "symbol": queued.get("symbol"),
                                         "timeframe": queued.get("timeframe"), "age_seconds": queued.get("age_seconds"),
                                         "payload": _safe_external_payload(queued["payload_json"])},
                    "fusion": fused,
                    "chart_trend_at_decision": {
                        "status": (trend_snapshot or {}).get("status"),
                        "updated_at": (trend_snapshot or {}).get("updated_at"),
                        "candidate_bias": (trend_snapshot or {}).get("candidate_bias"),
                        "evidence_score": (trend_snapshot or {}).get("evidence_score"),
                        "evidence_points": (trend_snapshot or {}).get("evidence_points"),
                        "timeframes": (trend_snapshot or {}).get("timeframes"),
                        "age_seconds": (trend_snapshot or {}).get("age_seconds"),
                    },
                    "reasons": list(fused.get("reasons", [])) + [f"Internal diagnostics: {r}" for r in internal_payload.get("reasons", [])],
                    "warnings": list(internal_payload.get("warnings", [])) + list(fused.get("warnings", [])),
                    "final_decision_reason": "; ".join(fused.get("reasons", [])),
                    "adaptive_learning_policy": adaptive_policy,
                    "disclaimer": "TradingView primary; internal rules veto only major conflicts. Score is not a win probability. Paper mode only.",
                }
                entry_signal_log_id = _record_signal_log(
                    conn, created_at=now_text, ticker=market["ticker"], direction=final_direction,
                    score=score, btc_price=entry_btc, strike=market["strike"],
                    seconds_remaining=market["seconds"], max_entry_cents=hard_max_entry, payload=payload,
                )
                entry_signal_score = score
                if not fused["approved"]:
                    note = "; ".join(fused.get("reasons", [])) or "fusion checks rejected the alert"
                    _mark_gainzalgo(conn, queued["id"], "vetoed" if fused.get("vetoed_by_internal") else "skipped", note)
                    return {"action": "wait", "reason": note, "ticker": market["ticker"], "source": source,
                            "score": score, "warnings": payload["warnings"], "mode": "SIMULATION_ONLY"}
                warnings = payload["warnings"]
            else:
                candles = fetch_closed_candles("1m", 100)
                entry_btc = float(candles["close"].iloc[-1])
                entry_candle_timestamp = str(candles["timestamp"].iloc[-1]) if "timestamp" in candles.columns else _utcnow().isoformat()
                final_direction = "BUY_UP" if direction == "UP" else "BUY_DOWN"
                payload = {
                    "direction": final_direction, "score": 0, "score_kind": "no internal score in external-only mode",
                    "source": "TradingView/GainzAlgo external-only", "external_signal": {
                        "id": queued["id"], "created_at": queued["created_at"], "direction": direction,
                        "symbol": queued.get("symbol"), "timeframe": queued.get("timeframe"), "age_seconds": queued.get("age_seconds"),
                        "payload": _safe_external_payload(queued["payload_json"])},
                    "reasons": ["TradingView alert passed freshness, market, spread, and entry-price gates."],
                    "warnings": ["External-only mode does not run internal confirmation."],
                    "disclaimer": "External indicator direction; paper mode only; score is not a win probability.",
                }
                entry_signal_log_id = _record_signal_log(
                    conn, created_at=now_text, ticker=market["ticker"], direction=final_direction,
                    score=0, btc_price=entry_btc, strike=market["strike"],
                    seconds_remaining=market["seconds"], max_entry_cents=hard_max_entry, payload=payload,
                )
                entry_signal_score = None
                warnings = payload["warnings"]
        else:
            bundle = _get_signal_bundle_for_evaluation()
            signal = evaluate_signal(
                bundle["candles_1m"], bundle["candles_5m"], bundle["candles_15m"],
                MarketContext(strike_price=market["strike"], seconds_remaining=market["seconds"],
                              up_ask_cents=yes_ask, down_ask_cents=no_ask, spread_cents=spread,
                              estimated_fee_cents=fee),
                minimum_score=min_score, max_entry_cents=hard_max_entry, max_spread_cents=max_spread,
            )
            payload = signal.to_dict()
            payload["source"] = "internal"
            entry_btc = float(signal.btc_price)
            entry_candle_timestamp = str(bundle["candles_1m"]["timestamp"].iloc[-1]) if "timestamp" in bundle["candles_1m"].columns else _utcnow().isoformat()
            score = int(signal.score)
            if signal.direction == "WAIT":
                _record_signal_log(conn, created_at=now_text, ticker=market["ticker"], direction="WAIT", score=score,
                                   btc_price=signal.btc_price, strike=signal.strike_price,
                                   seconds_remaining=signal.seconds_remaining, max_entry_cents=hard_max_entry, payload=payload)
                return {"action": "wait", "ticker": market["ticker"], "score": signal.score,
                        "reasons": signal.reasons, "warnings": signal.warnings}
            direction = "UP" if signal.direction == "BUY_UP" else "DOWN"
            entry = float(yes_ask if direction == "UP" else no_ask)
            final_direction = signal.direction
            warnings = signal.warnings
            entry_signal_log_id = _record_signal_log(
                conn, created_at=now_text, ticker=market["ticker"], direction=final_direction,
                score=score, btc_price=signal.btc_price, strike=signal.strike_price,
                seconds_remaining=signal.seconds_remaining, max_entry_cents=hard_max_entry, payload=payload,
            )
            entry_signal_score = score

        if entry is None:
            return {"action": "wait", "reason": "candidate entry price was not set; fail closed"}
        price_action, price_note = _entry_price_gate(float(entry), hard_max_entry, max_entry)
        if price_action != "enter":
            wait_payload = dict(payload or {})
            wait_payload["entry_price_gate"] = {"decision": "wait_for_better_price", "current_ask_cents": float(entry),
                                                 "target_max_cents": max_entry, "reason": price_note}
            wait_payload["adaptive_learning_policy"] = adaptive_policy
            wait_payload["reasons"] = list(wait_payload.get("reasons", [])) + [price_note]
            wait_signal_id = _record_signal_log(conn, created_at=_utcnow().isoformat(), ticker=market["ticker"], direction="WAIT",
                               score=score, btc_price=entry_btc, strike=market["strike"],
                               seconds_remaining=market["seconds"], max_entry_cents=hard_max_entry, payload=wait_payload)
            if entry_signal_log_id is not None:
                _update_signal_decision_status(conn, entry_signal_log_id, "wait_for_better_price", price_note)
            return {"action": "wait_for_better_price", "reason": price_note, "ticker": market["ticker"],
                    "entry_ask_cents": float(entry), "entry_target_max_cents": max_entry,
                    "hard_entry_cap_cents": hard_max_entry, "trade_placed": False, "mode": "SIMULATION_ONLY"}
        if float(entry) > max_entry:
            premium_note = (
                f"Entry ask {float(entry):.2f}c is above the preferred soft target {max_entry:.2f}c, "
                f"but is allowed under the hard cap {hard_max_entry:.2f}c because the direction passed its required checks. "
                "The preferred target is not a probability estimate; higher entry prices leave less upside."
            )
            if isinstance(payload, dict):
                existing_reasons = payload.get("reasons", [])
                payload["reasons"] = list(existing_reasons) + [premium_note] if isinstance(existing_reasons, list) else [premium_note]
                existing_warnings = payload.get("warnings", [])
                payload["warnings"] = list(existing_warnings) + ["Entry is above the preferred price target; the hard cap and all other safeguards still apply."] if isinstance(existing_warnings, list) else ["Entry is above the preferred price target; the hard cap and all other safeguards still apply."]
            warnings = list(warnings or []) + ["Entry is above the preferred price target; the hard cap and all other safeguards still apply."]
            # Keep the same explanation in the persisted signal payload that the dashboard reads.
            if entry_signal_log_id is not None:
                conn.execute(
                    "UPDATE signal_log SET payload_json=? WHERE id=?",
                    (json.dumps(payload, separators=(",", ":"), default=str), entry_signal_log_id),
                )
                conn.commit()
            log.info("Entry above preferred target: ask=%.2fc preferred=%.2fc hard_cap=%.2fc; passing signal/risk checks remain required",
                     float(entry), max_entry, hard_max_entry)

        contracts = max(0, min(10, int((max_trade * 100) // (float(entry) + fee))))
        if contracts < 1:
            reason = "even one contract plus fees exceeds MAX_TRADE_DOLLARS"
            _update_signal_decision_status(conn, entry_signal_log_id, "blocked", reason)
            if queued:
                _mark_gainzalgo(conn, queued["id"], "skipped", reason)
            _log_wait(conn, reason, ticker=market["ticker"], external=queued, internal_payload=internal_payload or {})
            return {"action": "blocked", "reason": reason, "entry_cents": entry, "fee_cents": fee}
        if realized - open_cost - contracts * (float(entry) + fee) <= -max_loss * 100:
            reason = "trade would exceed daily loss budget when open risk is reserved"
            _update_signal_decision_status(conn, entry_signal_log_id, "blocked", reason)
            if queued:
                _mark_gainzalgo(conn, queued["id"], "skipped", reason)
            return {"action": "blocked", "reason": reason}

        stop = max(1.0, float(entry) * (1 - stop_pct / 100)) if _truthy("PAPER_STOP_LOSS_ENABLED", False) else None
        tp_target = _take_profit_target_cents(float(entry), fee, exit_fee, tp_pct) if tp_enabled else None
        if tp_enabled and tp_target is not None and tp_target >= 100:
            log.warning("New paper trade take-profit target capped at 100c; requested %.1f%% net ROI may be unattainable after fees", tp_pct)
        signal_source = "hybrid" if source == "hybrid" else ("TradingView" if use_external else "internal")
        external_id = queued["id"] if queued else None
        note = (f"TradingView-primary hybrid paper entry; signal_id={external_id}" if source == "hybrid" else
                f"TradingView/GainzAlgo paper entry; signal_id={external_id}" if use_external else
                "Internal strategy paper entry; heuristic evidence score is not a win probability")
        timeframe_note = queued.get("timeframe") if queued else None
        trade_cur = conn.execute(
            """INSERT INTO paper_trades (created_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,
               notes,stop_loss_cents,entry_btc_price,high_water_btc,low_water_btc,last_stop_candle_timestamp,
               take_profit_target_cents,entry_signal_source,external_signal_id,timeframe,
               entry_signal_log_id,entry_signal_score)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now_text, market["ticker"], direction, contracts, float(entry), fee,
             note + f" | preferred target {max_entry:.2f}c (soft only); hard entry cap {hard_max_entry:.2f}c", stop,
             entry_btc, entry_btc, entry_btc, entry_candle_timestamp, tp_target, signal_source, external_id,
             timeframe_note or "15m contract; TV timeframe unknown", entry_signal_log_id, entry_signal_score),
        )
        trade_id = trade_cur.lastrowid
        conn.commit()
        if entry_signal_log_id is not None:
            conn.execute("UPDATE signal_log SET decision_status='paper_opened' WHERE id=?", (entry_signal_log_id,))
        record_decision_audit(
            conn, event_type="paper_trade_opened", alert_id=external_id, signal_log_id=entry_signal_log_id,
            trade_id=trade_id, market_ticker=market["ticker"], direction=direction, decision="paper_opened",
            reason="Paper entry passed the configured freshness, quote, price, fusion, exposure, and risk checks.",
            entry_ask_cents=float(entry), spread_cents=spread, internal_score=entry_signal_score,
            created_at=now_text, payload={"contracts": contracts, "fee_cents_per_contract": fee,
                                         "total_entry_cost_cents": contracts * (float(entry) + fee),
                                         "hard_entry_cap_cents": hard_max_entry, "target_entry_cents": max_entry,
                                         "mode": "SIMULATION_ONLY"},
        )
        conn.commit()
        if queued:
            _mark_gainzalgo(conn, queued["id"], "processed", f"paper trade opened: {market['ticker']} {direction}")
        total_cost = contracts * (float(entry) + fee)
        log.info("Paper trade opened ticker=%s direction=%s source=%s contracts=%s entry=%.2fc total=%.2fc; simulation only",
                 market["ticker"], direction, signal_source, contracts, entry, total_cost)
        return {"action": "paper_opened", "ticker": market["ticker"], "direction": direction,
                "source": signal_source, "signal_id": external_id, "contracts": contracts, "entry_cents": float(entry),
                "fee_cents_per_contract": fee, "total_entry_cost_dollars": round(total_cost / 100, 4),
                "stop_loss_trigger_bid_cents": round(stop, 2) if stop is not None else None,
                "trend_stop_enabled": _truthy("PAPER_TREND_STOP_LOSS_ENABLED", False),
                "trend_stop_shadow_mode": _truthy("PAPER_TREND_STOP_SHADOW_MODE", False),
                "take_profit_enabled": tp_enabled, "take_profit_target_cents": round(tp_target, 2) if tp_target is not None else None,
                "take_profit_target_return_percent": tp_pct if tp_enabled else None, "entry_btc_price": round(entry_btc, 2),
                "preferred_entry_target_cents": max_entry, "entry_target_max_cents": max_entry, "configured_hard_max_entry_cents": hard_max_entry,
                "entry_signal_log_id": entry_signal_log_id, "entry_signal_score": entry_signal_score,
                "mode": "SIMULATION_ONLY", "warnings": warnings}
    except (CandleFeedError, KalshiAPIError, ValueError, TypeError, KeyError) as exc:
        log.warning("Paper worker cycle failed closed: %s", exc)
        return {"action": "error_wait", "reason": str(exc), "mode": "SIMULATION_ONLY"}
    except Exception as exc:
        log.exception("Unexpected paper worker error; no trade should be assumed")
        return {"action": "error_wait", "reason": f"unexpected error: {type(exc).__name__}: {exc}", "mode": "SIMULATION_ONLY"}
    finally:
        if conn is not None:
            conn.close()


def _write_heartbeat(result: dict[str, Any]) -> None:
    """Publish worker liveness and the latest WAIT/entry reason to the shared dashboard DB."""
    conn = None
    try:
        conn = _connect()
        action = str(result.get("action", "unknown"))[:80]
        reason = str(result.get("reason") or result.get("final_decision_reason") or "")[:500]
        conn.execute(
            """INSERT INTO worker_heartbeat (worker_name,last_seen_at,signal_source,mode,last_action,last_reason)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(worker_name) DO UPDATE SET last_seen_at=excluded.last_seen_at,
                 signal_source=excluded.signal_source,mode=excluded.mode,last_action=excluded.last_action,
                 last_reason=excluded.last_reason""",
            ("paper_worker", _utcnow().isoformat(), os.getenv("PAPER_SIGNAL_SOURCE", "internal").strip().lower(),
             "SIMULATION_ONLY", action, reason),
        )
        conn.commit()
    except Exception as exc:
        log.warning("Could not write worker heartbeat to database: %s", exc)
    finally:
        if conn is not None:
            conn.close()


def run_forever() -> None:
    interval = max(10, min(60, int(_safe_float(os.getenv("PAPER_WORKER_INTERVAL_SECONDS", "15"), 15))))
    log.info("Paper worker started; database_backend=%s signal_source=%s mode=%s live_trading=%s",
             backend_name(), os.getenv("PAPER_SIGNAL_SOURCE", "internal").lower(),
             os.getenv("TRADING_MODE", "paper").lower(), _truthy("LIVE_TRADING_ENABLED", False))
    while True:
        try:
            result = run_once()
            _write_heartbeat(result)
            # Retrospective counterfactual lookups happen only after the current
            # decision cycle has completed, never before processing a fresh alert.
            # Limit to one public result lookup per idle/monitoring cycle to keep
            # signal latency and public API load bounded.
            if result.get("action") in {"wait", "monitoring", "blocked", "error_wait"}:
                audit_conn = None
                try:
                    audit_conn = _connect()
                    _resolve_counterfactual_signal_outcomes(audit_conn, max_checks=1)
                except Exception as audit_exc:
                    log.warning("Counterfactual review skipped; no trade decision affected: %s", audit_exc)
                finally:
                    if audit_conn is not None:
                        audit_conn.close()
            log.info("Paper worker cycle: %s", result)
        except Exception as exc:
            log.exception("Unexpected worker-loop error; continuing without a trade")
            _write_heartbeat({"action": "error_wait", "reason": f"{type(exc).__name__}: {exc}"})
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    run_forever()
