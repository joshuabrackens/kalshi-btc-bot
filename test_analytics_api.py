"""Offline integration test for the read-only Phase 3 analytics endpoint."""
from __future__ import annotations
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

_error = type("ProviderError", (Exception,), {})
for name, attrs in {
    "market_discovery": {"discover": lambda: {"candidates": []}},
    "kalshi_api": {"get_market": lambda *_a, **_k: {}, "get_orderbook": lambda *_a, **_k: {}, "KalshiAPIError": _error},
    "candle_feed": {"fetch_signal_candles": lambda: {}, "fetch_closed_candles": lambda *_a, **_k: {}, "CandleFeedError": _error},
}.items():
    if name not in sys.modules and not Path(__file__).with_name(f"{name}.py").is_file():
        module = types.ModuleType(name)
        for key, value in attrs.items(): setattr(module, key, value)
        sys.modules[name] = module

import db_store
from main import performance_analytics


def test_analytics_endpoint_returns_actual_and_counterfactual_metrics_separately():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "analytics.sqlite3")}):
            conn = db_store.connect_database()
            try:
                db_store.ensure_schema(conn)
                conn.execute(
                    """INSERT INTO signal_log
                       (created_at,market_ticker,direction,score,btc_price,strike_price,seconds_remaining,max_entry_cents,payload_json,
                        decision_status,proposed_direction,entry_ask_cents,trend_bias,trend_evidence_score,
                        counterfactual_outcome,counterfactual_pnl_cents,counterfactual_settled_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("2026-10-23T01:00:00+00:00", "KXBTC15M-CF", "WAIT", 0, 100000, 100000, 30, 80,
                     json.dumps({"reasons": ["test veto"], "chart_trend_at_decision": {"candidate_bias": "DOWN", "evidence_score": 80}}),
                     "vetoed", "UP", 50.0, "DOWN", 80, "UP", 48.0, "2026-10-23T01:15:00+00:00"),
                )
                cf_signal_id = conn.execute("SELECT id FROM signal_log WHERE market_ticker=?", ("KXBTC15M-CF",)).fetchone()[0]
                conn.execute(
                    """INSERT INTO signal_log
                       (created_at,market_ticker,direction,score,btc_price,strike_price,seconds_remaining,max_entry_cents,payload_json,
                        decision_status,proposed_direction,entry_ask_cents,trend_bias,trend_evidence_score)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("2026-10-23T00:00:00+00:00", "KXBTC15M-ACTUAL", "BUY_DOWN", 72, 100000, 99990, 600, 80,
                     json.dumps({"chart_trend_at_decision": {"candidate_bias": "DOWN", "evidence_score": 72}}),
                     "paper_opened", "DOWN", 70.0, "DOWN", 72),
                )
                actual_signal_id = conn.execute("SELECT id FROM signal_log WHERE market_ticker=?", ("KXBTC15M-ACTUAL",)).fetchone()[0]
                conn.execute(
                    """INSERT INTO paper_trades
                       (created_at,settled_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,
                        outcome,pnl_cents,entry_signal_source,entry_signal_log_id,trend_stop_shadow_triggered_at,trend_stop_shadow_pnl_cents)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("2026-10-23T00:00:00+00:00", "2026-10-23T00:15:00+00:00", "KXBTC15M-ACTUAL", "DOWN", 1,
                     70.0, 2.0, "DOWN", 28.0, "hybrid", actual_signal_id, "2026-10-23T00:05:00+00:00", 20.0),
                )
                db_store.record_decision_audit(conn, event_type="signal_evaluation", signal_log_id=cf_signal_id,
                                               market_ticker="KXBTC15M-CF", direction="UP", decision="vetoed",
                                               reason="strong opposing trend", entry_ask_cents=50, trend_bias="DOWN",
                                               trend_evidence_score=80, payload={"test": True})
                conn.commit()
            finally:
                conn.close()
            result = performance_analytics()
            assert result["mode"] == "SIMULATION_ONLY"
            assert result["summary"]["settled_trades"] == 1
            assert result["summary"]["net_pnl_cents"] == 28
            assert result["rejected_signal_counterfactuals"]["summary"]["net_pnl_cents"] == 48
            assert result["rejected_signal_counterfactuals"]["summary"]["settled_trades"] == 1
            assert result["trend_stop_shadow_comparison"]["triggered_and_settled_count"] == 1
            assert result["recent_decisions"][0]["event_type"] == "signal_evaluation"
