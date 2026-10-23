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
                conn.execute(
                    """INSERT INTO adaptive_policy_state
                       (policy_key,base_target_cents,effective_target_cents,last_evaluated_trade_count,
                        last_evaluated_latest_trade_id,adjustment_count,last_sample_count,last_sample_net_pnl_cents,
                        last_reason,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    ("btc_hybrid_entry_target", 80, 78, 20, 150, 2, 20, -52,
                     "Automatically tightened the preferred entry target from 79¢ to 78¢.", when),
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
            assert data["risk"]["configured_hard_max_entry_cents"] == 92.0
            assert data["risk"]["configured_entry_target_max_cents"] == 80.0
            assert data["risk"]["entry_target_max_cents"] == 78.0
            assert data["risk"]["adaptive_entry_target_adjustment_count"] == 2
            # The latest WAIT record stores zero as a decision placeholder, but the
            # actual chart evidence remains available separately for readable display.
            assert data["latest_signal"]["trend_evidence_score"] == 40


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
        "next-signal-countdown", "next-signal-reasons", "hard-entry-cap", "learning-reviewed", "learning-latest-observations",
        "learning-cohorts", "learning-recent-reviews", "adaptive-entry-target", "adaptive-entry-note",
        "trend-monitor-calendar", "trend-monitor-regime",
        "learning-effective-target", "learning-adjustment-count", "learning-policy-sample",
    ):
        assert element_id in parser.ids, f"Missing dashboard element {element_id}"
    assert "latest_directional_signal" in html
    assert "renderActivePosition(data)" in html
    assert "HISTORICAL CONTEXT" in html
    assert "Waiting For Next Available Signal" in html
    assert "renderLearning(report)" in html
    assert "policy_change_history" in html, "Dashboard should make saved adaptive-target changes reviewable"
    assert "trend-monitor-calendar" in html and "trend-monitor-regime" in html
    assert "getJSON('/api/learning')" in html
    assert "Automatic tightenings" in html and "preferred entry target may tighten" in html
    assert "Preferred entry target (soft)" in html
    assert "Current ask" in html and "hard cap" in html
    assert "chartScoreCandidates" in html, "WAIT placeholder score should fall back to stored chart evidence"
    assert "const alreadyEvaluated=" in html, "Current-window waiting panel must define its evaluated-state variable"
    assert "worker_signal_source" in html, "Dashboard should expose Web Service vs worker signal-source mismatches"
    assert "waiting_for_orderbook" in html, "Dashboard should show the worker note for a pending order-book check"
    assert "reasonScoreMatch" in html, "WAIT placeholder score should fall back to the evidence score in the actual reason text"
    # KPI/status/guardrail row is the first content after the header; the intro
    # follows it, then the active-position panel remains near the top.
    active_pos = html.index('id="active-position-section"')
    intro_end = html.index('</section>', html.index('<section class="intro">'))
    engine_overview = html.index('aria-label="Engine overview"')
    latest_signal = html.index('aria-label="Latest directional signal context"')
    assert engine_overview < intro_end < active_pos < latest_signal
    overview = html[engine_overview:html.index('</section>', engine_overview)]
    assert overview.index('Realized P&amp;L') < overview.index('Engine connection') < overview.index('Risk limits')


def test_dashboard_connection_badge_distinguishes_api_failure_from_panel_render_error():
    html = (Path(main.__file__).resolve().parent / "dashboard.html").read_text(encoding="utf-8")
    assert "Dashboard API request failed:" in html
    assert "API connected · display warning" in html
    assert "safeRender('active position'" in html
    assert "safeRender('analytics'" in html

def test_normalized_wait_evaluation_retains_external_alert_link():
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps({"external_signal": {"id": 391, "direction": "UP"}, "reasons": ["Waiting on quote."], "fusion": {"approved": False}})
    row = (88, now, "KXBTC15M-TEST", "WAIT", 0, 81800.0, 81810.0, 500, 92.0,
           "skipped", "UP", 87.0, 3.0, "UP", 52, 2.0, payload)
    normalized = main._dashboard_signal_from_row(row)
    assert normalized["external_signal_id"] == 391
    assert normalized["direction"] == "UP"
    assert normalized["decision_status"] == "skipped"

