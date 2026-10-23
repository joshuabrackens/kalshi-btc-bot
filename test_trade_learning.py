"""Persistent per-trade learning records and conservative cohort summaries."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import db_store
from trade_learning import build_learning_report, sync_settled_trade_learning


def _new_db(tmp: str):
    conn = db_store.connect_database()
    db_store.ensure_schema(conn)
    return conn


def _insert_linked_trade(conn, *, trade_id_ticker="KXBTC15M-LEARN", direction="UP", outcome="UP", pnl=28.0,
                         entry=70.0, bias="UP", evidence=78, volume=True, momentum=1, chase=False, settled=True):
    stamp = "2026-10-23T00:00:00+00:00"
    features = {
        "chart_trend_at_decision": {"candidate_bias": bias, "evidence_score": evidence, "age_seconds": 2.0,
                                     "timeframes": {"1m": "bullish", "5m": "bullish", "15m": "mixed"}},
        "strategies": {
            "momentum": {"direction": momentum, "rsi_1m": 58.5},
            "volume_confirmation": {"confirmed": volume},
            "market_structure": {"direction": 1, "label": "higher highs and higher lows"},
            "box_theory": {"state": "bullish_breakout", "direction": 1},
            "volatility": {"chase_warning": chase, "three_candle_abs_move_pct": 0.02},
        },
        "reasons": ["Trend and indicator evaluated together."],
        "warnings": ["Evidence score is not a calibrated probability."],
        "external_signal": {"age_seconds": 12.0},
        "decision_status": "paper_opened",
    }
    sig = conn.execute(
        """INSERT INTO signal_log
           (created_at,market_ticker,direction,score,btc_price,strike_price,seconds_remaining,max_entry_cents,
            payload_json,decision_status,proposed_direction,entry_ask_cents,spread_cents,trend_bias,
            trend_evidence_score,trend_snapshot_age_seconds)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (stamp, trade_id_ticker, f"BUY_{direction}", 72, 81800.0, 81750.0, 600, 80.0,
         json.dumps(features), "paper_opened", direction, entry, 2.0, bias, evidence, 2.0),
    )
    signal_id = sig.lastrowid
    conn.execute(
        """INSERT INTO paper_trades
           (created_at,settled_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,
            outcome,pnl_cents,entry_signal_source,entry_signal_log_id,entry_signal_score,entry_btc_price,timeframe)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (stamp, stamp if settled else None, trade_id_ticker, direction, 1, entry, 2.0, outcome,
         pnl if settled else None, "hybrid", signal_id, 72, 81800.0, "1m"),
    )
    conn.commit()
    return signal_id


def test_settled_trade_learning_captures_features_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "learning.sqlite3")}):
            conn = _new_db(tmp)
            try:
                _insert_linked_trade(conn, direction="UP", outcome="UP", pnl=28.0, bias="UP", volume=True, momentum=1)
                first = sync_settled_trade_learning(conn)
                second = sync_settled_trade_learning(conn)
                assert first == 1
                assert second == 0
                row = conn.execute(
                    "SELECT trade_outcome,pnl_result,pnl_cents,trend_alignment,entry_price_bucket,feature_snapshot_json,review_json FROM trade_learning"
                ).fetchone()
                assert row[0] == "UP"
                assert row[1] == "profit"
                assert row[2] == 28.0
                assert row[3] == "aligned"
                assert row[4] == "65–79¢"
                features = json.loads(row[5])
                review = json.loads(row[6])
                assert features["volume_confirmation"] == "yes"
                assert features["momentum_alignment"] == "aligned"
                assert review["result"] == "profit"
                assert review["directional_settlement_hit"] is True
                assert "never changes strategy rules" in review["interpretation_note"]
                count = conn.execute("SELECT COUNT(*) FROM trade_learning").fetchone()[0]
                assert count == 1
            finally:
                conn.close()


def test_learning_classifies_realized_pnl_separately_from_exit_reason():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "exit-reason.sqlite3")}):
            conn = _new_db(tmp)
            try:
                _insert_linked_trade(conn, trade_id_ticker="KXBTC15M-STOP", direction="DOWN",
                                     outcome="TREND_STOP_LOSS", pnl=12.8, bias="UP", volume=False, momentum=1)
                sync_settled_trade_learning(conn)
                row = conn.execute("SELECT trade_outcome,pnl_result,trend_alignment,momentum_alignment,review_json FROM trade_learning").fetchone()
                assert row[:4] == ("TREND_STOP_LOSS", "profit", "opposed", "opposed")
                review = json.loads(row[4])
                assert review["directional_settlement_hit"] is None
                report = build_learning_report(conn)
                assert report["summary"]["profitable"] == 1
                assert report["summary"]["unprofitable"] == 0
                assert report["automatic_strategy_changes_enabled"] is False
            finally:
                conn.close()


def test_unsettled_trade_is_not_learned_until_result_exists():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "unsettled.sqlite3")}):
            conn = _new_db(tmp)
            try:
                _insert_linked_trade(conn, trade_id_ticker="KXBTC15M-OPEN", settled=False)
                assert sync_settled_trade_learning(conn) == 0
                assert conn.execute("SELECT COUNT(*) FROM trade_learning").fetchone()[0] == 0
                conn.execute("UPDATE paper_trades SET settled_at=?, outcome='UP', pnl_cents=18 WHERE market_ticker=?",
                             ("2026-10-23T00:15:00+00:00", "KXBTC15M-OPEN"))
                conn.commit()
                assert sync_settled_trade_learning(conn) == 1
                report = build_learning_report(conn)
                assert report["summary"]["trades_reviewed"] == 1
                assert report["latest_review"]["pnl_result"] == "profit"
            finally:
                conn.close()


def test_learning_report_exposes_research_cohorts_but_never_auto_tunes_rules():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "cohorts.sqlite3")}):
            conn = _new_db(tmp)
            try:
                _insert_linked_trade(conn, trade_id_ticker="KXBTC15M-A", direction="UP", outcome="UP", pnl=28, bias="UP")
                _insert_linked_trade(conn, trade_id_ticker="KXBTC15M-B", direction="DOWN", outcome="TREND_STOP_LOSS", pnl=-35, bias="UP", volume=False, momentum=1, chase=True)
                sync_settled_trade_learning(conn)
                report = build_learning_report(conn)
                assert report["mode"] == "SIMULATION_ONLY"
                assert report["automatic_strategy_changes_enabled"] is False
                assert len(report["recent_reviews"]) == 2
                assert report["summary"]["trades_reviewed"] == 2
                assert report["summary"]["profitable"] == 1
                assert report["summary"]["unprofitable"] == 1
                assert report["cohorts"]["trend_alignment"]
                assert report["research_notes"]
                assert "never applied automatically" in report["policy"]
            finally:
                conn.close()


def test_legacy_settled_trade_uses_external_signal_link_when_signal_log_id_missing():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "legacy-link.sqlite3")}):
            conn = _new_db(tmp)
            try:
                stamp = "2026-10-23T00:00:00+00:00"
                payload = {
                    "chart_trend_at_decision": {"candidate_bias": "DOWN", "evidence_score": 74, "age_seconds": 3},
                    "strategies": {"momentum": {"direction": -1}, "volume_confirmation": {"confirmed": True}},
                    "reasons": ["Linked through prior external alert id."],
                    "external_signal": {"id": 901, "age_seconds": 5},
                }
                conn.execute(
                    """INSERT INTO signal_log
                       (created_at,market_ticker,direction,score,btc_price,strike_price,seconds_remaining,
                        max_entry_cents,payload_json,external_signal_id,decision_status,proposed_direction,
                        entry_ask_cents,trend_bias,trend_evidence_score)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (stamp, "KXBTC15M-LEGACY", "BUY_DOWN", 71, 81000, 81020, 400, 80,
                     json.dumps(payload), 901, "paper_opened", "DOWN", 72, "DOWN", 74),
                )
                conn.execute(
                    """INSERT INTO paper_trades
                       (created_at,settled_at,market_ticker,direction,contracts,entry_cents,fee_cents_per_contract,
                        outcome,pnl_cents,entry_signal_source,external_signal_id,entry_signal_score,entry_btc_price,timeframe)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (stamp, stamp, "KXBTC15M-LEGACY", "DOWN", 1, 72, 2, "DOWN", 26,
                     "hybrid", 901, 0, 81000, "1m"),
                )
                conn.commit()
                assert sync_settled_trade_learning(conn) == 1
                row = conn.execute("SELECT trend_alignment,feature_snapshot_json FROM trade_learning").fetchone()
                assert row[0] == "aligned"
                features = json.loads(row[1])
                assert features["entry_reasons"] == ["Linked through prior external alert id."]
                assert features["trend_evidence_score"] == 74
            finally:
                conn.close()
