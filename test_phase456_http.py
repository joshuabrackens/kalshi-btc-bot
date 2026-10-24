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
    with patch.object(main, "get_events", side_effect=slow_provider) as provider:
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
    with patch.object(main, "get_events", side_effect=[{"markets": first, "cursor": "page-2"}, {"markets": second, "cursor": ""}]) as provider:
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



def test_scanner_flattens_official_events_into_markets_with_category_metadata():
    setup_scanner()
    event_page = {
        "events": [
            {
                "event_ticker": "KXNBA-26OCT24-LALBOS",
                "series_ticker": "KXNBA",
                "title": "Lakers at Celtics",
                "category": "Sports",
                "status": "open",
                "markets": [
                    {"ticker": "KXNBA-26OCT24-LALBOS-LAL", "event_ticker": "KXNBA-26OCT24-LALBOS",
                     "title": "Will the Lakers win?", "category": "Politics", "status": "active", "volume_24h_fp": "125.0",
                     "yes_ask_dollars": "0.4300", "no_ask_dollars": "0.5800"},
                    {"ticker": "KXNBA-26OCT24-LALBOS-BOS", "event_ticker": "KXNBA-26OCT24-LALBOS",
                     "title": "Will the Celtics win?", "status": "active", "volume_24h_fp": "100.0"},
                    {"ticker": "KXNBA-26OCT24-LALBOS-PAUSED", "event_ticker": "KXNBA-26OCT24-LALBOS",
                     "title": "Paused market", "status": "paused", "volume_24h_fp": "1000.0"},
                ],
            },
        ],
        "cursor": "",
    }
    with patch.object(main, "get_events", return_value=event_page) as provider, \
         patch.object(main, "get_multivariate_events", return_value={"events": [], "cursor": ""}) as multivariate:
        main.market_scanner.start(force=True)
        result = wait_until_terminal(main.market_scanner)
    assert result["status"] == "complete"
    assert provider.call_args.kwargs["with_nested_markets"] is True
    assert multivariate.call_count == 1
    assert result["candidate_count"] == 2
    assert result["events_scanned"] == 1
    assert result["category_counts"]["sports"] == 2
    sample = {row["ticker"]: row for row in result["markets"]}
    assert sample["KXNBA-26OCT24-LALBOS-LAL"]["category_source"] == "kalshi_events_api"
    assert sample["KXNBA-26OCT24-LALBOS-LAL"]["event_title"] == "Lakers at Celtics"
    assert sample["KXNBA-26OCT24-LALBOS-LAL"]["route_status"] == "research_only"
    assert "injury/lineup/stat provider" in sample["KXNBA-26OCT24-LALBOS-LAL"]["context_status"]
    main._clear_market_scan_cache_for_tests()

def test_catalog_provider_paginates_standard_and_multivariate_event_feeds():
    standard_pages = [
        {"events": [{"event_ticker": "EVT-A", "category": "Sports", "markets": []}], "cursor": "standard-page-2"},
        {"events": [{"event_ticker": "EVT-B", "category": "Companies", "markets": []}], "cursor": ""},
    ]
    multi_pages = [
        {"events": [{"event_ticker": "MVE-A", "category": "Entertainment", "markets": []}], "cursor": "multi-page-2"},
        {"events": [], "cursor": ""},
    ]
    with patch.object(main, "get_events", side_effect=standard_pages) as standard, \
         patch.object(main, "get_multivariate_events", side_effect=multi_pages) as multivariate:
        p1 = main._get_market_catalog_page(status="open", limit=200, cursor=None)
        assert p1["_catalog_source"] == "events"
        assert p1["cursor"].startswith(main._CATALOG_EVENTS_PREFIX)
        p2 = main._get_market_catalog_page(status="open", limit=200, cursor=p1["cursor"])
        assert p2["_catalog_source"] == "events"
        assert p2["cursor"] == main._CATALOG_MVE_START
        p3 = main._get_market_catalog_page(status="open", limit=200, cursor=p2["cursor"])
        assert p3["_catalog_source"] == "multivariate_events"
        assert p3["cursor"].startswith(main._CATALOG_MVE_PREFIX)
        p4 = main._get_market_catalog_page(status="open", limit=200, cursor=p3["cursor"])
        assert p4["_catalog_source"] == "multivariate_events"
        assert p4["cursor"] == ""
    assert standard.call_count == 2
    assert multivariate.call_count == 2
    assert all(call.kwargs.get("with_nested_markets") is True for call in standard.call_args_list)
    assert all(call.kwargs.get("with_nested_markets") is True for call in multivariate.call_args_list)


def test_scan_stops_on_repeated_cursor_and_can_be_restarted():
    setup_scanner()
    page = {"markets": [{"ticker": "UNKNOWN-PAGE", "title": "Example"}], "cursor": "same-cursor"}
    with patch.object(main, "get_events", return_value=page) as provider:
        main.market_scanner.start(force=True)
        result = wait_until_terminal(main.market_scanner)
    assert result["status"] == "partial"
    assert provider.call_count == 2
    assert "repeated" in result["pagination_warning"]
    assert result["trade_submitted"] is False
    main._clear_market_scan_cache_for_tests()


