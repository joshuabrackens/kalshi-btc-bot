"""HTTP, full-market-scan progress, and fail-closed mode tests."""
from __future__ import annotations

import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
from kalshi_api import KalshiAPIError


def wait_until_terminal(scanner, timeout=4):
    deadline = time.monotonic() + timeout
    last = scanner.snapshot()
    while time.monotonic() < deadline:
        last = scanner.snapshot()
        if last["status"] != "running":
            return last
        time.sleep(0.005)
    raise AssertionError(f"Scanner did not finish in {timeout}s; last status={last['status']}")


def setup_scanner(max_pages=20):
    main._clear_market_scan_cache_for_tests()
    main.market_scanner._max_pages = max_pages
    main.market_scanner._max_markets = 100_000
    main.market_scanner._page_delay = 0


def test_dashboard_mode_request_cannot_enable_real_trading():
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "mode.sqlite3"),
               "PAPER_WORKER_ENABLED": "false", "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
               "MARKET_SCANNER_AUTO_START": "false"}
        with patch.dict(os.environ, env, clear=False):
            with TestClient(main.app) as client:
                before = client.get("/api/trading-mode").json()
                assert before["effective_mode"] == "paper"
                assert before["live_execution_enabled"] is False
                denied = client.post("/api/trading-mode", json={"requested_mode": "real"})
                assert denied.status_code == 409
                body = denied.json()["detail"]
                assert body["code"] == "live_mode_locked"
                assert body["live_execution_enabled"] is False
                after = client.get("/api/trading-mode").json()
                assert after["effective_mode"] == "paper"
                assert after["live_execution_enabled"] is False


def test_concurrent_scan_start_requests_are_coalesced():
    setup_scanner()
    markets = [{"ticker": "UNKNOWN-CONCURRENT", "title": "Example market"}]
    def slow_provider(**kwargs):
        time.sleep(0.04)
        return {"markets": markets, "cursor": ""}
    with patch.object(main, "get_markets", side_effect=slow_provider) as provider:
        with ThreadPoolExecutor(max_workers=12) as pool:
            starts = list(pool.map(lambda _index: main.market_scanner.start(force=True), range(24)))
        final = wait_until_terminal(main.market_scanner)
    assert provider.call_count == 1
    assert final["status"] == "complete"
    assert final["candidate_count"] == 1
    assert all(start["trade_submitted"] is False for start in starts)
    main._clear_market_scan_cache_for_tests()


def test_full_catalog_scan_follows_multiple_pages_and_classifies_all_rows():
    setup_scanner()
    first = [{"ticker": "KXBTC15M-TEST", "title": "BTC price up next 15 minutes", "category": "Crypto"}]
    second = [{"ticker": "KXSPORTS-TEST", "title": "Who wins?", "category": "Sports"}, {"ticker": "UNK-TEST", "title": "Mystery event"}]
    with patch.object(main, "get_markets", side_effect=[{"markets": first, "cursor": "page-2"}, {"markets": second, "cursor": ""}]) as provider:
        main.market_scanner.start(force=True)
        result = wait_until_terminal(main.market_scanner)
    assert result["status"] == "complete"
    assert provider.call_count == 2
    assert result["candidate_count"] == 3
    assert result["pages_scanned"] == 2
    assert result["category_counts"]["btc_15m"] == 1
    assert result["route_counts"]["research_only"] == 1
    assert result["route_counts"]["blocked_unclassified"] == 1
    assert result["trade_submitted"] is False
    assert result["markets"][0]["market_implied_yes_probability_pct"] is None
    main._clear_market_scan_cache_for_tests()


def test_scan_stops_on_repeated_cursor_and_can_be_restarted():
    setup_scanner()
    page = {"markets": [{"ticker": "UNKNOWN-PAGE", "title": "Example"}], "cursor": "same-cursor"}
    with patch.object(main, "get_markets", return_value=page) as provider:
        main.market_scanner.start(force=True)
        result = wait_until_terminal(main.market_scanner)
    assert result["status"] == "partial"
    assert provider.call_count == 2
    assert "repeated" in result["pagination_warning"]
    assert result["trade_submitted"] is False
    main._clear_market_scan_cache_for_tests()


