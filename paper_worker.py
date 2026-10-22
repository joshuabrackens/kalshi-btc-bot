"""Conservative automatic PAPER-TRADING worker for the Kalshi BTC 15m bot.

No authenticated endpoints and no real orders are used. The worker fails closed
unless the candidate market has an explicit strike and valid timing/prices.
"""
from __future__ import annotations
import json, os, sqlite3, time, logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from candle_feed import fetch_signal_candles, CandleFeedError
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
    if "stop_loss_cents" not in cols: conn.execute("ALTER TABLE paper_trades ADD COLUMN stop_loss_cents REAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS signal_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, market_ticker TEXT,
      direction TEXT NOT NULL, score INTEGER NOT NULL, btc_price REAL NOT NULL,
      strike_price REAL NOT NULL, seconds_remaining INTEGER NOT NULL, max_entry_cents REAL,
      payload_json TEXT NOT NULL)""")
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
    rows=conn.execute("SELECT id,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,stop_loss_cents FROM paper_trades WHERE settled_at IS NULL").fetchall()
    for trade_id,ticker,direction,contracts,entry,fee,stop in rows:
        try:
            # Settle only from an explicit final Kalshi result; never guess from BTC candles.
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
            if bid is None: continue
            if os.getenv("PAPER_STOP_LOSS_ENABLED", "true").lower() == "true" and stop is not None and bid <= float(stop):
                # Simulated exit at observed top bid, minus conservative exit fee.
                exit_fee=float(os.getenv("PAPER_EXIT_FEE_CENTS_PER_CONTRACT", "2.0"))
                pnl=(bid-float(entry)-float(fee)-exit_fee)*int(contracts)
                now=_utcnow().isoformat()
                conn.execute("UPDATE paper_trades SET settled_at=?,outcome='STOP_LOSS',pnl_cents=?,notes=COALESCE(notes,'') || ? WHERE id=?",
                  (now, pnl, f" | paper stop triggered; observed bid {bid:.2f}c; simulated exit only", trade_id))
                conn.commit()
                log.warning("Paper stop-loss closed trade %s at observed bid %.2fc; no real order placed",trade_id,bid)
        except (KalshiAPIError, ValueError, TypeError) as exc:
            log.warning("Could not monitor open paper trade %s: %s",trade_id,exc)

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
        # Conservative: one open position at a time to bound unobserved exposure.
        if conn.execute("SELECT 1 FROM paper_trades WHERE settled_at IS NULL LIMIT 1").fetchone():
            return {"action":"monitoring","reason":"an open paper position already exists"}
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
        candles=fetch_signal_candles()
        signal=evaluate_signal(candles["candles_1m"],candles["candles_5m"],candles["candles_15m"],
            MarketContext(strike_price=m["strike"],seconds_remaining=m["seconds"],up_ask_cents=yes_ask,down_ask_cents=no_ask,spread_cents=spread,estimated_fee_cents=fee),
            minimum_score=min_score,max_entry_cents=max_entry,max_spread_cents=float(os.getenv("PAPER_MAX_SPREAD_CENTS","5")))
        payload=signal.to_dict(); now=_utcnow().isoformat()
        conn.execute("INSERT INTO signal_log (created_at,market_ticker,direction,score,btc_price,strike_price,seconds_remaining,max_entry_cents,payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
          (now,m["ticker"],payload["direction"],payload["score"],payload["btc_price"],payload["strike_price"],payload["seconds_remaining"],payload["max_entry_cents"],json.dumps(payload)))
        conn.commit()
        if signal.direction=="WAIT": return {"action":"wait","ticker":m["ticker"],"score":signal.score,"reasons":signal.reasons,"warnings":signal.warnings}
        direction="UP" if signal.direction=="BUY_UP" else "DOWN"
        entry=yes_ask if direction=="UP" else no_ask
        contracts=max(0,min(10,int((max_trade*100)//(entry+fee))))
        if contracts<1: return {"action":"blocked","reason":"even one contract plus conservative fees exceeds MAX_TRADE_DOLLARS","entry_cents":entry,"fee_cents":fee}
        stop = max(1.0, entry * (1.0 - stop_pct / 100.0)) if os.getenv("PAPER_STOP_LOSS_ENABLED", "true").lower() == "true" else None
        total=contracts*(entry+fee)
        # Include open-risk reservation in daily loss budget before creating a position.
        if realized-open_cost-total <= -max_loss*100:
            return {"action":"blocked","reason":"trade would exceed daily loss budget when worst-case open risk is reserved"}
        conn.execute("INSERT INTO paper_trades (created_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,notes,stop_loss_cents) VALUES (?,?,?,?,?,?,?,?)",
          (now,m["ticker"],direction,contracts,entry,fee,"AUTO PAPER ENTRY; heuristic score is not a win probability",stop))
        conn.commit()
        return {"action":"paper_opened","ticker":m["ticker"],"direction":direction,"score":signal.score,"contracts":contracts,"entry_cents":entry,"fee_cents_per_contract":fee,"total_entry_cost_dollars":round(total/100,4),"stop_loss_trigger_bid_cents": round(stop, 2) if stop is not None else 
        None,"mode":"SIMULATION_ONLY","warnings":signal.warnings}
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
