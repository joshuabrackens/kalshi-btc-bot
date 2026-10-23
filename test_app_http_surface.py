"""HTTP-level smoke tests for the safe read surface and admin-gated journal writes."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


def _evaluate_body():
    candles = []
    for i in range(25):
        close = 100 + i * 0.1
        candles.append({"timestamp": f"2026-10-23T00:{i:02d}:00+00:00", "open": close - 0.05,
                        "high": close + 0.1, "low": close - 0.1, "close": close, "volume": 10})
    return {"candles_1m": candles, "candles_5m": candles, "candles_15m": candles,
            "market": {"market_ticker": "KXBTC15M-TEST", "strike_price": 100,
                       "seconds_remaining": 600, "up_ask_cents": 50, "down_ask_cents": 50,
                       "spread_cents": 2, "estimated_fee_cents": 2}}


def test_read_only_routes_work_and_manual_writes_are_disabled_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "http.sqlite3"),
               "PAPER_WORKER_ENABLED": "false", "PAPER_ADMIN_API_SECRET": ""}
        with patch.dict(os.environ, env, clear=False):
            with TestClient(main.app) as client:
                assert client.get("/health").status_code == 200
                dashboard_response = client.get("/api/dashboard")
                assert dashboard_response.status_code == 200, dashboard_response.text
                dashboard_data = dashboard_response.json()
                assert dashboard_data["mode"] == "SIMULATION_ONLY"
                assert "current_window_decision" in dashboard_data
                assert "active_position" in dashboard_data
                assert client.get("/api/category-readiness").json()["current_active_strategy"] == "btc_15m"
                assert client.get("/api/analytics").json()["mode"] == "SIMULATION_ONLY"
                denied_open = client.post("/paper/open", json={
                    "market_ticker": "KXBTC15M-TEST", "direction": "UP", "contracts": 1,
                    "entry_cents": 50, "fee_cents_per_contract": 2,
                })
                assert denied_open.status_code == 403
                denied_evaluate = client.post("/evaluate", json=_evaluate_body())
                assert denied_evaluate.status_code == 403


def test_configured_manual_api_secret_allows_simulated_open_and_settlement_only():
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "manual.sqlite3"),
               "PAPER_WORKER_ENABLED": "false", "PAPER_ADMIN_API_SECRET": "test-admin-secret",
               "MAX_TRADE_DOLLARS": "1.00", "MAX_DAILY_LOSS_DOLLARS": "10.00",
               "PAPER_MAX_TRADES_PER_DAY": "10", "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false"}
        with patch.dict(os.environ, env, clear=False):
            with TestClient(main.app) as client:
                payload = {"market_ticker": "KXBTC15M-TEST", "direction": "UP", "contracts": 1,
                           "entry_cents": 50, "fee_cents_per_contract": 2}
                unauthorized = client.post("/paper/open", json=payload)
                assert unauthorized.status_code == 401
                headers = {"X-Paper-Admin-Secret": "test-admin-secret"}
                opened = client.post("/paper/open", json=payload, headers=headers)
                assert opened.status_code == 200, opened.text
                trade_id = opened.json()["paper_trade_id"]
                assert opened.json()["mode"] == "SIMULATION_ONLY"
                settled = client.post(f"/paper/{trade_id}/settle", json={"outcome": "UP"}, headers=headers)
                assert settled.status_code == 200, settled.text
                assert settled.json()["net_pnl_cents"] == 48
                assert settled.json()["mode"] == "SIMULATION_ONLY"