def test_scan_stops_at_overlong_cursor():
    setup_scanner()
    first_page = {"markets": [{"ticker": "UNKNOWN-LONG-CURSOR", "title": "Example"}], "cursor": "x" * 513}
    with patch.object(main, "get_markets", return_value=first_page) as provider:
        main.market_scanner.start(force=True)
        result = wait_until_terminal(main.market_scanner)
    assert provider.call_count == 1
    assert result["status"] == "partial"
    assert "512 characters" in result["pagination_warning"]
    main._clear_market_scan_cache_for_tests()


def test_scan_failure_is_fail_safe_and_retry_resumes():
    setup_scanner()
    rows = [{"ticker": "UNKNOWN-RECOVERED", "title": "Recovery example"}]
    with patch.object(main, "get_markets", side_effect=[{"markets": [], "cursor": "next"}, KalshiAPIError("upstream private detail"), {"markets": rows, "cursor": ""}]) as provider:
        main.market_scanner.start(force=True)
        failed = wait_until_terminal(main.market_scanner)
        assert failed["status"] == "failed"
        assert failed["error_code"] == "kalshi_catalog_unavailable"
        assert "upstream private detail" not in failed["error_message"]
        main.market_scanner.start(force=True)
        recovered = wait_until_terminal(main.market_scanner)
    assert provider.call_count == 3
    assert recovered["status"] == "complete"
    assert recovered["candidate_count"] == 1
    assert recovered["markets"][0]["ticker"] == "UNKNOWN-RECOVERED"
    main._clear_market_scan_cache_for_tests()


def test_live_readiness_and_multiuser_readiness_remain_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "readiness.sqlite3"),
               "PAPER_WORKER_ENABLED": "false", "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
               "MARKET_SCANNER_AUTO_START": "false"}
        with patch.dict(os.environ, env, clear=False):
            with TestClient(main.app) as client:
                live = client.get("/api/live-readiness").json()
                multi = client.get("/api/multi-user-readiness").json()
                assert live["live_ready"] is False and live["live_execution_enabled"] is False
                assert live["passed_checks"] < live["total_checks"]
                assert multi["enabled"] is False and multi["production_ready"] is False
                assert client.get("/api/trading-mode").json()["live_execution_enabled"] is False


def test_public_scan_routes_only_inventory_no_trade_calls():
    setup_scanner()
    with patch.object(main, "get_markets", return_value={"markets": [{"ticker": "KXBTC15M-TEST", "title": "BTC price up next 15 minutes", "category": "Crypto"}], "cursor": ""}):
        with TestClient(main.app) as client:
            started = client.post("/api/market-scan").json()
            assert started["status"] in {"running", "complete"}
            result = None
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = client.get("/api/market-scan").json()
                if result["status"] != "running":
                    break
                time.sleep(0.01)
            assert result["status"] == "complete"
            assert result["candidate_count"] == 1
            assert result["trade_submitted"] is False
    main._clear_market_scan_cache_for_tests()


def test_catalog_start_endpoint_returns_progress_not_upstream_502():
    setup_scanner()
    with patch.object(main, "get_markets", side_effect=KalshiAPIError("temporary outage")):
        with TestClient(main.app) as client:
            response = client.post("/api/market-scan")
            assert response.status_code == 200
            result = None
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = client.get("/api/market-scan").json()
                if result["status"] != "running":
                    break
                time.sleep(0.01)
            assert result["status"] == "failed"
            assert result["error_code"] == "kalshi_catalog_unavailable"
    main._clear_market_scan_cache_for_tests()


def test_scanner_retains_bounded_top_activity_analysis_pool():
    setup_scanner(max_pages=20)
    rows = [
        {"ticker": f"UNK-{i}", "title": "Example", "volume_24h_fp": str(i), "open_interest_fp": str(i * 2)}
        for i in range(1, 80)
    ]
    with patch.object(main, "get_markets", return_value={"markets": rows, "cursor": ""}):
        main.market_scanner.start(force=True)
        result = wait_until_terminal(main.market_scanner)
    pool = main.market_scanner.analysis_snapshot()["analysis_candidates"]
    assert len(pool) == 79
    volumes = [float(row["volume_24h"]) for row in pool]
    assert volumes == sorted(volumes, reverse=True)
    assert result["candidate_count"] == 79
    main._clear_market_scan_cache_for_tests()


def test_market_analysis_run_endpoint_requires_admin_session():
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "market-analysis-admin.sqlite3"),
               "PAPER_WORKER_ENABLED": "false", "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
               "MARKET_SCANNER_AUTO_START": "false", "MARKET_ANALYSIS_AUTO_START": "false",
               "KALSHI_VAULT_ADMIN_SECRET": "test-admin-secret-that-is-at-least-32-bytes"}
        with patch.dict(os.environ, env, clear=False):
            with TestClient(main.app) as client:
                response = client.post("/api/market-analysis/run", json={})
                assert response.status_code == 401


