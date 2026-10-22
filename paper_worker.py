"""Paper-only Kalshi BTC 15m worker with TradingView-primary hybrid and take-profit controls.

No authenticated endpoints and no real orders are used. The worker fails closed
unless the candidate market has an explicit strike and valid timing/prices.
"""
from __future__ import annotations
import json, os, sqlite3, time, logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from candle_feed import fetch_signal_candles, fetch_closed_candles, CandleFeedError
from trend_stop import update_trend_stop
from signal_engine import MarketContext, evaluate_signal
from market_discovery import discover
from kalshi_api import get_market, get_orderbook, KalshiAPIError

log = logging.getLogger("paper_worker")
DB_PATH = Path(os.getenv("BOT_DB_PATH", str(Path(__file__).resolve().parent / "bot_journal.sqlite3")))


def _utcnow(): return datetime.now(timezone.utc)

def _parse_time(value: Any):
    if not isinstance(value, str) or not value.strip(): return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError): return None

def _num(obj: dict, keys: tuple[str, ...], *, dollars_keys: tuple[str, ...] = ()) -> float | None:
    for key in dollars_keys:
        val = obj.get(key)
        if val is not None:
            try: return float(val) * 100
            except (ValueError, TypeError): pass
    for key in keys:
        val = obj.get(key)
        if val is not None:
            try: return float(val)
            except (ValueError, TypeError): pass
    return None

def _price(m: dict, side: str, kind: str) -> float | None:
    # API *_dollars fields are dollars; bare yes_ask/no_ask fields are cents.
    return _num(m, (f"{side}_{kind}",), dollars_keys=(f"{side}_{kind}_dollars",))

def _extract_market(raw: dict) -> dict | None:
    m = raw.get("market", raw)
    if not isinstance(m, dict): return None
    ticker = m.get("ticker")
    status = str(m.get("status", "")).lower()
    title = " ".join(str(m.get(k, "")) for k in ("title", "subtitle", "event_ticker")).lower()
    # Only support the intended 15m BTC direction series, not arbitrary BTC markets.
    if not ticker or not str(ticker).upper().startswith("KXBTC15M-"): return None
    # Explicit strike only. Never infer the strike from current BTC or ticker text.
    strike = None
    for key in ("floor_strike", "strike_price", "custom_strike", "strike"):
        try:
            if m.get(key) is not None: strike = float(m[key]); break
        except (TypeError, ValueError): pass
    if strike is None or strike <= 0: return None
    close = _parse_time(m.get("close_time") or m.get("expiration_time"))
    if close is None: return None
    seconds = int((close - _utcnow()).total_seconds())
    if seconds <= 0 or seconds > 900: return None
    # We only trade contracts whose displayed title clearly describes BTC going up.
    if "up" not in title or "btc" not in title and "bitcoin" not in title: return None
    return {"raw": m, "ticker": str(ticker), "strike": strike, "seconds": seconds,
            "yes_ask": _price(m, "yes", "ask"), "no_ask": _price(m, "no", "ask"),
            "status": status, "title": title}


def _top_bid(book: dict, side: str) -> float | None:
    """Return the highest bid in cents from Kalshi's order book."""
    if not isinstance(book, dict):
        return None

    # Kalshi may wrap the order book in either of these keys.
    ob = book.get("orderbook_fp", book.get("orderbook", book))

    if not isinstance(ob, dict):
        return None

    values = []

    for key in (side, f"{side}_dollars"):
        levels = ob.get(key)

        if not isinstance(levels, list):
            continue

        for level in levels:
            try:
                price = float(level[0])

                # Convert dollar prices to cents.
                if key.endswith("_dollars"):
                    price *= 100

                if 0 <= price <= 100:
                    values.append(price)

            except (ValueError, TypeError, IndexError):
                continue

    return max(values) if values else None


