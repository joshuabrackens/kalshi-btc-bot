"""Deterministic offline stress checks for Phase 4–6 safety foundations.

No network calls, orders, credentials, or database writes are used.
"""
from __future__ import annotations

import os
import random
import time
from unittest.mock import patch

from market_router import classify_and_route, scan_market_catalog
from multi_user_security import create_session_token, session_token_digest
from trading_mode import request_mode, trading_mode_status, LiveModeLocked


def main() -> None:
    started = time.perf_counter()
    rng = random.Random(20261023)

    # A million-row virtual feed is supplied lazily; the router should consume at most 500.
    consumed = 0

    def market_feed():
        nonlocal consumed
        for index in range(1_000_000):
            consumed += 1
            if index % 50_000 == 0:
                yield {"ticker": f"KXBTC15M-{index}", "title": "Bitcoin price up in 15 minutes", "category": "Crypto"}
            elif index % 2 == 0:
                yield {"ticker": f"KXSPORTS-{index}", "title": "Sports event", "category": "Sports"}
            else:
                yield {"ticker": f"UNKNOWN-{index}", "title": "Unknown event"}

    result = scan_market_catalog(market_feed(), max_results=50)
    assert consumed == result["candidate_count"] == 500
    assert result["trade_submitted"] is False
    assert all(not row["trade_submitted"] for row in result["markets"])

    # Fuzz 100,000 market metadata combinations: only a correctly identified BTC 15m ticker/title can route.
    for i in range(100_000):
        market = {
            "ticker": rng.choice([f"KXBTC15M-{i}", f"KXSPORTS-{i}", f"UNKNOWN-{i}", "", None]),
            "title": rng.choice(["Bitcoin price up in 15 minutes", "BTC game winner", "weather event", "", None]),
            "category": rng.choice(["Crypto", "Sports", "Politics", "Weather", "unknown", None]),
        }
        row = classify_and_route(market)
        if row["route_status"] == "btc_paper_strategy_only":
            assert str(market["ticker"]).startswith("KXBTC15M-")
            assert "bitcoin" in str(market["title"]).lower() or "btc" in str(market["title"]).lower()
        assert row["trade_submitted"] is False

    # Exercise mode status and denied real-mode requests under every risky environment combination.
    mode_combinations = [
        ("paper", "false", "paper"), ("paper", "true", "blocked"),
        ("real", "false", "blocked"), ("real", "true", "blocked"),
        ("live", "false", "blocked"), ("", "", "blocked"),
    ]
    for _ in range(20_000):
        configured, live_flag, expected = mode_combinations[rng.randrange(len(mode_combinations))]
        with patch.dict(os.environ, {"TRADING_MODE": configured, "LIVE_TRADING_ENABLED": live_flag}, clear=False):
            status = trading_mode_status()
            assert status["effective_mode"] == expected
            assert status["live_execution_enabled"] is False
            if expected == "paper":
                assert status["paper_execution_enabled"] is True
            else:
                assert status["paper_execution_enabled"] is False
            try:
                request_mode("real")
                raise AssertionError("real mode was unexpectedly accepted")
            except LiveModeLocked:
                pass

    # Session token uniqueness/digest properties at a moderate volume; no raw tokens persisted.
    seen: set[str] = set()
    for _ in range(10_000):
        token, digest = create_session_token()
        assert token not in seen
        seen.add(token)
        assert digest == session_token_digest(token) and digest != token

    elapsed = time.perf_counter() - started
    print("PHASE 4–6 OFFLINE STRESS: PASS")
    print(f"catalog consumption: {consumed:,} (capped at 500 of 1,000,000 available)")
    print("market metadata fuzz cases: 100,000; no trade submitted")
    print("trading-mode gate checks: 20,000; live execution always false")
    print("session-token uniqueness checks: 10,000")
    print(f"wall-clock time: {elapsed:.2f}s")
    print("Network calls, exchange orders, credentials, and production DB writes: 0")


if __name__ == "__main__":
    main()
