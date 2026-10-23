"""HTTP coverage for the safe multi-market, dashboard mode, and readiness gates."""
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


def test_dashboard_mode_request_cannot_enable_real_trading():
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "mode.sqlite3"),
               "PAPER_WORKER_ENABLED": "false", "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false"}
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






def test_concurrent_market_scan_clicks_are_coalesced_by_cache_lock():
    main._clear_market_scan_cache_for_tests()
    markets = [{"ticker": "UNKNOWN-CONCURRENT", "title": "Example market"}]

    def slow_provider(**kwargs):
        time.sleep(0.015)
        return {"markets": markets}

    with patch.object(main, "get_markets", side_effect=slow_provider) as provider:
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _index: main.api_market_scan(), range(24)))
    assert provider.call_count == 1
    assert sum(1 for result in results if result["cache_hit"] is False) == 1
    assert all(result["trade_submitted"] is False for result in results)
    main._clear_market_scan_cache_for_tests()


def test_market_scan_refreshes_after_cache_ttl_expires():
    main._clear_market_scan_cache_for_tests()
    markets1 = [{"ticker": "UNKNOWN-ONE", "title": "First snapshot"}]
    markets2 = [{"ticker": "UNKNOWN-TWO", "title": "Second snapshot"}]
    with patch.object(main, "MARKET_SCAN_CACHE_TTL_SECONDS", 5), patch.object(
        main, "get_markets", side_effect=[{"markets": markets1}, {"markets": markets2}]
    ) as provider:
        first = main.api_market_scan()
        with main._market_scan_cache_lock:
            main._market_scan_cache_fetched_at = time.monotonic() - 10
        second = main.api_market_scan()
    assert provider.call_count == 2
    assert first["markets"][0]["ticker"] == "UNKNOWN-ONE"
    assert second["markets"][0]["ticker"] == "UNKNOWN-TWO"
    assert second["cache_hit"] is False
    main._clear_market_scan_cache_for_tests()


def test_readiness_endpoints_report_gaps_without_enabling_trading():
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "readiness.sqlite3"),
               "PAPER_WORKER_ENABLED": "false", "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false"}
        with patch.dict(os.environ, env, clear=False):
            with TestClient(main.app) as client:
                live = client.get("/api/live-readiness").json()
                multi = client.get("/api/multi-user-readiness").json()
                assert live["live_ready"] is False and live["live_execution_enabled"] is False
                assert live["passed_checks"] < live["total_checks"]
                assert multi["enabled"] is False and multi["production_ready"] is False
                assert client.get("/api/trading-mode").json()["live_execution_enabled"] is False


def test_market_scan_only_classifies_candidates_and_never_submits_orders():
    main._clear_market_scan_cache_for_tests()
    markets = [
        {"ticker": "KXBTC15M-TEST", "title": "Bitcoin price up next 15 minutes", "category": "Crypto"},
        {"ticker": "KXSPORTS-TEST", "title": "Who wins?", "category": "Sports"},
        {"ticker": "UNK-TEST", "title": "Mystery event"},
    ]
    with patch.object(main, "get_markets", side_effect=[
        {"markets": markets, "cursor": "next"},
        {"markets": [], "cursor": ""},
    ]):
        response = main.api_market_scan()
    assert response["candidate_count"] == 3
    assert response["trade_submitted"] is False
    assert response["cursor_available"] is False
    assert response["pages_scanned"] == 2
    statuses = {row["ticker"]: row["route_status"] for row in response["markets"]}
    assert statuses["KXBTC15M-TEST"] == "btc_paper_strategy_only"
    assert statuses["KXSPORTS-TEST"] == "research_only"
    assert statuses["UNK-TEST"] == "blocked_unclassified"
    main._clear_market_scan_cache_for_tests()


def test_market_scan_caps_pagination_at_five_pages_and_handles_repeated_cursor():
    main._clear_market_scan_cache_for_tests()
    page = {"markets": [{"ticker": "UNKNOWN-PAGE", "title": "Example"}], "cursor": "same-cursor"}
    with patch.object(main, "get_markets", return_value=page) as provider:
        result = main.api_market_scan()
    assert provider.call_count == 2  # The repeated cursor is detected on the second page.
    assert result["pages_scanned"] == 2
    assert result["cursor_available"] is True
    assert "cursor repeated" in result["pagination_warning"]
    assert result["trade_submitted"] is False
    main._clear_market_scan_cache_for_tests()


def test_market_scan_cache_limits_repeat_upstream_catalog_requests():
    main._clear_market_scan_cache_for_tests()
    markets = [{"ticker": "UNKNOWN-CACHE", "title": "Example market"}]
    with patch.object(main, "get_markets", return_value={"markets": markets, "cursor": ""}) as provider:
        first = main.api_market_scan()
        second = main.api_market_scan()
    assert provider.call_count == 1
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert second["cache_age_seconds"] >= 0
    assert second["trade_submitted"] is False
    main._clear_market_scan_cache_for_tests()


def test_market_scan_stops_before_sending_an_overlong_cursor():
    main._clear_market_scan_cache_for_tests()
    first_page = {"markets": [{"ticker": "UNKNOWN-LONG-CURSOR", "title": "Example"}],
                  "cursor": "x" * (main.MARKET_SCAN_MAX_CURSOR_LENGTH + 1)}
    with patch.object(main, "get_markets", return_value=first_page) as provider:
        result = main.api_market_scan()
    assert provider.call_count == 1
    assert result["pages_scanned"] == 1
    assert result["cursor_available"] is True
    assert "safety length" in result["pagination_warning"]
    assert result["trade_submitted"] is False
    main._clear_market_scan_cache_for_tests()


def test_failed_market_scan_is_not_cached_and_can_recover():
    from kalshi_api import KalshiAPIError

    main._clear_market_scan_cache_for_tests()
    markets = [{"ticker": "UNKNOWN-RECOVERED", "title": "Recovery example"}]
    with patch.object(main, "get_markets", side_effect=[KalshiAPIError("temporary outage"), {"markets": markets}]) as provider:
        with pytest.raises(HTTPException) as exc:
            main.api_market_scan()
        assert exc.value.status_code == 502
        result = main.api_market_scan()
    assert provider.call_count == 2
    assert result["cache_hit"] is False
    assert result["markets"][0]["ticker"] == "UNKNOWN-RECOVERED"
    main._clear_market_scan_cache_for_tests()


def test_market_scan_stops_when_upstream_cursor_is_not_text():
    main._clear_market_scan_cache_for_tests()
    malformed_page = {"markets": [{"ticker": "UNKNOWN-CURSOR-TYPE", "title": "Example"}], "cursor": {"next": "page"}}
    with patch.object(main, "get_markets", return_value=malformed_page) as provider:
        result = main.api_market_scan()
    assert provider.call_count == 1
    assert result["pages_scanned"] == 1
    assert result["cursor_available"] is True
    assert "non-text cursor" in result["pagination_warning"]
    assert result["trade_submitted"] is False
    main._clear_market_scan_cache_for_tests()