def _ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, settled_at TEXT,
      market_ticker TEXT NOT NULL, direction TEXT NOT NULL, contracts INTEGER NOT NULL,
      entry_cents REAL NOT NULL, fee_cents_per_contract REAL NOT NULL, outcome TEXT,
      pnl_cents REAL, notes TEXT, stop_loss_cents REAL)""")
    cols={r[1] for r in conn.execute("PRAGMA table_info(paper_trades)")}
    migrations = (
        ("stop_loss_cents", "REAL"),
        ("entry_btc_price", "REAL"),
        ("high_water_btc", "REAL"),
        ("low_water_btc", "REAL"),
        ("trailing_stop_btc", "REAL"),
        ("last_stop_candle_timestamp", "TEXT"),
        ("last_trend_check_at", "TEXT"),
        ("trend_stop_shadow_triggered_at", "TEXT"),
        ("trend_stop_shadow_trigger_bid_cents", "REAL"),
        ("trend_stop_shadow_pnl_cents", "REAL"),
        ("trend_stop_shadow_reason", "TEXT"),
        ("trend_stop_shadow_candle_timestamp", "TEXT"),
        ("take_profit_target_cents", "REAL"),
        ("entry_signal_source", "TEXT"),
        ("external_signal_id", "INTEGER"),
    )
    for col, definition in migrations:
        if col not in cols:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {definition}")
    conn.execute("""CREATE TABLE IF NOT EXISTS signal_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, market_ticker TEXT,
      direction TEXT NOT NULL, score INTEGER NOT NULL, btc_price REAL NOT NULL,
      strike_price REAL NOT NULL, seconds_remaining INTEGER NOT NULL, max_entry_cents REAL,
      payload_json TEXT NOT NULL)""")
    # The webhook creates this table; these columns make each alert a one-time queue item.
    conn.execute("""CREATE TABLE IF NOT EXISTS gainzalgo_signals (
      id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
      direction TEXT NOT NULL, symbol TEXT, timeframe TEXT, payload_json TEXT NOT NULL,
      processed_at TEXT, processing_status TEXT, processing_note TEXT)""")
    signal_cols={r[1] for r in conn.execute("PRAGMA table_info(gainzalgo_signals)")}
    for col, definition in (("processed_at", "TEXT"), ("processing_status", "TEXT"), ("processing_note", "TEXT")):
        if col not in signal_cols:
            conn.execute(f"ALTER TABLE gainzalgo_signals ADD COLUMN {col} {definition}")
    conn.commit()

def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c=sqlite3.connect(DB_PATH, timeout=15)
    c.execute("PRAGMA busy_timeout=15000")
    _ensure_schema(c)
    return c

def _limits(conn, max_trade_dollars, max_daily_loss_dollars):
    today=_utcnow().date().isoformat()
    realized=float(conn.execute("SELECT COALESCE(SUM(pnl_cents),0) FROM paper_trades WHERE settled_at IS NOT NULL AND substr(settled_at,1,10)=?",(today,)).fetchone()[0])
    # Reserve the entire entry cost of open positions as worst-case loss budget.
    open_cost=float(conn.execute("SELECT COALESCE(SUM(contracts*(entry_cents+fee_cents_per_contract)),0) FROM paper_trades WHERE settled_at IS NULL").fetchone()[0])
    count=int(conn.execute("SELECT COUNT(*) FROM paper_trades WHERE substr(created_at,1,10)=?",(today,)).fetchone()[0])
    return realized, open_cost, count, (realized - open_cost <= -max_daily_loss_dollars*100)

def _take_profit_target_cents(
    entry_cents: float,
    entry_fee_cents: float,
    exit_fee_cents: float,
    target_return_percent: float,
) -> float:
    """Bid needed to hit a net return target after both entry and exit fees.

    The target percentage is measured against entry price plus entry fee per
    contract. The target is not clamped to 100c: a target over 100c is simply
    unreachable for a binary Kalshi contract and will never trigger.
    """
    entry = float(entry_cents)
    entry_fee = float(entry_fee_cents)
    exit_fee = float(exit_fee_cents)
    percent = float(target_return_percent)
    if not 0 < entry < 100:
        raise ValueError("take-profit entry price must be between 0 and 100 cents")
    if entry_fee < 0 or exit_fee < 0:
        raise ValueError("take-profit fees cannot be negative")
    if not 0 < percent <= 200:
        raise ValueError("PAPER_TAKE_PROFIT_PERCENT must be greater than 0 and at most 200")
    entry_cost = entry + entry_fee
    return round(entry + entry_fee + exit_fee + entry_cost * percent / 100.0, 2)


def _major_flaw_veto(
    direction: str,
    internal_payload: dict[str, Any],
    minimum_opposing_points: int = 4,
) -> tuple[bool, list[str]]:
    """Only veto an external TradingView signal for a strong internal conflict.

    Ordinary internal WAITs, low heuristic scores, mixed timeframes, RSI
    warnings, or an unconfirmed box do not override the external indicator.
    """
    wanted = 1 if str(direction).upper() == "UP" else -1
    strategies = internal_payload.get("strategies") or {}
    reasons: list[str] = []

    trends = (strategies.get("multi_timeframe_trend") or {}).get("timeframes") or {}
    trend_5m = trends.get("5m")
    trend_15m = trends.get("15m")
    totals = strategies.get("evidence_totals") or {}
    up_points = int(totals.get("up_points", 0) or 0)
    down_points = int(totals.get("down_points", 0) or 0)
    wanted_points = up_points if wanted > 0 else down_points
    opposing_points = down_points if wanted > 0 else up_points
    margin = opposing_points - wanted_points

    if trend_5m == -wanted and trend_15m == -wanted and margin >= int(minimum_opposing_points):
        side_text = "bearish" if wanted > 0 else "bullish"
        reasons.append(
            f"Major conflict: both the 5-minute and 15-minute trends are {side_text}, "
            f"and opposing internal evidence leads by {margin} points (required: "
            f"{minimum_opposing_points})."
        )

    box = strategies.get("box_theory") or {}
    structure = strategies.get("market_structure") or {}
    volume = strategies.get("volume_confirmation") or {}
    box_direction = box.get("direction", 0)
    if (
        box_direction == -wanted
        and box.get("state") in {"bullish_breakout", "bearish_breakout"}
        and bool(volume.get("confirmed"))
        and structure.get("direction") == -wanted
    ):
        opposite_breakout = "bearish" if wanted > 0 else "bullish"
        reasons.append(
            f"Major conflict: a volume-confirmed {opposite_breakout} box breakout "
            "also agrees with opposing market structure."
        )

    return bool(reasons), reasons


def _safe_external_payload(raw_payload: str) -> dict[str, Any]:
    """Keep the dashboard explanation but never store or display the webhook secret."""
    try:
        body = json.loads(raw_payload)
    except (TypeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        return {}
    body.pop("secret", None)
    return body


def _record_signal_log(
    conn,
    *,
    created_at: str,
    ticker: str,
    direction: str,
    score: int,
    btc_price: float,
    strike: float,
    seconds_remaining: int,
    max_entry_cents: float | None,
    payload: dict[str, Any],
) -> None:
    conn.execute(
        """INSERT INTO signal_log
           (created_at,market_ticker,direction,score,btc_price,strike_price,
            seconds_remaining,max_entry_cents,payload_json)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (created_at, ticker, direction, int(score), float(btc_price), float(strike),
         int(seconds_remaining), max_entry_cents, json.dumps(payload, default=str)),
    )
    conn.commit()