def test_market_autopilot_is_admin_gated_and_requires_provider_and_paper_gate(tmp_path):
    admin_secret = "test-admin-secret-for-automation-123456789"
    env = {
        "DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp_path) / "auto.sqlite3"),
        "KALSHI_VAULT_ADMIN_SECRET": admin_secret,
        "PAPER_ADMIN_API_SECRET": "",
        "MARKET_ANALYSIS_PROVIDER_URL": "", "MARKET_AUTOPAPER_ENABLED": "false",
        "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
        "MARKET_SCANNER_AUTO_START": "false", "MARKET_ANALYSIS_AUTO_START": "false",
    }
    with patch.dict(os.environ, env, clear=False):
        with TestClient(main.app) as client:
            state = client.get("/api/market-automation").json()
            assert state["autopilot_enabled"] is False
            assert state["real_orders_enabled"] is False
            assert client.post("/api/market-automation", json={"enabled": True}).status_code == 401
            login = client.post("/api/admin/login", json={"secret": admin_secret})
            assert login.status_code == 200
            no_provider = client.post("/api/market-automation", json={"enabled": True})
            assert no_provider.status_code == 409
            assert client.get("/api/market-automation").json()["autopilot_enabled"] is False
            off = client.post("/api/market-automation", json={"enabled": False})
            assert off.status_code == 200 and off.json()["autopilot_enabled"] is False


def test_market_autopilot_enables_only_paper_research_when_every_gate_passes(tmp_path):
    from runtime_controls import market_autopilot_enabled
    admin_secret = "test-admin-secret-for-automation-abcdefghijkl"
    env = {
        "DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp_path) / "auto-on.sqlite3"),
        "KALSHI_VAULT_ADMIN_SECRET": admin_secret,
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze",
        "MARKET_AUTOPAPER_ENABLED": "true", "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
        "MARKET_SCANNER_AUTO_START": "false", "MARKET_ANALYSIS_AUTO_START": "false",
    }
    with patch.dict(os.environ, env, clear=False), \
         patch.object(main.market_scanner, "start_scheduler") as scan_scheduler, \
         patch.object(main.market_decision_pipeline, "start_scheduler") as analysis_scheduler, \
         patch.object(main.market_scanner, "start", return_value={"status": "running"}) as start_scan:
        with TestClient(main.app) as client:
            assert client.post("/api/admin/login", json={"secret": admin_secret}).status_code == 200
            result = client.post("/api/market-automation", json={"enabled": True})
            assert result.status_code == 200, result.text
            payload = result.json()
            assert payload["autopilot_enabled"] is True
            assert payload["scanner_enabled"] is True
            assert payload["real_orders_enabled"] is False
            assert market_autopilot_enabled() is True
            scan_scheduler.assert_any_call(enabled=True)
            analysis_scheduler.assert_any_call(enabled=True)
            start_scan.assert_called_once_with(force=False)
            paused = client.post("/api/market-automation", json={"enabled": False})
            assert paused.status_code == 200 and paused.json()["autopilot_enabled"] is False
            assert market_autopilot_enabled() is False


def test_market_autopilot_refuses_un_safe_mode_and_invalid_payload(tmp_path):
    admin_secret = "test-admin-secret-for-automation-987654321"
    env = {
        "DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp_path) / "auto-unsafe.sqlite3"),
        "KALSHI_VAULT_ADMIN_SECRET": admin_secret,
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze",
        "MARKET_AUTOPAPER_ENABLED": "true", "TRADING_MODE": "real", "LIVE_TRADING_ENABLED": "false",
        "MARKET_SCANNER_AUTO_START": "false", "MARKET_ANALYSIS_AUTO_START": "false",
    }
    with patch.dict(os.environ, env, clear=False):
        with TestClient(main.app) as client:
            client.post("/api/admin/login", json={"secret": admin_secret})
            bad_shape = client.post("/api/market-automation", json={"enabled": "true"})
            assert bad_shape.status_code == 400
            unsafe = client.post("/api/market-automation", json={"enabled": True})
            assert unsafe.status_code == 409
            assert client.get("/api/market-automation").json()["autopilot_enabled"] is False
