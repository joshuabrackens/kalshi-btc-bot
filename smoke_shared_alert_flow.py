"""Offline end-to-end smoke test for webhook -> queue -> hybrid paper entry.
Uses deterministic stubs for candle and public-market providers; no network calls.
Run with: python smoke_shared_alert_flow.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd


def main() -> None:
    temp = tempfile.TemporaryDirectory()
    os.environ.pop("DATABASE_URL", None)
    os.environ["BOT_DB_PATH"] = str(Path(temp.name) / "shared.sqlite3")
    os.environ["GAINZALGO_WEBHOOK_SECRET"] = "local-smoke-secret"
    os.environ["PAPER_SIGNAL_SOURCE"] = "hybrid"
    os.environ.pop("PAPER_SHARED_SQLITE_OK", None)
    os.environ["PAPER_WORKER_ENABLED"] = "false"
    os.environ["TRADING_MODE"] = "paper"
    os.environ["LIVE_TRADING_ENABLED"] = "false"
    os.environ["MAX_TRADE_DOLLARS"] = "1.00"
    os.environ["MAX_DAILY_LOSS_DOLLARS"] = "10.00"
    os.environ["PAPER_FEE_CENTS_PER_CONTRACT"] = "2"
    os.environ["PAPER_MAX_ENTRY_CENTS"] = "92"
    os.environ["PAPER_ENTRY_TARGET_MAX_CENTS"] = "80"
    os.environ["PAPER_MAX_SPREAD_CENTS"] = "5"
    os.environ["PAPER_MINIMUM_SCORE"] = "65"

    now = datetime.now(timezone.utc)
    ticker = "KXBTC15M-26OCT222200-00"
    market = {"market": {"ticker": ticker, "status": "open", "title": "BTC price goes up",
                          "strike_price": 100.0, "close_time": (now + timedelta(seconds=600)).isoformat(),
                          "yes_ask": 50, "no_ask": 50}}

    discovery = types.ModuleType("market_discovery")
    discovery.discover = lambda: {"candidates": [{"ticker": ticker}]}
    sys.modules["market_discovery"] = discovery

    kalshi = types.ModuleType("kalshi_api")
    kalshi.get_market = lambda _ticker: market
    kalshi.get_orderbook = lambda _ticker: {"orderbook_fp": {"yes": [[48, 10]], "no": [[48, 10]]}}
    kalshi.KalshiAPIError = type("KalshiAPIError", (Exception,), {})
    sys.modules["kalshi_api"] = kalshi

    def candles(n: int = 35) -> pd.DataFrame:
        values = []
        for i in range(n):
            close = 100 + i * 0.25
            values.append({"timestamp": (now - timedelta(minutes=n - i)).isoformat(),
                           "open": close - 0.1, "high": close + 0.2,
                           "low": close - 0.25, "close": close, "volume": 100.0})
        return pd.DataFrame(values)

    feed = types.ModuleType("candle_feed")
    feed.CandleFeedError = type("CandleFeedError", (Exception,), {})
    feed.fetch_signal_candles = lambda: {"candles_1m": candles(), "candles_5m": candles(), "candles_15m": candles()}
    feed.fetch_closed_candles = lambda _interval, count: candles(count)
    sys.modules["candle_feed"] = feed

    trend = types.ModuleType("trend_stop")
    trend.update_trend_stop = lambda *args, **kwargs: {"high_water_btc": 100, "low_water_btc": 100,
                                                         "trailing_stop_btc": 99, "reason": "smoke", "triggered": False}
    sys.modules["trend_stop"] = trend

    try:
        import main
        import paper_worker

        class FakeRequest:
            def __init__(self, payload):
                self._payload = payload
            async def json(self):
                return self._payload

        async def exercise():
            # In a real two-service Render deployment, hybrid mode must fail closed without
            # DATABASE_URL rather than reading an isolated SQLite file.
            blocked = paper_worker.run_once()
            assert blocked.get("action") == "blocked" and "DATABASE_URL" in blocked.get("reason", ""), blocked

            # This smoke test deliberately runs both components in one process and shares
            # one local SQLite file; opt in explicitly only for this test.
            os.environ["PAPER_SHARED_SQLITE_OK"] = "true"
            payload = {"secret": "local-smoke-secret", "direction": "UP", "symbol": "BTCUSDT",
                       "timeframe": "1m", "timestamp": datetime.now(timezone.utc).isoformat()}
            received = await main.gainzalgo_webhook(FakeRequest(payload))
            assert received["status"] == "recorded", received
            duplicate = await main.gainzalgo_webhook(FakeRequest(payload))
            assert duplicate["status"] == "duplicate_ignored", duplicate
            result = paper_worker.run_once()
            assert result.get("action") == "paper_opened", result
            paper_worker._write_heartbeat(result)
            with main.db_connect() as conn:
                alert_status = conn.execute("SELECT processing_status FROM gainzalgo_signals ORDER BY id DESC LIMIT 1").fetchone()[0]
                open_count = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE settled_at IS NULL").fetchone()[0]
            dashboard = main.dashboard_data()
            assert alert_status == "processed", alert_status
            assert open_count == 1, open_count
            assert dashboard["external_alert_count"] == 1
            assert dashboard["latest_webhook_event"]["status"] == "duplicate"
            assert dashboard["latest_external_alert"]["processing_status"] == "processed"
            assert dashboard["health"]["worker_status"] == "running"
            print("PASS: webhook received -> shared queue -> hybrid decision -> paper trade -> dashboard heartbeat")
            print(f"Decision: {result['action']} {result['direction']} {result['ticker']} at {result['entry_cents']:.2f}¢ (SIMULATION_ONLY)")

        asyncio.run(exercise())
    finally:
        temp.cleanup()


if __name__ == "__main__":
    main()