def _manage_open(conn, max_daily_loss_dollars: float):
    rows = conn.execute(
        "SELECT id,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,stop_loss_cents, "
        "entry_btc_price,high_water_btc,low_water_btc,trailing_stop_btc,last_stop_candle_timestamp,last_trend_check_at, "
        "trend_stop_shadow_triggered_at,take_profit_target_cents "
        "FROM paper_trades WHERE settled_at IS NULL"
    ).fetchall()
    trend_enabled = os.getenv("PAPER_TREND_STOP_LOSS_ENABLED", "false").lower() == "true"
    shadow_enabled = os.getenv("PAPER_TREND_STOP_SHADOW_MODE", "false").lower() == "true"
    take_profit_enabled = os.getenv("PAPER_TAKE_PROFIT_ENABLED", "false").lower() == "true"
    take_profit_percent = float(os.getenv("PAPER_TAKE_PROFIT_PERCENT", "20"))
    exit_fee = max(0.0, float(os.getenv("PAPER_EXIT_FEE_CENTS_PER_CONTRACT", "2.0")))
    check_seconds = max(30.0, min(180.0, float(os.getenv("PAPER_TREND_STOP_CHECK_SECONDS", "45"))))
    atr_multiplier = float(os.getenv("PAPER_TREND_STOP_ATR_MULTIPLIER", "2.5"))
    swing_lookback = max(8, min(100, int(os.getenv("PAPER_TREND_STOP_SWING_LOOKBACK", "30"))))

    for row in rows:
        (trade_id, ticker, direction, contracts, entry, fee, static_stop,
         entry_btc, high_water, low_water, trailing_stop, last_candle_ts,
         last_check_at, shadow_triggered_at, stored_tp_target) = row
        current_stop_candle_ts = None
        try:
            # An official Kalshi settlement always takes precedence over simulated exits.
            market_payload = get_market(ticker)
            market = market_payload.get("market", market_payload)
            status = str(market.get("status", "")).lower() if isinstance(market, dict) else ""
            result = str(market.get("result", "")).lower() if isinstance(market, dict) else ""
            if status in {"settled", "finalized"} and result in {"yes", "no"}:
                outcome = "UP" if result == "yes" else "DOWN"
                gross = 100 * int(contracts) if direction == outcome else 0
                pnl = gross - float(entry) * int(contracts) - float(fee) * int(contracts)
                conn.execute(
                    "UPDATE paper_trades SET settled_at=?,outcome=?,pnl_cents=?,notes=COALESCE(notes,'') || ? WHERE id=?",
                    (_utcnow().isoformat(), outcome, pnl,
                     " | automatically settled from official Kalshi final result", trade_id),
                )
                conn.commit()
                log.info("Paper trade %s settled from official market result %s", trade_id, result)
                continue

            orderbook = get_orderbook(ticker)
            bid = _top_bid(orderbook, "yes" if direction == "UP" else "no")
            if bid is None:
                continue

            tp_target = None
            tp_triggered = False
            if take_profit_enabled:
                tp_target = float(stored_tp_target) if stored_tp_target is not None else _take_profit_target_cents(
                    entry, fee, exit_fee, take_profit_percent
                )
                if stored_tp_target is None:
                    conn.execute(
                        "UPDATE paper_trades SET take_profit_target_cents=? WHERE id=?",
                        (tp_target, trade_id),
                    )
                    conn.commit()
                # A target over 100 cents is not attainable for a binary contract.
                tp_triggered = tp_target <= 100.0 and float(bid) >= tp_target

            trend_triggered = False
            trend_reason = None
            now_dt = _utcnow()
            last_check = _parse_time(last_check_at)
            due = last_check is None or (now_dt - last_check).total_seconds() >= check_seconds
            shadow_already_recorded = bool(shadow_triggered_at)
            should_monitor_trend = trend_enabled or (shadow_enabled and not shadow_already_recorded)
            if should_monitor_trend and entry_btc is not None and due:
                # Persist throttle before the request so feed errors do not hammer Binance.
                conn.execute(
                    "UPDATE paper_trades SET last_trend_check_at=? WHERE id=?",
                    (now_dt.isoformat(), trade_id),
                )
                conn.commit()
                try:
                    candles = fetch_closed_candles("1m", 100)
                    candle_ts = str(candles["timestamp"].iloc[-1])
                    current_stop_candle_ts = candle_ts
                    if candle_ts != last_candle_ts:
                        update = update_trend_stop(
                            candles, direction,
                            high_water_btc=float(high_water) if high_water is not None else float(entry_btc),
                            low_water_btc=float(low_water) if low_water is not None else float(entry_btc),
                            trailing_stop_btc=float(trailing_stop) if trailing_stop is not None else None,
                            atr_multiplier=atr_multiplier,
                            swing_lookback=swing_lookback,
                        )
                        conn.execute(
                            """UPDATE paper_trades
                               SET high_water_btc=?,low_water_btc=?,trailing_stop_btc=?,last_stop_candle_timestamp=?
                               WHERE id=?""",
                            (update["high_water_btc"], update["low_water_btc"],
                             update["trailing_stop_btc"], candle_ts, trade_id),
                        )
                        conn.commit()
                        trailing_stop = update["trailing_stop_btc"]
                        trend_reason = update["reason"]
                        trend_triggered = bool(update["triggered"])
                        log.info(
                            "Paper trade %s chart-stop update: %s; stop_enabled=%s; shadow_mode=%s; trigger=%s",
                            trade_id, trend_reason, trend_enabled, shadow_enabled, trend_triggered,
                        )
                    elif trailing_stop is not None:
                        last_close = float(candles["close"].iloc[-1])
                        trend_triggered = (
                            (direction == "UP" and last_close <= float(trailing_stop))
                            or (direction == "DOWN" and last_close >= float(trailing_stop))
                        )
                        if trend_triggered:
                            trend_reason = (
                                f"Previously stored {direction} chart stop {float(trailing_stop):.2f} "
                                f"breached by latest closed BTC candle {last_close:.2f}"
                            )
                except (CandleFeedError, ValueError, TypeError, KeyError) as exc:
                    log.warning(
                        "Trend stop check unavailable for paper trade %s; no chart-stop exit: %s",
                        trade_id, exc,
                    )

            static_triggered = (
                os.getenv("PAPER_STOP_LOSS_ENABLED", "false").lower() == "true"
                and static_stop is not None and float(bid) <= float(static_stop)
            )

            # Shadow mode records hypothetical exits only; it does not close the position.
            if trend_triggered and shadow_enabled:
                shadow_pnl = (float(bid) - float(entry) - float(fee) - exit_fee) * int(contracts)
                shadow_now = _utcnow().isoformat()
                cur = conn.execute(
                    """UPDATE paper_trades
                       SET trend_stop_shadow_triggered_at=?,trend_stop_shadow_trigger_bid_cents=?,
                           trend_stop_shadow_pnl_cents=?,trend_stop_shadow_reason=?,
                           trend_stop_shadow_candle_timestamp=?
                       WHERE id=? AND trend_stop_shadow_triggered_at IS NULL""",
                    (shadow_now, float(bid), shadow_pnl, trend_reason, current_stop_candle_ts, trade_id),
                )
                conn.commit()
                if cur.rowcount:
                    log.info(
                        "Paper trade %s TREND STOP SHADOW ONLY: would exit at Kalshi bid %.2fc; "
                        "hypothetical pnl %.2fc; trade remains OPEN; %s",
                        trade_id, float(bid), shadow_pnl, trend_reason,
                    )

            # Loss controls take priority over the profit target if more than one condition fires.
            if (trend_triggered and trend_enabled) or static_triggered or tp_triggered:
                pnl = (float(bid) - float(entry) - float(fee) - exit_fee) * int(contracts)
                now = _utcnow().isoformat()
                if trend_triggered and trend_enabled:
                    outcome = "TREND_STOP_LOSS"
                    note = (
                        f" | paper trend stop triggered; {trend_reason}; simulated Kalshi exit bid "
                        f"{bid:.2f}c; no real order placed"
                    )
                elif static_triggered:
                    outcome = "STOP_LOSS"
                    note = f" | paper fixed stop triggered; observed Kalshi bid {bid:.2f}c; simulated exit only"
                else:
                    outcome = "TAKE_PROFIT"
                    note = (
                        f" | paper take-profit triggered; target {float(tp_target):.2f}c; "
                        f"observed Kalshi exit bid {float(bid):.2f}c; target return {take_profit_percent:.2f}% "
                        "after fees; simulated exit only"
                    )
                conn.execute(
                    "UPDATE paper_trades SET settled_at=?,outcome=?,pnl_cents=?,notes=COALESCE(notes,'') || ? WHERE id=?",
                    (now, outcome, pnl, note, trade_id),
                )
                conn.commit()
                log.warning(
                    "Paper trade %s closed by %s at observed Kalshi bid %.2fc; simulated PnL %.2fc; no real order placed",
                    trade_id, outcome, bid, pnl,
                )
        except (KalshiAPIError, ValueError, TypeError, KeyError) as exc:
            log.warning("Could not monitor open paper trade %s: %s", trade_id, exc)