def test_scan_stops_at_overlong_cursor():
    setup_scanner()
    first_page = {"markets": [{"ticker": "UNKNOWN-LONG-CURSOR", "title": "Example"}], "cursor": "x" * 2049}
    with patch.object(main, "get_events", return_value=first_page) as provider:
        main.market_scanner.start(force=True)
        result = wait_until_terminal(main.market_scanner)
    assert provider.call_count == 1
    assert result["status"] == "partial"
    assert "2048 characters" in result["pagination_warning"]
    main._clear_market_scan_cache_for_tests()


def test_scan_failure_is_fail_safe_and_retry_resumes():
    setup_scanner()
    rows = [{"ticker": "UNKNOWN-RECOVERED", "title": "Recovery example"}]
    with patch.object(main, "get_events", side_effect=[{"markets": [], "cursor": "next"}, KalshiAPIError("upstream private detail"), {"markets": rows, "cursor": ""}]) as provider:
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




def test_scanner_retries_transient_rate_limit_and_exposes_safe_status():
    setup_scanner()
    main.market_scanner._page_retries = 1
    rows = [{"ticker": "UNKNOWN-AFTER-429", "title": "Example"}]
    with patch.object(main, "get_events", side_effect=[
        KalshiAPIError("rate limited", status_code=429, retryable=True),
        {"markets": rows, "cursor": ""},
    ]) as provider, patch("market_catalog_scanner.time.sleep"):
        main.market_scanner.start(force=True)
        result = wait_until_terminal(main.market_scanner)
    assert provider.call_count == 2
    assert result["status"] == "complete"
    assert result["candidate_count"] == 1
    assert result["page_retries"] == 1
    assert result["error_message"] is None
    main._clear_market_scan_cache_for_tests()


def test_scanner_restarts_once_after_kalshi_rejects_continuation_cursor():
    setup_scanner()
    first_page = {"markets": [{"ticker": "UNKNOWN-FIRST-PASS", "title": "First page"}], "cursor": "stale-cursor"}
    restarted_first_page = {"markets": [{"ticker": "UNKNOWN-RESTARTED", "title": "Fresh first page"}], "cursor": ""}
    with patch.object(main, "get_events", side_effect=[
        first_page,
        KalshiAPIError("cursor rejected", status_code=400, retryable=False),
        restarted_first_page,
    ]) as provider:
        main.market_scanner.start(force=True)
        result = wait_until_terminal(main.market_scanner)
    assert provider.call_count == 3
    assert result["status"] == "complete"
    assert result["candidate_count"] == 1
    assert result["markets"][0]["ticker"] == "UNKNOWN-RESTARTED"
    assert result["pagination_warning"] is None
    main._clear_market_scan_cache_for_tests()


def test_scanner_failure_exposes_upstream_http_status_and_page_number():
    setup_scanner()
    with patch.object(main, "get_events", side_effect=KalshiAPIError("bad request", status_code=400, retryable=False)):
        main.market_scanner.start(force=True)
        result = wait_until_terminal(main.market_scanner)
    assert result["status"] == "failed"
    assert result["upstream_status_code"] == 400
    assert result["failed_page_number"] == 1
    assert result["error_code"] == "kalshi_catalog_http_error"
    assert "HTTP 400" in result["error_message"]
    main._clear_market_scan_cache_for_tests()




def test_lifespan_restores_schedulers_when_persistent_autopilot_is_already_on(tmp_path):
    env = {
        "DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp_path) / "restart.sqlite3"),
        "PAPER_WORKER_ENABLED": "false", "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
        "MARKET_SCANNER_AUTO_START": "false", "MARKET_ANALYSIS_AUTO_START": "false",
    }
    with patch.dict(os.environ, env, clear=False), \
         patch.object(main, "market_autopilot_enabled", return_value=True), \
         patch.object(main.market_scanner, "start_scheduler") as scan_scheduler, \
         patch.object(main.market_decision_pipeline, "start_scheduler") as analysis_scheduler:
        with TestClient(main.app):
            pass
    scan_scheduler.assert_called_once_with(enabled=True)
    analysis_scheduler.assert_called_once_with(enabled=True)


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
    with patch.object(main, "get_events", return_value={"markets": [{"ticker": "KXBTC15M-TEST", "title": "BTC price up next 15 minutes", "category": "Crypto"}], "cursor": ""}):
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
    with patch.object(main, "get_events", side_effect=KalshiAPIError("temporary outage")):
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
        {"ticker": f"KXNBA-TEST-{i}", "title": "Who wins this basketball game?", "category": "Sports",
         "status": "active", "volume_24h_fp": str(i), "open_interest_fp": str(i * 2)}
        for i in range(1, 80)
    ]
    with patch.object(main, "get_events", return_value={"markets": rows, "cursor": ""}):
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
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze", "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token",
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
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze", "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token",
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
