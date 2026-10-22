"""Conservative automatic PAPER-TRADING worker for the Kalshi BTC 15m bot.

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

def _manage_open(conn, max_daily_loss_dollars: float):
    rows=conn.execute(
        "SELECT id,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,stop_loss_cents, "
        "entry_btc_price,high_water_btc,low_water_btc,trailing_stop_btc,last_stop_candle_timestamp,last_trend_check_at "
        "FROM paper_trades WHERE settled_at IS NULL"
    ).fetchall()
    trend_enabled = os.getenv("PAPER_TREND_STOP_LOSS_ENABLED", "false").lower() == "true"
    check_seconds = max(30.0, min(180.0, float(os.getenv("PAPER_TREND_STOP_CHECK_SECONDS", "45"))))
    atr_multiplier = float(os.getenv("PAPER_TREND_STOP_ATR_MULTIPLIER", "2.5"))
    swing_lookback = max(8, min(100, int(os.getenv("PAPER_TREND_STOP_SWING_LOOKBACK", "30"))))
    for row in rows:
        (trade_id,ticker,direction,contracts,entry,fee,static_stop,entry_btc,high_water,low_water,
         trailing_stop,last_candle_ts,last_check_at) = row
        try:
            # Settlement always takes precedence and uses only Kalshi's official final result.
            market_payload=get_market(ticker)
            market=market_payload.get("market",market_payload)
            status=str(market.get("status", "")).lower() if isinstance(market,dict) else ""
            result=str(market.get("result", "")).lower() if isinstance(market,dict) else ""
            if status in {"settled", "finalized"} and result in {"yes", "no"}:
                outcome="UP" if result=="yes" else "DOWN"
                gross=100*int(contracts) if direction==outcome else 0
                pnl=gross-float(entry)*int(contracts)-float(fee)*int(contracts)
                conn.execute("UPDATE paper_trades SET settled_at=?,outcome=?,pnl_cents=?,notes=COALESCE(notes,'') || ? WHERE id=?",
                    (_utcnow().isoformat(),outcome,pnl," | automatically settled from Kalshi final result",trade_id))
                conn.commit()
                log.info("Paper trade %s settled from official market result %s",trade_id,result)
                continue

            payload=get_orderbook(ticker)
            bid=_top_bid(payload, "yes" if direction=="UP" else "no")
            if bid is None:
                continue

            trend_triggered = False
            trend_reason = None
            now_dt = _utcnow()
            # Only update chart stops using a new, fully closed 1m candle. This gate
            # is OFF by default and is separate from the legacy fixed-contract stop.
            last_check = _parse_time(last_check_at)
            due = last_check is None or (now_dt - last_check).total_seconds() >= check_seconds
            if trend_enabled and entry_btc is not None and due:
                # Persist the throttle before the request so a failed feed won't hammer Binance.
                conn.execute("UPDATE paper_trades SET last_trend_check_at=? WHERE id=?",
                             (now_dt.isoformat(), trade_id))
                conn.commit()
                try:
                    candles = fetch_closed_candles("1m", 100)
                    candle_ts = str(candles["timestamp"].iloc[-1])
                    if candle_ts != last_candle_ts:
                        update = update_trend_stop(
                            candles, direction,
                            high_water_btc=(float(high_water) if high_water is not None else float(entry_btc)),
                            low_water_btc=(float(low_water) if low_water is not None else float(entry_btc)),
                            trailing_stop_btc=(float(trailing_stop) if trailing_stop is not None else None),
                            atr_multiplier=atr_multiplier, swing_lookback=swing_lookback,
                        )
                        conn.execute(
                            "UPDATE paper_trades SET high_water_btc=?,low_water_btc=?,trailing_stop_btc=?,last_stop_candle_timestamp=? WHERE id=?",
                            (update["high_water_btc"], update["low_water_btc"], update["trailing_stop_btc"], candle_ts, trade_id)
                        )
                        conn.commit()
                        trailing_stop = update["trailing_stop_btc"]
                        trend_reason = update["reason"]
                        trend_triggered = bool(update["triggered"])
                        log.info("Paper trade %s chart-stop update: %s; enabled=%s; trigger=%s",
                                 trade_id, trend_reason, trend_enabled, trend_triggered)
                    elif trailing_stop is not None:
                        # Re-check an already processed candle in case a previous cycle
                        # persisted the trail but was interrupted before marking the exit.
                        last_close = float(candles["close"].iloc[-1])
                        trend_triggered = (
                            (direction == "UP" and last_close <= float(trailing_stop))
                            or (direction == "DOWN" and last_close >= float(trailing_stop))
                        )
                        if trend_triggered:
                            trend_reason = (f"Previously stored {direction} chart stop {float(trailing_stop):.2f} "
                                            f"breached by latest closed BTC candle {last_close:.2f}")
                except (CandleFeedError, ValueError, TypeError, KeyError) as exc:
                    log.warning("Trend stop check unavailable for paper trade %s; no chart-stop exit: %s", trade_id, exc)

            static_triggered = (
                os.getenv("PAPER_STOP_LOSS_ENABLED", "false").lower() == "true"
                and static_stop is not None and bid <= float(static_stop)
            )
            if trend_triggered or static_triggered:
                # Chart trigger decides *when* to exit; the observed Kalshi bid
                # models the paper exit price. BTC price is never substituted for it.
                exit_fee=float(os.getenv("PAPER_EXIT_FEE_CENTS_PER_CONTRACT", "2.0"))
                pnl=(bid-float(entry)-float(fee)-exit_fee)*int(contracts)
                now=_utcnow().isoformat()
                if trend_triggered:
                    outcome = "TREND_STOP_LOSS"
                    note = (f" | paper trend stop triggered; {trend_reason}; "
                            f"simulated Kalshi exit bid {bid:.2f}c; no real order placed")
                else:
                    outcome = "STOP_LOSS"
                    note = f" | paper fixed stop triggered; observed Kalshi bid {bid:.2f}c; simulated exit only"
                conn.execute("UPDATE paper_trades SET settled_at=?,outcome=?,pnl_cents=?,notes=COALESCE(notes,'') || ? WHERE id=?",
                  (now, outcome, pnl, note, trade_id))
                conn.commit()
                log.warning("Paper trade %s closed by %s at observed Kalshi bid %.2fc; no real order placed",
                            trade_id, outcome, bid)
        except (KalshiAPIError, ValueError, TypeError, KeyError) as exc:
            log.warning("Could not monitor open paper trade %s: %s",trade_id,exc)

def _next_gainzalgo_signal(conn):
    """Consume one fresh, unprocessed GainzAlgo alert. Returns (row, mapped direction)."""
    max_age=max(30, min(900, int(os.getenv("GAINZALGO_MAX_SIGNAL_AGE_SECONDS", "300"))))
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
        return {"id": signal_id, "created_at": created_at, "direction": direction, "payload_json": payload_json}, direction
    return None, None

def _mark_gainzalgo(conn, signal_id, status, note):
    conn.execute("UPDATE gainzalgo_signals SET processed_at=?,processing_status=?,processing_note=? WHERE id=? AND processed_at IS NULL",
                 (_utcnow().isoformat(), status, str(note)[:500], signal_id))
    conn.commit()

def run_once() -> dict:
    """Run one paper-only monitoring cycle; safe to call in tests or a worker loop."""
    if os.getenv("TRADING_MODE", "paper").lower() != "paper":
        return {"action":"blocked","reason":"TRADING_MODE must be paper"}
    if os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true":
        return {"action":"blocked","reason":"This worker refuses to run when live trading is enabled"}
    max_trade=float(os.getenv("MAX_TRADE_DOLLARS","1.00"))
    max_loss=float(os.getenv("MAX_DAILY_LOSS_DOLLARS","10.00"))
    fee=float(os.getenv("PAPER_FEE_CENTS_PER_CONTRACT","2.0"))
    stop_pct=float(os.getenv("PAPER_STOP_LOSS_PERCENT","35"))
    min_score=int(os.getenv("PAPER_MINIMUM_SCORE","65"))
    max_entry=float(os.getenv("PAPER_MAX_ENTRY_CENTS","78"))
    conn=_connect()
    try:
        _manage_open(conn,max_loss)
        realized,open_cost,count,halt=_limits(conn,max_trade,max_loss)
        if halt: return {"action":"blocked","reason":"daily loss budget reached including open-position risk","realized_pnl_cents":round(realized,2),"open_risk_cents":round(open_cost,2)}
        daily_cap=max(1,min(96,int(os.getenv("PAPER_MAX_TRADES_PER_DAY","96"))))
        if count>=daily_cap: return {"action":"wait","reason":"paper daily trade cap reached"}
        # GainzAlgo is the default source; the internal heuristic is used only when explicitly selected.
        source=os.getenv("PAPER_SIGNAL_SOURCE", "gainzalgo").strip().lower()
        queued_signal, queued_direction = _next_gainzalgo_signal(conn) if source == "gainzalgo" else (None, None)
        if conn.execute("SELECT 1 FROM paper_trades WHERE settled_at IS NULL LIMIT 1").fetchone():
            if queued_signal:
                _mark_gainzalgo(conn, queued_signal["id"], "skipped", "an open paper position already exists")
            return {"action":"monitoring","reason":"an open paper position already exists"}
        if source == "gainzalgo" and not queued_signal:
            return {"action":"wait","reason":"waiting for a fresh GainzAlgo 15m alert"}
        discovered=discover()
        candidates=discovered.get("candidates",[])
        parsed=[]
        for c in candidates:
            ticker=c.get("ticker")
            if not ticker: continue
            try:
                m=_extract_market(get_market(str(ticker)))
                if m: parsed.append(m)
            except (KalshiAPIError, ValueError, TypeError) as exc:
                log.warning("Market lookup failed for %s: %s",ticker,exc)
        if not parsed: return {"action":"wait","reason":"no candidate had an explicit strike, valid 15m close time, and verified BTC-up market title"}
        # Use the earliest-closing valid candidate; do not assume discovery ordering.
        m=min(parsed,key=lambda x:x["seconds"])
        book=get_orderbook(m["ticker"])
        yes_bid=_top_bid(book,"yes"); no_bid=_top_bid(book,"no")
        yes_ask=m["yes_ask"]; no_ask=m["no_ask"]
        # Don't manufacture missing prices or infer them from the opposite side.
        if yes_ask is None or no_ask is None or yes_bid is None or no_bid is None:
            return {"action":"wait","reason":"market/orderbook missing an explicit yes/no ask or bid"}
        spread=max(yes_ask-yes_bid,no_ask-no_bid)
        now=_utcnow().isoformat()
        if source == "gainzalgo":
            direction=queued_direction
            entry=yes_ask if direction=="UP" else no_ask
            if entry is None or entry <= 0 or entry > max_entry:
                _mark_gainzalgo(conn, queued_signal["id"], "skipped", f"invalid or too expensive ask: {entry}; max_entry_cents={max_entry}")
                return {"action":"wait","reason":"GainzAlgo signal skipped: market ask is missing or exceeds PAPER_MAX_ENTRY_CENTS","entry_cents":entry}
            if spread > float(os.getenv("PAPER_MAX_SPREAD_CENTS","5")):
                _mark_gainzalgo(conn, queued_signal["id"], "skipped", f"spread too wide: {spread:.2f} cents")
                return {"action":"wait","reason":"GainzAlgo signal skipped: market spread exceeds PAPER_MAX_SPREAD_CENTS","spread_cents":spread}
            payload={"source":"GainzAlgo","signal_id":queued_signal["id"],"direction":direction,"raw_payload":queued_signal["payload_json"]}
            warnings=[]
            entry_btc_price=None
            entry_btc_candle_timestamp=None
        else:
            candles=fetch_signal_candles()
            signal=evaluate_signal(candles["candles_1m"],candles["candles_5m"],candles["candles_15m"],
                MarketContext(strike_price=m["strike"],seconds_remaining=m["seconds"],up_ask_cents=yes_ask,down_ask_cents=no_ask,spread_cents=spread,estimated_fee_cents=fee),
                minimum_score=min_score,max_entry_cents=max_entry,max_spread_cents=float(os.getenv("PAPER_MAX_SPREAD_CENTS","5")))
            payload=signal.to_dict()
            entry_btc_price=float(signal.btc_price)
            entry_btc_candle_timestamp=str(candles["candles_1m"]["timestamp"].iloc[-1])
            conn.execute("INSERT INTO signal_log (created_at,market_ticker,direction,score,btc_price,strike_price,seconds_remaining,max_entry_cents,payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
              (now,m["ticker"],payload["direction"],payload["score"],payload["btc_price"],payload["strike_price"],payload["seconds_remaining"],payload["max_entry_cents"],json.dumps(payload)))
            conn.commit()
            if signal.direction=="WAIT": return {"action":"wait","ticker":m["ticker"],"score":signal.score,"reasons":signal.reasons,"warnings":signal.warnings}
            direction="UP" if signal.direction=="BUY_UP" else "DOWN"
            entry=yes_ask if direction=="UP" else no_ask
            warnings=signal.warnings
        # GainzAlgo alerts do not contain the candle price; capture the latest closed
        # BTC candle so future chart-stop tests have a real entry reference.
        if entry_btc_price is None:
            entry_candles=fetch_closed_candles("1m", 100)
            entry_btc_price=float(entry_candles["close"].iloc[-1])
            entry_btc_candle_timestamp=str(entry_candles["timestamp"].iloc[-1])
        contracts=max(0,min(10,int((max_trade*100)//(entry+fee))))
        if contracts<1:
            if queued_signal: _mark_gainzalgo(conn, queued_signal["id"], "skipped", "one contract plus fees exceeds MAX_TRADE_DOLLARS")
            return {"action":"blocked","reason":"even one contract plus conservative fees exceeds MAX_TRADE_DOLLARS","entry_cents":entry,"fee_cents":fee}
        stop = max(1.0, entry * (1.0 - stop_pct / 100.0)) if os.getenv("PAPER_STOP_LOSS_ENABLED", "false").lower() == "true" else None
        total=contracts*(entry+fee)
        # Include open-risk reservation in daily loss budget before creating a position.
        if realized-open_cost-total <= -max_loss*100:
            if queued_signal: _mark_gainzalgo(conn, queued_signal["id"], "skipped", "trade would exceed daily loss budget")
            return {"action":"blocked","reason":"trade would exceed daily loss budget when worst-case open risk is reserved"}
        conn.execute("INSERT INTO paper_trades (created_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,notes,stop_loss_cents,entry_btc_price,high_water_btc,low_water_btc,last_stop_candle_timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
          (now,m["ticker"],direction,contracts,entry,fee,(f"GainzAlgo paper entry; signal_id={queued_signal['id']}" if queued_signal else "AUTO PAPER ENTRY; heuristic score is not a win probability"),stop,entry_btc_price,entry_btc_price,entry_btc_price,entry_btc_candle_timestamp))
        conn.commit()
        if queued_signal: _mark_gainzalgo(conn, queued_signal["id"], "processed", f"paper trade opened: {m['ticker']} {direction}")
        return {"action":"paper_opened","ticker":m["ticker"],"direction":direction,"source":"GainzAlgo" if queued_signal else "internal_heuristic","signal_id":queued_signal["id"] if queued_signal else None,"contracts":contracts,"entry_cents":entry,"fee_cents_per_contract":fee,"total_entry_cost_dollars":round(total/100,4),"stop_loss_trigger_bid_cents": round(stop, 2) if stop is not None else None,"trend_stop_enabled": os.getenv("PAPER_TREND_STOP_LOSS_ENABLED", "false").lower() == "true","entry_btc_price": round(entry_btc_price, 2),"mode":"SIMULATION_ONLY","warnings":warnings}
    except (CandleFeedError, KalshiAPIError, ValueError, TypeError, KeyError) as exc:
        return {"action":"error_wait","reason":str(exc)}
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