def _next_gainzalgo_signal(conn):
    """Consume one fresh, unprocessed GainzAlgo alert. Returns (row, mapped direction)."""
    max_age = max(30, min(900, int(os.getenv(
        "TRADINGVIEW_MAX_SIGNAL_AGE_SECONDS",
        os.getenv("GAINZALGO_MAX_SIGNAL_AGE_SECONDS", "300"),
    ))))
    now=_utcnow()
    rows=conn.execute("SELECT id,created_at,direction,symbol,timeframe,payload_json FROM gainzalgo_signals WHERE processed_at IS NULL ORDER BY id ASC LIMIT 20").fetchall()
    for row in rows:
        signal_id, created_at, raw_direction, symbol, timeframe, payload_json = row
        created=_parse_time(created_at)
        age=(now-created).total_seconds() if created else float("inf")
        if age < 0 or age > max_age:
            conn.execute("UPDATE gainzalgo_signals SET processed_at=?,processing_status='ignored',processing_note=? WHERE id=?",
                         (now.isoformat(), f"stale or invalid timestamp; age_seconds={age:.0f}", signal_id))
            conn.commit()
            continue
        symbol_text=str(symbol or "").upper()
        timeframe_text=str(timeframe or "").lower().replace(" ", "")
        if symbol_text and "BTC" not in symbol_text:
            conn.execute("UPDATE gainzalgo_signals SET processed_at=?,processing_status='ignored',processing_note='symbol is not BTC' WHERE id=?", (now.isoformat(), signal_id)); conn.commit(); continue
        if timeframe_text and timeframe_text not in {"15", "15m", "15min", "15minute", "15minutes"}:
            conn.execute("UPDATE gainzalgo_signals SET processed_at=?,processing_status='ignored',processing_note='timeframe is not 15m' WHERE id=?", (now.isoformat(), signal_id)); conn.commit(); continue
        d=str(raw_direction or "").upper()
        if d in {"BUY", "LONG", "UP"}: direction="UP"
        elif d in {"SELL", "SHORT", "DOWN"}: direction="DOWN"
        else:
            conn.execute("UPDATE gainzalgo_signals SET processed_at=?,processing_status='ignored',processing_note='unrecognized direction' WHERE id=?", (now.isoformat(), signal_id)); conn.commit(); continue
        return {
            "id": signal_id,
            "created_at": created_at,
            "direction": direction,
            "payload_json": payload_json,
            "age_seconds": round(age, 2),
        }, direction
    return None, None

