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
from typing import Any

from candle_feed import fetch_signal_candles, fetch_closed_candles, CandleFeedError
from trend_stop import update_trend_stop
from signal_engine import MarketContext, evaluate_signal
from signal_fusion import evaluate_fusion
from btc_trend_monitor import build_trend_snapshot
from market_discovery import discover
from kalshi_api import get_market, get_orderbook, KalshiAPIError
from db_store import connect_database, ensure_schema, backend_name

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


def _extract_market(raw: dict) -> dict | None:
    m = raw.get("market", raw)
    if not isinstance(m, dict):
        return None
    ticker = m.get("ticker")
    status = str(m.get("status", "")).lower()
    title = " ".join(str(m.get(k, "")) for k in ("title", "subtitle", "event_ticker")).lower()
    if not ticker or not str(ticker).upper().startswith("KXBTC15M-"):
        return None
    strike = None
    for key in ("floor_strike", "strike_price", "custom_strike", "strike"):
        try:
            if m.get(key) is not None:
                strike = float(m[key])
                break
        except (TypeError, ValueError, OverflowError):
            pass
    if strike is None or not math.isfinite(strike) or strike <= 0:
        return None
    close = _parse_time(m.get("close_time") or m.get("expiration_time"))
    if close is None:
        return None
    seconds = int((close - _utcnow()).total_seconds())
    if seconds <= 0 or seconds > 900:
        return None
    if "up" not in title or ("btc" not in title and "bitcoin" not in title):
        return None
    if status in {"settled", "finalized", "closed", "inactive"}:
        return None
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
    if not 0 < float(entry) < 100:
        raise ValueError("take-profit entry price must be between 0 and 100 cents")
    if float(entry_fee) < 0 or float(exit_fee) < 0:
        raise ValueError("take-profit fees cannot be negative")
    if not 0 < float(target_return_percent) <= 200:
        raise ValueError("PAPER_TAKE_PROFIT_PERCENT must be greater than 0 and at most 200")
    entry_cost = float(entry) + float(entry_fee)
    return round(float(entry) + float(entry_fee) + float(exit_fee) + entry_cost * float(target_return_percent) / 100, 2)


def _take_profit_trailing_floor(target_cents: float, peak_bid_cents: float, trail_percent: float) -> float:
    if not 0 < float(target_cents) <= 100:
        raise ValueError("take-profit target must be between 0 and 100 cents")
    if not float(target_cents) <= float(peak_bid_cents) <= 100:
        raise ValueError("take-profit peak bid must be between target and 100 cents")
    if not 0 < float(trail_percent) <= 50:
        raise ValueError("PAPER_TAKE_PROFIT_TRAIL_PERCENT must be greater than 0 and at most 50")
    return round(max(float(target_cents), float(peak_bid_cents) * (1 - float(trail_percent) / 100)), 2)


def _safe_external_payload(raw_payload: str) -> dict[str, Any]:
    try:
        body = json.loads(raw_payload)
    except (TypeError, ValueError):
        return {}
    if not isinstance(body, dict):
        return {}
    for key in list(body):
        if key.lower() in {"secret", "token", "api_key", "authorization", "password"}:
            body.pop(key, None)
    return body


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
                conn.execute("UPDATE gainzalgo_signals SET processed_at=?,processing_status='ignored',processing_note=? WHERE id=?",
                             (now.isoformat(), f"source alert timestamp is stale/invalid; age_seconds={source_age:.1f}", signal_id))
                conn.commit()
                continue
        if age < 0 or age > max_age:
            conn.execute("UPDATE gainzalgo_signals SET processed_at=?,processing_status='ignored',processing_note=? WHERE id=?",
                         (now.isoformat(), f"received alert is stale/invalid; age_seconds={age:.1f}", signal_id))
            conn.commit()
            continue
        symbol_text = str(symbol or "").upper()
        if not symbol_text or ("BTC" not in symbol_text and "BITCOIN" not in symbol_text):
            conn.execute("UPDATE gainzalgo_signals SET processed_at=?,processing_status='ignored',processing_note='symbol missing or not BTC' WHERE id=?",
                         (now.isoformat(), signal_id)); conn.commit(); continue
        timeframe_text = str(timeframe or "").lower().replace(" ", "")
        if timeframe_text and timeframe_text not in {"1", "1m", "1min", "1minute", "1minutes", "15", "15m", "15min", "15minute", "15minutes"}:
            conn.execute("UPDATE gainzalgo_signals SET processed_at=?,processing_status='ignored',processing_note=? WHERE id=?",
                         (now.isoformat(), "unsupported timeframe; expected 1m or 15m", signal_id)); conn.commit(); continue
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
            conn.execute("UPDATE gainzalgo_signals SET processed_at=?,processing_status='ignored',processing_note='unrecognized direction' WHERE id=?",
                         (now.isoformat(), signal_id)); conn.commit(); continue
        if not timeframe_text:
            log.warning("Alert %s did not include timeframe; verify chart/alert configuration", signal_id)
        return {"id": signal_id, "created_at": created_at, "direction": direction,
                "symbol": symbol, "timeframe": timeframe_text or None, "payload_json": payload_json,
                "processing_status": processing_status or "queued", "age_seconds": round(age, 2)}, direction
    return None, None


