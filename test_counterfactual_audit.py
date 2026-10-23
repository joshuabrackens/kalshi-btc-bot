"""Counterfactual signal outcome checks do not mutate paper trades."""
from __future__ import annotations
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

# Isolate this unit test from external providers and live HTTP calls.
_provider_error = type("ProviderError", (Exception,), {})
for name, attrs in {
    "candle_feed": {"fetch_signal_candles": lambda: {}, "fetch_closed_candles": lambda *_a, **_k: {}, "CandleFeedError": _provider_error},
    "trend_stop": {"update_trend_stop": lambda *_a, **_k: {}},
    "market_discovery": {"discover": lambda: {"candidates": []}},
    "kalshi_api": {"get_market": lambda *_a, **_k: {}, "get_orderbook": lambda *_a, **_k: {}, "KalshiAPIError": _provider_error},
}.items():
    if name not in sys.modules:
        module = types.ModuleType(name)
        for key, value in attrs.items(): setattr(module, key, value)
        sys.modules[name] = module

import db_store
import paper_worker


def test_rejected_signal_gets_hypothetical_settlement_not_a_trade():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "cf.sqlite3"), "PAPER_FEE_CENTS_PER_CONTRACT": "2"}):
            conn = db_store.connect_database()
            try:
                db_store.ensure_schema(conn)
                old_created = "2026-10-23T03:00:00+00:00"
                conn.execute(
                    """INSERT INTO signal_log (created_at,market_ticker,direction,score,btc_price,strike_price,seconds_remaining,
                       max_entry_cents,payload_json,decision_status,proposed_direction,entry_ask_cents,trend_bias,trend_evidence_score)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (old_created, "KXBTC15M-TEST-CF", "WAIT", 0, 100000, 100000, 30, 80, "{}", "vetoed", "UP", 50, "DOWN", 82),
                )
                conn.commit()
                with patch.object(paper_worker, "get_market", return_value={"market": {"status": "settled", "result": "yes"}}):
                    checked = paper_worker._resolve_counterfactual_signal_outcomes(conn, max_checks=1)
                row = conn.execute("SELECT counterfactual_outcome,counterfactual_pnl_cents,counterfactual_settled_at FROM signal_log WHERE market_ticker=?", ("KXBTC15M-TEST-CF",)).fetchone()
                trade_count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
                audit_count = conn.execute("SELECT COUNT(*) FROM decision_audit WHERE event_type='counterfactual_settled'").fetchone()[0]
                assert checked == 1
                assert row[0] == "UP"
                assert row[1] == 48.0  # 100c payout - 50c ask - 2c entry fee
                assert row[2] is not None
                assert trade_count == 0
                assert audit_count == 1
            finally:
                conn.close()