def _mark_gainzalgo(conn, signal_id, status, note):
    conn.execute("UPDATE gainzalgo_signals SET processed_at=?,processing_status=?,processing_note=? WHERE id=? AND processed_at IS NULL",
                 (_utcnow().isoformat(), status, str(note)[:500], signal_id))
    conn.commit()

def run_once() -> dict:
    """Run one paper-only cycle; no authenticated or real order placement exists."""
    if os.getenv("TRADING_MODE", "paper").lower() != "paper":
        return {"action": "blocked", "reason": "TRADING_MODE must be paper"}
    if os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true":
        return {"action": "blocked", "reason": "This worker refuses to run when live trading is enabled"}

    max_trade = float(os.getenv("MAX_TRADE_DOLLARS", "1.00"))
    max_loss = float(os.getenv("MAX_DAILY_LOSS_DOLLARS", "10.00"))
    fee = float(os.getenv("PAPER_FEE_CENTS_PER_CONTRACT", "2.0"))
    exit_fee = max(0.0, float(os.getenv("PAPER_EXIT_FEE_CENTS_PER_CONTRACT", "2.0")))
    stop_pct = float(os.getenv("PAPER_STOP_LOSS_PERCENT", "35"))
    min_score = int(os.getenv("PAPER_MINIMUM_SCORE", "65"))
    max_entry = float(os.getenv("PAPER_MAX_ENTRY_CENTS", "78"))
    max_spread = float(os.getenv("PAPER_MAX_SPREAD_CENTS", "5"))
    take_profit_enabled = os.getenv("PAPER_TAKE_PROFIT_ENABLED", "false").lower() == "true"
    take_profit_percent = float(os.getenv("PAPER_TAKE_PROFIT_PERCENT", "20"))
    if take_profit_enabled:
        # Validate the target before opening a position; bad env values fail closed.
        try:
            _take_profit_target_cents(50.0, fee, exit_fee, take_profit_percent)
        except ValueError as exc:
            return {"action": "blocked", "reason": str(exc)}

    conn = _connect()
    try:
        _manage_open(conn, max_loss)
        realized, open_cost, count, halt = _limits(conn, max_trade, max_loss)
        if halt:
            return {
                "action": "blocked",
                "reason": "daily loss budget reached including open-position risk",
                "realized_pnl_cents": round(realized, 2),
                "open_risk_cents": round(open_cost, 2),
            }
        daily_cap = max(1, min(96, int(os.getenv("PAPER_MAX_TRADES_PER_DAY", "96"))))
        if count >= daily_cap:
            return {"action": "wait", "reason": "paper daily trade cap reached"}

        source = os.getenv("PAPER_SIGNAL_SOURCE", "internal").strip().lower()
        if source not in {"internal", "gainzalgo", "hybrid"}:
            return {"action": "blocked", "reason": "PAPER_SIGNAL_SOURCE must be internal, gainzalgo, or hybrid"}
        external_source = source in {"gainzalgo", "hybrid"}
        queued_signal, queued_direction = _next_gainzalgo_signal(conn) if external_source else (None, None)

        if conn.execute("SELECT 1 FROM paper_trades WHERE settled_at IS NULL LIMIT 1").fetchone():
            if queued_signal:
                _mark_gainzalgo(conn, queued_signal["id"], "skipped", "an open paper position already exists")
            return {"action": "monitoring", "reason": "an open paper position already exists"}
        if external_source and not queued_signal:
            provider = "TradingView indicator" if source == "hybrid" else "TradingView/GainzAlgo"
            return {"action": "wait", "reason": f"waiting for a fresh {provider} 15m alert"}

        discovered = discover()
        candidates = discovered.get("candidates", [])
        parsed = []
        for candidate in candidates:
            ticker = candidate.get("ticker")
            if not ticker:
                continue
            try:
                market = _extract_market(get_market(str(ticker)))
                if market:
                    parsed.append(market)
            except (KalshiAPIError, ValueError, TypeError) as exc:
                log.warning("Market lookup failed for %s: %s", ticker, exc)
        if not parsed:
            return {
                "action": "wait",
                "reason": "no candidate had an explicit strike, valid 15m close time, and verified BTC-up market title",
            }
        market = min(parsed, key=lambda item: item["seconds"])
        if market["seconds"] < max(30, int(os.getenv("PAPER_MIN_SECONDS_REMAINING", "30"))):
            if queued_signal:
                _mark_gainzalgo(conn, queued_signal["id"], "skipped", "less than the minimum time remained for a fresh entry")
            return {"action": "wait", "reason": "too little time remains for a fresh entry"}

        orderbook = get_orderbook(market["ticker"])
        yes_bid = _top_bid(orderbook, "yes")
        no_bid = _top_bid(orderbook, "no")
        yes_ask = market["yes_ask"]
        no_ask = market["no_ask"]
        if yes_ask is None or no_ask is None or yes_bid is None or no_bid is None:
            # Leave fresh external alert queued so the same valid alert can be retried while fresh.
            return {"action": "wait", "reason": "market/orderbook missing an explicit yes/no ask or bid"}
        spread = max(yes_ask - yes_bid, no_ask - no_bid)
        now = _utcnow().isoformat()
        warnings: list[str] = []
        internal_payload: dict[str, Any] | None = None
        signal_score = 0
        entry_btc_price: float
        entry_btc_candle_timestamp: str

        if external_source:
            direction = queued_direction
            entry = yes_ask if direction == "UP" else no_ask
            if entry is None or entry <= 0 or entry > max_entry:
                _mark_gainzalgo(conn, queued_signal["id"], "skipped", f"invalid or too expensive ask: {entry}; max_entry_cents={max_entry}")
                return {"action": "wait", "reason": "TradingView signal skipped: selected contract ask is missing or exceeds PAPER_MAX_ENTRY_CENTS", "entry_cents": entry}
            if spread > max_spread:
                _mark_gainzalgo(conn, queued_signal["id"], "skipped", f"spread too wide: {spread:.2f} cents")
                return {"action": "wait", "reason": "TradingView signal skipped: Kalshi spread exceeds PAPER_MAX_SPREAD_CENTS", "spread_cents": spread}

            if source == "hybrid":
                candles = fetch_signal_candles()
                internal_signal = evaluate_signal(
                    candles["candles_1m"], candles["candles_5m"], candles["candles_15m"],
                    MarketContext(
                        strike_price=market["strike"], seconds_remaining=market["seconds"],
                        up_ask_cents=yes_ask, down_ask_cents=no_ask,
                        spread_cents=spread, estimated_fee_cents=fee,
                    ),
                    minimum_score=min_score, max_entry_cents=max_entry, max_spread_cents=max_spread,
                )
                internal_payload = internal_signal.to_dict()
                veto_min_points = max(2, min(12, int(os.getenv("PAPER_HYBRID_VETO_MIN_POINTS", "4"))))
                vetoed, veto_reasons = _major_flaw_veto(direction, internal_payload, veto_min_points)
                entry_btc_price = float(candles["candles_1m"]["close"].iloc[-1])
                entry_btc_candle_timestamp = str(candles["candles_1m"]["timestamp"].iloc[-1])
                evidence = internal_payload.get("strategies", {}).get("evidence_totals", {})
                desired_points = int(evidence.get("up_points" if direction == "UP" else "down_points", 0) or 0)
                signal_score = round(max(0, min(100, desired_points / 12 * 100)))
                final_direction = "WAIT" if vetoed else ("BUY_UP" if direction == "UP" else "BUY_DOWN")
                safe_external = _safe_external_payload(queued_signal["payload_json"])
                reasons = [f"TradingView indicator signaled {direction}."]
                if vetoed:
                    reasons.extend(veto_reasons)
                else:
                    reasons.append(
                        "Accepted: the internal system found no major flaw. Ordinary WAIT results, "
                        "mixed indicators, or a low internal score do not override the TradingView signal."
                    )
                payload = {
                    **internal_payload,
                    "direction": final_direction,
                    "score": signal_score,
                    "score_kind": "internal directional evidence score; not a win probability",
                    "source": "TradingView primary; internal engine is a major-flaw veto only",
                    "external_signal": {
                        "id": queued_signal["id"],
                        "direction": direction,
                        "created_at": queued_signal["created_at"],
                        "age_seconds": queued_signal["age_seconds"],
                        "payload": safe_external,
                    },
                    "major_flaw_veto": {"vetoed": vetoed, "minimum_opposing_points": veto_min_points, "reasons": veto_reasons},
                    "reasons": reasons + list(internal_payload.get("reasons", [])),
                    "warnings": list(internal_payload.get("warnings", [])),
                    "final_decision_reason": "; ".join(veto_reasons) if vetoed else "TradingView direction accepted; no major internal flaw detected.",
                    "take_profit": {
                        "enabled": take_profit_enabled,
                        "target_return_percent": take_profit_percent if take_profit_enabled else None,
                        "target_basis": "net ROI after estimated entry and exit fees",
                    },
                    "disclaimer": "TradingView is the primary direction source; internal rules can veto only clear major conflicts. Paper mode only.",
                }
                warnings = payload["warnings"]
                _record_signal_log(
                    conn, created_at=now, ticker=market["ticker"], direction=final_direction,
                    score=signal_score, btc_price=entry_btc_price, strike=market["strike"],
                    seconds_remaining=market["seconds"], max_entry_cents=max_entry, payload=payload,
                )
                if vetoed:
                    reason_text = "; ".join(veto_reasons)
                    _mark_gainzalgo(conn, queued_signal["id"], "vetoed", reason_text)
                    return {
                        "action": "wait", "ticker": market["ticker"], "source": "hybrid",
                        "reason": "TradingView signal rejected by the internal major-flaw veto",
                        "veto_reasons": veto_reasons, "score": signal_score,
                        "warnings": warnings, "mode": "SIMULATION_ONLY",
                    }
            else:
                entry_candles = fetch_closed_candles("1m", 100)
                entry_btc_price = float(entry_candles["close"].iloc[-1])
                entry_btc_candle_timestamp = str(entry_candles["timestamp"].iloc[-1])
                final_direction = "BUY_UP" if direction == "UP" else "BUY_DOWN"
                signal_score = 0
                safe_external = _safe_external_payload(queued_signal["payload_json"])
                payload = {
                    "direction": final_direction, "score": 0,
                    "score_kind": "no internal score in external-only mode",
                    "source": "TradingView indicator (external-only compatibility mode)",
                    "external_signal": {"id": queued_signal["id"], "direction": direction,
                                        "created_at": queued_signal["created_at"],
                                        "age_seconds": queued_signal["age_seconds"], "payload": safe_external},
                    "reasons": [f"TradingView indicator signaled {direction}; market and risk checks passed."],
                    "warnings": [],
                    "take_profit": {"enabled": take_profit_enabled,
                                    "target_return_percent": take_profit_percent if take_profit_enabled else None},
                    "disclaimer": "External indicator direction; paper mode only.",
                }
                _record_signal_log(
                    conn, created_at=now, ticker=market["ticker"], direction=final_direction,
                    score=0, btc_price=entry_btc_price, strike=market["strike"],
                    seconds_remaining=market["seconds"], max_entry_cents=max_entry, payload=payload,
                )
        else:
            candles = fetch_signal_candles()
            signal = evaluate_signal(
                candles["candles_1m"], candles["candles_5m"], candles["candles_15m"],
                MarketContext(
                    strike_price=market["strike"], seconds_remaining=market["seconds"],
                    up_ask_cents=yes_ask, down_ask_cents=no_ask,
                    spread_cents=spread, estimated_fee_cents=fee,
                ),
                minimum_score=min_score, max_entry_cents=max_entry, max_spread_cents=max_spread,
            )
            payload = signal.to_dict()
            payload["source"] = "internal"
            payload["take_profit"] = {
                "enabled": take_profit_enabled,
                "target_return_percent": take_profit_percent if take_profit_enabled else None,
                "target_basis": "net ROI after estimated entry and exit fees",
            }
            entry_btc_price = float(signal.btc_price)
            entry_btc_candle_timestamp = str(candles["candles_1m"]["timestamp"].iloc[-1])
            signal_score = int(signal.score)
            _record_signal_log(
                conn, created_at=now, ticker=market["ticker"], direction=signal.direction,
                score=signal_score, btc_price=signal.btc_price, strike=signal.strike_price,
                seconds_remaining=signal.seconds_remaining, max_entry_cents=signal.max_entry_cents,
                payload=payload,
            )
            if signal.direction == "WAIT":
                return {
                    "action": "wait", "ticker": market["ticker"], "score": signal.score,
                    "reasons": signal.reasons, "warnings": signal.warnings,
                }
            direction = "UP" if signal.direction == "BUY_UP" else "DOWN"
            entry = yes_ask if direction == "UP" else no_ask
            warnings = signal.warnings

        contracts = max(0, min(10, int((max_trade * 100) // (entry + fee))))
        if contracts < 1:
            if queued_signal:
                _mark_gainzalgo(conn, queued_signal["id"], "skipped", "one contract plus fees exceeds MAX_TRADE_DOLLARS")
            return {
                "action": "blocked", "reason": "even one contract plus fees exceeds MAX_TRADE_DOLLARS",
                "entry_cents": entry, "fee_cents": fee,
            }
        stop = max(1.0, entry * (1.0 - stop_pct / 100.0)) if os.getenv("PAPER_STOP_LOSS_ENABLED", "false").lower() == "true" else None
        take_profit_target = (
            _take_profit_target_cents(entry, fee, exit_fee, take_profit_percent)
            if take_profit_enabled else None
        )
        total = contracts * (entry + fee)
        if realized - open_cost - total <= -max_loss * 100:
            if queued_signal:
                _mark_gainzalgo(conn, queued_signal["id"], "skipped", "trade would exceed daily loss budget")
            return {"action": "blocked", "reason": "trade would exceed daily loss budget when worst-case open risk is reserved"}

        signal_source_label = "hybrid" if source == "hybrid" else ("TradingView" if external_source else "internal")
        external_signal_id = queued_signal["id"] if queued_signal else None
        if source == "hybrid":
            note = f"TradingView-primary hybrid paper entry; signal_id={external_signal_id}; internal engine vetoes only major contradictions"
        elif source == "gainzalgo":
            note = f"TradingView external-only paper entry; signal_id={external_signal_id}"
        else:
            note = "Internal strategy paper entry; heuristic score is not a win probability"

        conn.execute(
            """INSERT INTO paper_trades
               (created_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,
                notes,stop_loss_cents,entry_btc_price,high_water_btc,low_water_btc,last_stop_candle_timestamp,
                take_profit_target_cents,entry_signal_source,external_signal_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, market["ticker"], direction, contracts, entry, fee, note, stop,
             entry_btc_price, entry_btc_price, entry_btc_price, entry_btc_candle_timestamp,
             take_profit_target, signal_source_label, external_signal_id),
        )
        conn.commit()
        if queued_signal:
            _mark_gainzalgo(conn, queued_signal["id"], "processed", f"paper trade opened: {market['ticker']} {direction}")

        return {
            "action": "paper_opened", "ticker": market["ticker"], "direction": direction,
            "source": signal_source_label, "signal_id": external_signal_id,
            "contracts": contracts, "entry_cents": entry, "fee_cents_per_contract": fee,
            "total_entry_cost_dollars": round(total / 100, 4),
            "stop_loss_trigger_bid_cents": round(stop, 2) if stop is not None else None,
            "trend_stop_enabled": os.getenv("PAPER_TREND_STOP_LOSS_ENABLED", "false").lower() == "true",
            "trend_stop_shadow_mode": os.getenv("PAPER_TREND_STOP_SHADOW_MODE", "false").lower() == "true",
            "take_profit_enabled": take_profit_enabled,
            "take_profit_target_cents": round(take_profit_target, 2) if take_profit_target is not None else None,
            "take_profit_target_return_percent": take_profit_percent if take_profit_enabled else None,
            "entry_btc_price": round(entry_btc_price, 2),
            "mode": "SIMULATION_ONLY", "warnings": warnings,
        }
    except (CandleFeedError, KalshiAPIError, ValueError, TypeError, KeyError) as exc:
        return {"action": "error_wait", "reason": str(exc)}
    finally:
        conn.close()

def run_forever():
    interval=max(10,min(60,int(os.getenv("PAPER_WORKER_INTERVAL_SECONDS","15"))))
    log.info("Paper worker started; real orders are not supported")
    while True:
        try: log.info("Paper worker cycle: %s",run_once())
        except Exception: log.exception("Unexpected paper worker error; continuing without a trade")
        time.sleep(interval)

if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
    run_forever()