def _mark_gainzalgo(conn, signal_id: int, status: str, note: str) -> None:
    conn.execute(
        "UPDATE gainzalgo_signals SET processed_at=?,processing_status=?,processing_note=? WHERE id=? AND processed_at IS NULL",
        (_utcnow().isoformat(), status, str(note)[:500], signal_id),
    )
    conn.commit()


def _record_signal_log(conn, *, created_at: str, ticker: str | None, direction: str, score: int,
                       btc_price: float, strike: float, seconds_remaining: int,
                       max_entry_cents: float | None, payload: dict[str, Any]) -> int | None:
    """Persist one evaluation and return its ID so an opened trade can reference it."""
    cur = conn.execute(
        """INSERT INTO signal_log (created_at,market_ticker,direction,score,btc_price,strike_price,
           seconds_remaining,max_entry_cents,payload_json) VALUES (?,?,?,?,?,?,?,?,?)""",
        (created_at, ticker, direction, int(score), float(btc_price), float(strike), int(seconds_remaining),
         max_entry_cents, json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False)),
    )
    conn.commit()
    return cur.lastrowid


def _log_wait(conn, reason: str, *, ticker: str | None = None, external: dict | None = None,
              internal_payload: dict | None = None) -> None:
    """Keep a visible audit entry when an alert is received but no trade is opened."""
    external = external or {}
    internal_payload = internal_payload or {}
    btc = float(internal_payload.get("btc_price") or 0.01)
    strike = float(internal_payload.get("strike_price") or btc)
    seconds = int(internal_payload.get("seconds_remaining") or 0)
    score = int(internal_payload.get("score") or 0)
    payload = {
        **internal_payload,
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
    """Return (configured hard cap, effective target cap) in cents.

    The hard cap preserves the existing safety setting. The separate target cap
    defaults to 80c so we do not chase expensive contracts while the model lacks
    a calibrated per-signal probability. The effective entry ceiling is the lower
    of those two limits.
    """
    hard_cap = max(1.0, min(99.0, _safe_float(os.getenv("PAPER_MAX_ENTRY_CENTS", "92"), 92)))
    target_cap = max(1.0, min(99.0, _safe_float(os.getenv("PAPER_ENTRY_TARGET_MAX_CENTS", "80"), 80)))
    return hard_cap, min(hard_cap, target_cap)


def _entry_price_gate(current_ask_cents: float, hard_cap_cents: float, target_cap_cents: float) -> tuple[str, str]:
    """Fail-closed price discipline: enter only at/below the active target cap.

    An expensive quote should wait, not be treated as an entry or as evidence of
    an edge. Calling code still enforces signal freshness, market validity, and spread.
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
    if ask > target:
        return "wait", f"Current ask {ask:.2f}c is above the entry target {target:.2f}c; waiting for a better price instead of chasing."
    return "enter", f"Current ask {ask:.2f}c is at or below the entry target {target:.2f}c."


def _set_gainzalgo_pending(conn, signal_id: int, status: str, note: str) -> None:
    """Record a pending status without setting processed_at, so a fresh alert can retry."""
    conn.execute(
        "UPDATE gainzalgo_signals SET processing_status=?,processing_note=? WHERE id=? AND processed_at IS NULL",
        (str(status)[:80], str(note)[:500], signal_id),
    )
    conn.commit()


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
            if tp_enabled:
                if tp_target is None:
                    tp_target = _take_profit_target_cents(float(entry), float(entry_fee), exit_fee, tp_pct)
                    conn.execute("UPDATE paper_trades SET take_profit_target_cents=? WHERE id=?", (tp_target, trade_id))
                    conn.commit()
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
            if due and (trend_enabled or shadow_enabled or (tp_enabled and tp_armed and reversal_enabled)):
                conn.execute("UPDATE paper_trades SET last_trend_check_at=? WHERE id=?", (now.isoformat(), trade_id))
                conn.commit()
                try:
                    bundle = _get_signal_bundle_for_evaluation() if (tp_enabled and tp_armed and reversal_enabled) else None
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
                    if tp_enabled and tp_armed and reversal_enabled and fresh_candles and candle_ts != tp_last_reversal_ts and tp_target is not None and tp_target <= 100:
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
                                continue
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
                            conn.execute("UPDATE paper_trades SET take_profit_last_reversal_candle_timestamp=? WHERE id=?", (candle_ts, trade_id))
                            conn.commit()
                            if veto and not tp_triggered:
                                tp_triggered = True
                                tp_reason = "major opposing setup after take-profit activation: " + "; ".join(veto_reasons)
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

            if (trend_triggered and trend_enabled) or fixed_triggered or tp_triggered:
                pnl = (float(bid) - float(entry) - float(entry_fee) - exit_fee) * int(contracts)
                if trend_triggered and trend_enabled:
                    outcome, note = "TREND_STOP_LOSS", f" | simulated chart-stop exit at bid {bid:.2f}c; {trend_reason}"
                elif fixed_triggered:
                    outcome, note = "STOP_LOSS", f" | simulated fixed-stop exit at bid {bid:.2f}c"
                else:
                    outcome, note = "TAKE_PROFIT", f" | simulated trailing take-profit at bid {bid:.2f}c; {tp_reason}; target={tp_target}c"
                conn.execute("UPDATE paper_trades SET settled_at=?,outcome=?,pnl_cents=?,notes=COALESCE(notes,'') || ? WHERE id=?",
                             (_utcnow().isoformat(), outcome, pnl, note, trade_id))
                conn.commit()
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
    try:
        conn = _connect()
        _manage_open(conn)
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
            return {"action": "blocked", "reason": "daily loss budget reached including open-position risk",
                    "realized_pnl_cents": round(realized, 2), "open_risk_cents": round(open_cost, 2)}
        daily_cap = max(1, min(96, int(_safe_float(os.getenv("PAPER_MAX_TRADES_PER_DAY", "96"), 96))))
        if daily_count >= daily_cap:
            return {"action": "wait", "reason": "paper daily trade cap reached"}

        if conn.execute("SELECT 1 FROM paper_trades WHERE settled_at IS NULL LIMIT 1").fetchone():
            if queued:
                _mark_gainzalgo(conn, queued["id"], "skipped", "an open paper position already exists")
            return {"action": "monitoring", "reason": "an open paper position already exists"}
        if use_external and not queued:
            return {"action": "wait", "reason": f"waiting for a fresh TradingView/GainzAlgo alert; source={source}", "source": source}

        found = discover()
        candidates = found.get("candidates", []) if isinstance(found, dict) else []
        parsed: list[dict] = []
        for candidate in candidates:
            ticker = candidate.get("ticker") if isinstance(candidate, dict) else None
            if not ticker:
                continue
            try:
                extracted = _extract_market(get_market(str(ticker)))
                if extracted:
                    parsed.append(extracted)
            except (KalshiAPIError, ValueError, TypeError) as exc:
                log.warning("Market lookup failed for %s: %s", ticker, exc)
        if not parsed:
            if queued:
                # Keep a fresh external alert unprocessed while market discovery can retry; it is still age-limited.
                _log_wait(conn, "No candidate had an explicit strike, valid 15m close time, and verified BTC-up title.", external=queued)
            return {"action": "wait", "reason": "no candidate had an explicit strike, valid 15m close time, and verified BTC-up market title"}
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
            return {"action": "wait", "reason": "current orderbook is missing a usable YES/NO best bid to derive asks", "ticker": market["ticker"]}
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
                _log_wait(conn, note, ticker=market["ticker"], external=queued)
                return {"action": "wait", "reason": note, "ticker": market["ticker"], "spread_cents": spread}

            price_action, price_note = _entry_price_gate(entry, hard_max_entry, max_entry)
            if price_action != "enter":
                note = price_note
                _set_gainzalgo_pending(conn, queued["id"], "waiting_for_better_price", note)
                max_age_config = os.getenv("TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS", os.getenv("GAINZALGO_MAX_SIGNAL_AGE_SECONDS", "300"))
                max_age = max(30, min(900, int(_safe_float(max_age_config, 300))))
                remaining = max(0.0, max_age - float(queued.get("age_seconds", 0) or 0))
                return {"action": "wait_for_better_price", "reason": note, "ticker": market["ticker"],
                        "direction": direction, "entry_ask_cents": entry, "entry_target_max_cents": max_entry,
                        "signal_age_seconds": queued.get("age_seconds"), "signal_freshness_seconds_remaining": round(remaining, 1),
                        "trade_placed": False, "mode": "SIMULATION_ONLY"}

            if source == "hybrid":
                bundle = _get_signal_bundle_for_evaluation()
                internal_signal = evaluate_signal(
                    bundle["candles_1m"], bundle["candles_5m"], bundle["candles_15m"],
                    MarketContext(strike_price=market["strike"], seconds_remaining=market["seconds"],
                                  up_ask_cents=yes_ask, down_ask_cents=no_ask, spread_cents=spread,
                                  estimated_fee_cents=fee),
                    minimum_score=min_score, max_entry_cents=max_entry, max_spread_cents=max_spread,
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
                    fee_cents_per_contract=fee, max_entry_cents=max_entry, max_spread_cents=max_spread,
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
                    },
                    "reasons": list(fused.get("reasons", [])) + [f"Internal diagnostics: {r}" for r in internal_payload.get("reasons", [])],
                    "warnings": list(internal_payload.get("warnings", [])) + list(fused.get("warnings", [])),
                    "final_decision_reason": "; ".join(fused.get("reasons", [])),
                    "disclaimer": "TradingView primary; internal rules veto only major conflicts. Score is not a win probability. Paper mode only.",
                }
                entry_signal_log_id = _record_signal_log(
                    conn, created_at=now_text, ticker=market["ticker"], direction=final_direction,
                    score=score, btc_price=entry_btc, strike=market["strike"],
                    seconds_remaining=market["seconds"], max_entry_cents=max_entry, payload=payload,
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
                    seconds_remaining=market["seconds"], max_entry_cents=max_entry, payload=payload,
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
                minimum_score=min_score, max_entry_cents=max_entry, max_spread_cents=max_spread,
            )
            payload = signal.to_dict()
            payload["source"] = "internal"
            entry_btc = float(signal.btc_price)
            entry_candle_timestamp = str(bundle["candles_1m"]["timestamp"].iloc[-1]) if "timestamp" in bundle["candles_1m"].columns else _utcnow().isoformat()
            score = int(signal.score)
            if signal.direction == "WAIT":
                _record_signal_log(conn, created_at=now_text, ticker=market["ticker"], direction="WAIT", score=score,
                                   btc_price=signal.btc_price, strike=signal.strike_price,
                                   seconds_remaining=signal.seconds_remaining, max_entry_cents=max_entry, payload=payload)
                return {"action": "wait", "ticker": market["ticker"], "score": signal.score,
                        "reasons": signal.reasons, "warnings": signal.warnings}
            direction = "UP" if signal.direction == "BUY_UP" else "DOWN"
            entry = float(yes_ask if direction == "UP" else no_ask)
            final_direction = signal.direction
            warnings = signal.warnings
            entry_signal_log_id = _record_signal_log(
                conn, created_at=now_text, ticker=market["ticker"], direction=final_direction,
                score=score, btc_price=signal.btc_price, strike=signal.strike_price,
                seconds_remaining=signal.seconds_remaining, max_entry_cents=signal.max_entry_cents, payload=payload,
            )
            entry_signal_score = score

        if entry is None:
            return {"action": "wait", "reason": "candidate entry price was not set; fail closed"}
        price_action, price_note = _entry_price_gate(float(entry), hard_max_entry, max_entry)
        if price_action != "enter":
            wait_payload = dict(payload or {})
            wait_payload["entry_price_gate"] = {"decision": "wait_for_better_price", "current_ask_cents": float(entry),
                                                 "target_max_cents": max_entry, "reason": price_note}
            wait_payload["reasons"] = list(wait_payload.get("reasons", [])) + [price_note]
            _record_signal_log(conn, created_at=_utcnow().isoformat(), ticker=market["ticker"], direction="WAIT",
                               score=score, btc_price=entry_btc, strike=market["strike"],
                               seconds_remaining=market["seconds"], max_entry_cents=max_entry, payload=wait_payload)
            return {"action": "wait_for_better_price", "reason": price_note, "ticker": market["ticker"],
                    "entry_ask_cents": float(entry), "entry_target_max_cents": max_entry,
                    "trade_placed": False, "mode": "SIMULATION_ONLY"}
        contracts = max(0, min(10, int((max_trade * 100) // (float(entry) + fee))))
        if contracts < 1:
            reason = "even one contract plus fees exceeds MAX_TRADE_DOLLARS"
            if queued:
                _mark_gainzalgo(conn, queued["id"], "skipped", reason)
            _log_wait(conn, reason, ticker=market["ticker"], external=queued, internal_payload=internal_payload or {})
            return {"action": "blocked", "reason": reason, "entry_cents": entry, "fee_cents": fee}
        if realized - open_cost - contracts * (float(entry) + fee) <= -max_loss * 100:
            reason = "trade would exceed daily loss budget when open risk is reserved"
            if queued:
                _mark_gainzalgo(conn, queued["id"], "skipped", reason)
            return {"action": "blocked", "reason": reason}

        stop = max(1.0, float(entry) * (1 - stop_pct / 100)) if _truthy("PAPER_STOP_LOSS_ENABLED", False) else None
        tp_target = _take_profit_target_cents(float(entry), fee, exit_fee, tp_pct) if tp_enabled else None
        signal_source = "hybrid" if source == "hybrid" else ("TradingView" if use_external else "internal")
        external_id = queued["id"] if queued else None
        note = (f"TradingView-primary hybrid paper entry; signal_id={external_id}" if source == "hybrid" else
                f"TradingView/GainzAlgo paper entry; signal_id={external_id}" if use_external else
                "Internal strategy paper entry; heuristic evidence score is not a win probability")
        timeframe_note = queued.get("timeframe") if queued else None
        conn.execute(
            """INSERT INTO paper_trades (created_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,
               notes,stop_loss_cents,entry_btc_price,high_water_btc,low_water_btc,last_stop_candle_timestamp,
               take_profit_target_cents,entry_signal_source,external_signal_id,timeframe,
               entry_signal_log_id,entry_signal_score)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now_text, market["ticker"], direction, contracts, float(entry), fee,
             note + f" | entry target <= {max_entry:.2f}c (configured hard cap {hard_max_entry:.2f}c)", stop,
             entry_btc, entry_btc, entry_btc, entry_candle_timestamp, tp_target, signal_source, external_id,
             timeframe_note or "15m contract; TV timeframe unknown", entry_signal_log_id, entry_signal_score),
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
                "entry_target_max_cents": max_entry, "configured_hard_max_entry_cents": hard_max_entry,
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
            log.info("Paper worker cycle: %s", result)
        except Exception as exc:
            log.exception("Unexpected worker-loop error; continuing without a trade")
            _write_heartbeat({"action": "error_wait", "reason": f"{type(exc).__name__}: {exc}"})
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    run_forever()
