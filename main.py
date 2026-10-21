from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from signal_engine import MarketContext, evaluate_signal
from market_discovery import discover
from kalshi_api import get_market, get_orderbook, KalshiAPIError

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("BOT_DB_PATH", str(APP_DIR / "bot_journal.sqlite3")))

app = FastAPI(
    title="Kalshi BTC 15-Minute Bot",
    description=(
        "Cloud-ready starter runtime. Signal evaluation and journaling work; "
        "live market feeds, Telegram delivery, and order execution are not connected."
    ),
    version="0.1.0",
)


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            market_ticker TEXT,
            direction TEXT NOT NULL,
            score INTEGER NOT NULL,
            btc_price REAL NOT NULL,
            strike_price REAL NOT NULL,
            seconds_remaining INTEGER NOT NULL,
            max_entry_cents REAL,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            settled_at TEXT,
            market_ticker TEXT NOT NULL,
            direction TEXT NOT NULL,
            contracts INTEGER NOT NULL,
            entry_cents REAL NOT NULL,
            fee_cents_per_contract REAL NOT NULL,
            outcome TEXT,
            pnl_cents REAL,
            notes TEXT,
            stop_loss_cents REAL
        )
        """
    )
    # Safe migration for databases created before stop_loss_cents existed.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
    if "stop_loss_cents" not in columns:
        conn.execute("ALTER TABLE paper_trades ADD COLUMN stop_loss_cents REAL")
    conn.commit()
    return conn


class Candle(BaseModel):
    timestamp: str | None = None
    open: float
    high: float
    low: float
    close: float
    volume: float = Field(ge=0)


class MarketInput(BaseModel):
    market_ticker: str | None = None
    strike_price: float
    seconds_remaining: int = Field(ge=0)
    up_ask_cents: float | None = Field(default=None, ge=0, le=100)
    down_ask_cents: float | None = Field(default=None, ge=0, le=100)
    spread_cents: float | None = Field(default=None, ge=0, le=100)
    estimated_fee_cents: float = Field(default=0, ge=0)


class EvaluateRequest(BaseModel):
    candles_1m: list[Candle]
    candles_5m: list[Candle]
    candles_15m: list[Candle]
    market: MarketInput
    minimum_score: int = Field(default=65, ge=0, le=100)
    max_entry_cents: float = Field(default=78, ge=0, le=100)


class PaperTradeRequest(BaseModel):
    market_ticker: str = Field(min_length=1, max_length=120)
    direction: str
    contracts: int = Field(ge=1, le=10)  # Additional dollar cap is enforced below.
    entry_cents: float = Field(gt=0, le=99)
    fee_cents_per_contract: float = Field(default=0, ge=0, le=10)
    stop_loss_cents: float | None = Field(default=None, gt=0, le=99)
    notes: str | None = Field(default=None, max_length=500)


class PaperSettleRequest(BaseModel):
    outcome: str
    notes: str | None = Field(default=None, max_length=500)


@app.on_event("startup")
def startup() -> None:
    with db_connect():
        pass
    # The automatic worker is opt-in and paper-only. It runs in this same
    # service process so it shares the configured SQLite database and disk.
    if os.getenv("PAPER_WORKER_ENABLED", "false").lower() == "true":
        from paper_worker import run_forever
        thread = threading.Thread(target=run_forever, name="paper-trading-worker", daemon=True)
        thread.start()


@app.get("/")
def home():
    return FileResponse(
        Path(__file__).resolve().parent / "dashboard.html",
        media_type="text/html"
    )

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "time_utc": datetime.now(timezone.utc).isoformat()}


def candles_to_df(candles: list[Candle]) -> pd.DataFrame:
    return pd.DataFrame([c.model_dump() for c in candles])


@app.post("/evaluate")
def evaluate(request: EvaluateRequest) -> dict[str, Any]:
    """Evaluate a supplied snapshot. This endpoint does not fetch live data or trade."""
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
            candles_to_df(request.candles_1m),
            candles_to_df(request.candles_5m),
            candles_to_df(request.candles_15m),
            market,
            minimum_score=request.minimum_score,
            max_entry_cents=request.max_entry_cents,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    payload = signal.to_dict()
    created_at = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO signal_log (
                created_at, market_ticker, direction, score, btc_price,
                strike_price, seconds_remaining, max_entry_cents, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                request.market.market_ticker,
                payload["direction"],
                payload["score"],
                payload["btc_price"],
                payload["strike_price"],
                payload["seconds_remaining"],
                payload["max_entry_cents"],
                json.dumps(payload),
            ),
        )
    return {"created_at": created_at, **payload}


@app.get("/journal/recent")
def recent_signals(limit: int = 25) -> dict[str, Any]:
    if not 1 <= limit <= 250:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 250")
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, market_ticker, direction, score, btc_price,
                   strike_price, seconds_remaining, max_entry_cents
            FROM signal_log ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    columns = [
        "id", "created_at", "market_ticker", "direction", "score", "btc_price",
        "strike_price", "seconds_remaining", "max_entry_cents"
    ]
    return {"signals": [dict(zip(columns, row)) for row in rows]}


@app.get("/status")
def status() -> dict[str, Any]:
    with db_connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
    return {
        "status": "starter-ready",
        "signals_logged": count,
        "database_path": str(DB_PATH),
        "live_market_data": "not connected",
        "telegram_alerts": "not connected",
        "paper_trading": "automatic paper worker enabled" if os.getenv("PAPER_WORKER_ENABLED", "false").lower() == "true" else "manual ledger; automatic worker disabled",
        "paper_worker_enabled": os.getenv("PAPER_WORKER_ENABLED", "false").lower() == "true",
        "real_orders": "not implemented; no order path exists",
    }


@app.post("/paper/open")
def open_paper_trade(request: PaperTradeRequest) -> dict[str, Any]:
    """Record a simulated position only; this never sends an order to Kalshi."""
    direction = request.direction.upper()
    if direction not in {"UP", "DOWN"}:
        raise HTTPException(status_code=422, detail="direction must be UP or DOWN")
    today = datetime.now(timezone.utc).date().isoformat()
    max_trade_dollars = max(0.01, float(os.getenv("MAX_TRADE_DOLLARS", "1.00")))
    max_daily_loss_dollars = max(0.01, float(os.getenv("MAX_DAILY_LOSS_DOLLARS", "10.00")))
    total_cost_cents = request.contracts * (request.entry_cents + request.fee_cents_per_contract)
    if total_cost_cents > max_trade_dollars * 100 + 1e-9:
        raise HTTPException(
            status_code=422,
            detail=f"Trade blocked: total entry cost plus fees ({total_cost_cents / 100:.2f} USD) exceeds MAX_TRADE_DOLLARS ({max_trade_dollars:.2f} USD).",
        )
    with db_connect() as conn:
        daily_pnl_cents = float(conn.execute(
            """SELECT COALESCE(SUM(pnl_cents), 0) FROM paper_trades
               WHERE settled_at IS NOT NULL AND substr(settled_at, 1, 10) = ?""",
            (today,),
        ).fetchone()[0])
        if daily_pnl_cents <= -(max_daily_loss_dollars * 100):
            raise HTTPException(status_code=429, detail="Paper trading halted: daily realized loss limit reached.")
        daily_count = int(conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE substr(created_at, 1, 10) = ?",
            (today,),
        ).fetchone()[0])
        daily_cap = max(1, min(96, int(os.getenv("PAPER_MAX_TRADES_PER_DAY", "96"))))
        if daily_count >= daily_cap:
            raise HTTPException(status_code=429, detail="Paper-trade daily cap reached.")
        duplicate = conn.execute(
            """SELECT id FROM paper_trades
               WHERE market_ticker = ? AND direction = ? AND settled_at IS NULL""",
            (request.market_ticker, direction),
        ).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail=f"Unsettled paper trade already exists (id {duplicate[0]}).")
        created_at = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """INSERT INTO paper_trades
               (created_at, market_ticker, direction, contracts, entry_cents,
                fee_cents_per_contract, notes, stop_loss_cents) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (created_at, request.market_ticker, direction, request.contracts,
             request.entry_cents, request.fee_cents_per_contract, request.notes, request.stop_loss_cents),
        )
        trade_id = cur.lastrowid
    return {
        "paper_trade_id": trade_id, "mode": "SIMULATION_ONLY", "status": "open",
        "market_ticker": request.market_ticker, "direction": direction,
        "contracts": request.contracts, "entry_cents": request.entry_cents,
        "stop_loss_cents": request.stop_loss_cents,
        "max_gross_cost_cents": round(request.contracts * request.entry_cents, 4),
        "total_entry_cost_including_fees_dollars": round(total_cost_cents / 100, 4),
        "note": "No live order was placed. stop_loss_cents is stored as a target only; automatic price monitoring/exits are NOT implemented in this file."
    }


