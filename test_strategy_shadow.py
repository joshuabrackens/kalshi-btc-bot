"""Tests for same-entry hold-to-settlement strategy shadow comparison."""
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import db_store
from strategy_shadow import (
    HOLD_TO_SETTLEMENT_KEY,
    build_strategy_comparison_report,
    ensure_hold_to_settlement_shadow_rows,
    process_hold_to_settlement_shadow,
)


def _new_db(tmp: str):
    conn = db_store.connect_database()
    db_store.ensure_schema(conn)
    return conn


def _insert_trade(conn, *, ticker, direction, entry, fee, pnl, outcome, settled_at, notes):
    now = "2026-10-24T07:00:00+00:00"
    cur = conn.execute(
        """INSERT INTO paper_trades
           (created_at,settled_at,market_ticker,direction,contracts,entry_cents,
            fee_cents_per_contract,outcome,pnl_cents,notes,entry_signal_source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (now, settled_at, ticker, direction, 1, entry, fee, outcome, pnl, notes,
         "hybrid" if "hybrid" in notes.lower() else None),
    )
    conn.commit()
    return cur.lastrowid


def test_hold_to_settlement_shadow_compares_same_entry_without_mutating_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "shadow.sqlite3")}):
            conn = _new_db(tmp)
            try:
                stopped_id = _insert_trade(
                    conn, ticker="KXBTC15M-SHADOW-A", direction="UP", entry=60, fee=2,
                    pnl=-10, outcome="TREND_STOP_LOSS", settled_at="2026-10-24T07:05:00+00:00",
                    notes="TradingView-primary hybrid paper entry; simulated chart-stop exit",
                )
                held_id = _insert_trade(
                    conn, ticker="KXBTC15M-SHADOW-B", direction="DOWN", entry=70, fee=2,
                    pnl=28, outcome="DOWN", settled_at="2026-10-24T07:10:00+00:00",
                    notes="AUTO PAPER ENTRY; automatically settled from official Kalshi final result",
                )

                created = ensure_hold_to_settlement_shadow_rows(conn)
                assert created == {"created": 2, "already_resolved": 1}

                calls = []
                def fake_get_market(ticker):
                    calls.append(ticker)
                    if ticker == "KXBTC15M-SHADOW-A":
                        return {"market": {"status": "finalized", "result": "yes"}}
                    raise AssertionError("the already resolved official result should not be fetched")

                result = process_hold_to_settlement_shadow(
                    conn, fake_get_market, max_market_checks=1,
                    now=datetime(2026, 10, 24, 7, 20, tzinfo=timezone.utc),
                )
                assert result["newly_settled"] == 1
                assert result["pending"] == 0
                assert calls == ["KXBTC15M-SHADOW-A"]

                shadow = conn.execute(
                    """SELECT baseline_trade_id,state,outcome,pnl_cents,entry_cents,contracts
                       FROM strategy_shadow_trades WHERE strategy_key=? ORDER BY baseline_trade_id""",
                    (HOLD_TO_SETTLEMENT_KEY,),
                ).fetchall()
                assert shadow[0][0] == stopped_id
                assert shadow[0][1:4] == ("settled", "UP", 38.0)
                assert shadow[0][4:] == (60.0, 1)
                assert shadow[1][0] == held_id
                assert shadow[1][1:4] == ("settled", "DOWN", 28.0)

                baseline = conn.execute(
                    "SELECT pnl_cents,outcome FROM paper_trades WHERE id=?",
                    (stopped_id,),
                ).fetchone()
                assert baseline == (-10.0, "TREND_STOP_LOSS"), "shadow results must never overwrite actual paper results"

                report = build_strategy_comparison_report(conn)
                paired = report["paired_comparison"]
                assert paired["paired_resolved_trades"] == 2
                assert paired["current_engine"]["positive_net_rate_percent"] == 50.0
                assert paired["hold_to_settlement_candidate"]["positive_net_rate_percent"] == 100.0
                assert paired["net_pnl_difference_cents"] == 48.0
                assert paired["target_met_by_candidate"] is False, "two wins are not enough to claim the 90% target"
                assert report["automatic_strategy_changes_enabled"] is False
            finally:
                conn.close()


def test_shadow_rate_limits_retries_for_unsettled_market_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "shadow-retry.sqlite3")}):
            conn = _new_db(tmp)
            try:
                _insert_trade(
                    conn, ticker="KXBTC15M-SHADOW-PENDING", direction="DOWN", entry=50, fee=2,
                    pnl=-5, outcome="TREND_STOP_LOSS", settled_at="2026-10-24T07:05:00+00:00",
                    notes="TradingView-primary hybrid paper entry; simulated chart-stop exit",
                )
                calls = []
                def active_market(ticker):
                    calls.append(ticker)
                    return {"market": {"status": "active", "result": ""}}

                first = process_hold_to_settlement_shadow(
                    conn, active_market, max_market_checks=1,
                    now=datetime(2026, 10, 24, 7, 20, tzinfo=timezone.utc),
                )
                second = process_hold_to_settlement_shadow(
                    conn, active_market, max_market_checks=1,
                    retry_seconds=60,
                    now=datetime(2026, 10, 24, 7, 20, 30, tzinfo=timezone.utc),
                )
                assert first["created"] == 1
                assert first["checked"] == 1 and first["pending"] == 1
                assert second["created"] == 0 and second["checked"] == 0
                assert calls == ["KXBTC15M-SHADOW-PENDING"]
                assert conn.execute("SELECT COUNT(*) FROM strategy_shadow_trades").fetchone()[0] == 1
            finally:
                conn.close()


if __name__ == "__main__":
    import unittest
    unittest.main()
