"""Regression coverage for independent current-window, historical-signal, and position state."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import db_store
import main


def test_wait_window_does_not_erase_last_directional_or_open_position_reasoning():
    now = datetime.now(timezone.utc)
    when = now.isoformat()
    older = (now - timedelta(minutes=7)).isoformat()
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "dashboard-context.sqlite3"),
            "PAPER_WORKER_ENABLED": "false", "PAPER_WORKER_INTERVAL_SECONDS": "15",
            "PAPER_SIGNAL_SOURCE": "hybrid", "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=False):
            with main.db_connect() as conn:
                entry_payload = {
                    "reasons": ["Entry used a fresh DOWN alert.", "BTC trend monitor aligned with DOWN."],
                    "warnings": ["Evidence score is not a win probability."],
                    "chart_trend_at_decision": {
                        "candidate_bias": "DOWN", "evidence_score": 82,
                        "updated_at": older, "age_seconds": 1,
                        "timeframes": {"1m": "bearish", "5m": "bearish", "15m": "mixed"},
                    },
                    "fusion": {"approved": True, "reasons": ["trend aligned"]},
                    "disclaimer": "Saved entry rationale; historical context.",
                }
                entry_cur = conn.execute(
                    """INSERT INTO signal_log
                       (created_at,market_ticker,direction,score,btc_price,strike_price,seconds_remaining,
                        max_entry_cents,payload_json,external_signal_id,decision_status,proposed_direction,
                        entry_ask_cents,spread_cents,trend_bias,trend_evidence_score,trend_snapshot_age_seconds)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (older, "KXBTC15M-DOWN", "BUY_DOWN", 82, 81700, 81750, 500, 80,
                     json.dumps(entry_payload), 77, "paper_opened", "DOWN", 74, 2, "DOWN", 82, 1.0),
                )
                entry_signal_id = entry_cur.lastrowid
                # A newer WAIT entry record represents a later window/price wait. The old
                # directional history and currently open trade must remain inspectable.
                conn.execute(
                    """INSERT INTO signal_log
                       (created_at,market_ticker,direction,score,btc_price,strike_price,seconds_remaining,
                        max_entry_cents,payload_json,decision_status,proposed_direction,entry_ask_cents,
                        trend_bias,trend_evidence_score)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (when, "KXBTC15M-UP", "WAIT", 0, 81800, 81810, 600, 80,
                     json.dumps({"reasons": ["Ask above target; wait for better price."],
                                 "decision_status": "wait_for_better_price"}),
                     "wait_for_better_price", "UP", 87, "UP", 40),
                )
                conn.execute(
                    """INSERT INTO paper_trades
                       (created_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,
                        entry_btc_price,timeframe,entry_signal_source,external_signal_id,entry_signal_log_id,
                        entry_signal_score,notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (older, "KXBTC15M-DOWN", "DOWN", 1, 74, 2, 81700, "1m", "hybrid", 77,
                     entry_signal_id, 82, "test open position"),
                )
                conn.commit()
            data = main.dashboard_data()
            assert data["current_window_decision"]["status"] == "WAIT THIS WINDOW"
            last = data["latest_directional_signal"]
            assert last["direction"] == "UP"
            assert last["logged_direction"] == "WAIT"
            assert last["decision_status"] == "wait_for_better_price"
            position = data["active_position"]
            assert position["direction"] == "DOWN"
            assert position["entry_signal_context_available"] is True
            assert position["entry_signal"]["direction"] == "BUY_DOWN"
            assert position["entry_signal"]["reasons"] == entry_payload["reasons"]
            assert position["entry_signal"]["chart_trend_at_decision"]["candidate_bias"] == "DOWN"
            assert position["entry_cost_dollars"] == 0.76


def test_dashboard_has_separate_historical_signal_active_position_and_window_context():
    from html.parser import HTMLParser

    html = (Path(main.__file__).resolve().parent / "dashboard.html").read_text(encoding="utf-8")

    class IdParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = []
        def handle_starttag(self, tag, attrs):
            for key, value in attrs:
                if key == "id" and value:
                    self.ids.append(value)

    parser = IdParser()
    parser.feed(html)
    duplicates = sorted({item for item in parser.ids if parser.ids.count(item) > 1})
    assert not duplicates, f"Duplicate dashboard element ids: {duplicates}"
    for element_id in (
        "active-position-details", "active-position-reasons", "active-position-warnings",
        "current-window-status", "current-window-reason", "signal-age-badge", "decision-section-title",
        "signal-eyebrow", "active-position-summary", "strategy-checks-title",
    ):
        assert element_id in parser.ids, f"Missing dashboard element {element_id}"
    assert "latest_directional_signal" in html
    assert "renderActivePosition(data)" in html
    assert "HISTORICAL CONTEXT" in html