@app.post("/paper/{trade_id}/settle")
def settle_paper_trade(trade_id: int, request: PaperSettleRequest) -> dict[str, Any]:
    """Settle using the official final UP/DOWN outcome; not a live order."""
    outcome = request.outcome.upper()
    if outcome not in {"UP", "DOWN"}:
        raise HTTPException(status_code=422, detail="outcome must be UP or DOWN")
    with db_connect() as conn:
        row = conn.execute(
            """SELECT id, direction, contracts, entry_cents, fee_cents_per_contract,
                      settled_at, market_ticker FROM paper_trades WHERE id = ?""",
            (trade_id,),
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
    return {"paper_trade_id": trade_id, "market_ticker": ticker, "outcome": outcome,
            "gross_payout_cents": gross_payout, "entry_cost_cents": cost,
            "fees_cents": fees, "net_pnl_cents": round(pnl, 4),
            "net_pnl_dollars": round(pnl / 100, 4), "mode": "SIMULATION_ONLY"}


@app.get("/paper/trades")
def paper_trades(limit: int = 50) -> dict[str, Any]:
    if not 1 <= limit <= 250:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 250")
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT id, created_at, settled_at, market_ticker, direction, contracts,
                      entry_cents, fee_cents_per_contract, outcome, pnl_cents, notes, stop_loss_cents
               FROM paper_trades ORDER BY id DESC LIMIT ?""", (limit,)
        ).fetchall()
    keys = ["id", "created_at", "settled_at", "market_ticker", "direction", "contracts",
            "entry_cents", "fee_cents_per_contract", "outcome", "pnl_cents", "notes", "stop_loss_cents"]
    return {"trades": [dict(zip(keys, row)) for row in rows]}


@app.get("/paper/stats")
def paper_stats() -> dict[str, Any]:
    with db_connect() as conn:
        total, settled, wins, pnl = conn.execute(
            """SELECT COUNT(*), SUM(CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END),
                      SUM(CASE WHEN pnl_cents > 0 THEN 1 ELSE 0 END), COALESCE(SUM(pnl_cents), 0)
               FROM paper_trades"""
        ).fetchone()
    settled, wins = int(settled or 0), int(wins or 0)
    return {"mode": "SIMULATION_ONLY", "total_paper_trades": int(total or 0),
            "settled_trades": settled, "winning_trades": wins,
            "win_rate_percent": round(100 * wins / settled, 2) if settled else None,
            "net_pnl_cents": round(float(pnl), 4), "net_pnl_dollars": round(float(pnl) / 100, 4),
            "important": "Results only mean something if entries, fees, and official outcomes are recorded accurately."}


@app.get("/markets/btc-candidates")
def btc_candidates() -> dict[str, Any]:
    """List candidate open BTC/Bitcoin markets for manual verification."""
    try:
        return discover()
    except KalshiAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/markets/{ticker}")
def market_details(ticker: str) -> dict[str, Any]:
    """Fetch a specific market's public details."""
    try:
        return get_market(ticker)
    except KalshiAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/markets/{ticker}/orderbook")
def market_orderbook(ticker: str) -> dict[str, Any]:
    """Fetch a specific market's public order book."""
    try:
        return get_orderbook(ticker)
    except KalshiAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
