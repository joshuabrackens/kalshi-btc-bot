from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import db_store
from db_store import connect_database, ensure_schema
from market_decision_pipeline import MarketDecisionPipeline


def _iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


def _seed_prediction(db_path: Path, *, evaluated_at: str, ticker: str = "KXTEST-RESOLVE") -> None:
    with patch.dict("os.environ", {"DATABASE_URL": "", "BOT_DB_PATH": str(db_path)}, clear=False):
        db_store._INITIALIZED.clear()
        conn = connect_database()
        try:
            ensure_schema(conn)
            conn.execute(
                """INSERT INTO market_research_decisions
                   (evaluated_at,market_ticker,category,decision,selected_side,model_probability_yes,
                    expected_net_edge_cents,model_id,model_version,payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (evaluated_at, ticker, "sports", "blocked", None, 0.73, None,
                 "model-test", "version-1", "{}"),
            )
            conn.commit()
        finally:
            conn.close()


def _run_collector(db_path: Path, market: dict):
    with patch.dict("os.environ", {
        "DATABASE_URL": "", "BOT_DB_PATH": str(db_path), "MARKET_ANALYSIS_PROVIDER_URL": "",
        "MARKET_OUTCOME_MIN_AGE_MINUTES": "60", "MARKET_OUTCOME_RETRY_MINUTES": "60",
    }, clear=False):
        pipeline = MarketDecisionPipeline(
            lambda: {"status": "complete", "analysis_candidates": [{"ticker": "KXTEST-RESOLVE", "category": "sports"}]},
            lambda _ticker: {"market": market}, lambda _ticker: {},
        )
        return pipeline.run_once_for_tests()


def test_settled_market_adds_one_prediction_outcome_for_chronological_calibration(tmp_path):
    db = tmp_path / "outcomes.sqlite3"
    _seed_prediction(db, evaluated_at=_iso(timedelta(hours=-3)))
    result = _run_collector(db, {"ticker": "KXTEST-RESOLVE", "status": "settled", "result": "yes",
                                "settlement_ts": _iso(timedelta(hours=-1))})
    assert result["status"] == "provider_not_configured"
    with patch.dict("os.environ", {"DATABASE_URL": "", "BOT_DB_PATH": str(db)}, clear=False):
        conn = connect_database()
        try:
            row = conn.execute("SELECT model_probability_yes,actual_outcome_yes,outcome_source FROM market_research_prediction_outcomes").fetchone()
            assert row is not None
            assert float(row[0]) == 0.73
            assert int(row[1]) == 1
            assert row[2] == "kalshi_api:market_resolution_timestamp"
            assert conn.execute("SELECT COUNT(*) FROM market_research_prediction_outcomes").fetchone()[0] == 1
        finally:
            conn.close()


def test_repeated_scans_do_not_reset_calibration_prediction_age(tmp_path):
    db = tmp_path / "repeated-predictions.sqlite3"
    first_prediction_at = _iso(timedelta(hours=-3))
    newer_prediction_at = _iso(timedelta(minutes=-10))
    _seed_prediction(db, evaluated_at=first_prediction_at)

    # A repeated scan for the same market/model version must not replace the
    # older forecast's age clock indefinitely.
    with patch.dict("os.environ", {"DATABASE_URL": "", "BOT_DB_PATH": str(db)}, clear=False):
        conn = connect_database()
        try:
            conn.execute(
                """INSERT INTO market_research_decisions
                   (evaluated_at,market_ticker,category,decision,selected_side,model_probability_yes,
                    expected_net_edge_cents,model_id,model_version,payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (newer_prediction_at, "KXTEST-RESOLVE", "sports", "blocked", None, 0.91, None,
                 "model-test", "version-1", "{}"),
            )
            conn.commit()
        finally:
            conn.close()

    result = _run_collector(db, {
        "ticker": "KXTEST-RESOLVE", "status": "settled", "result": "yes",
        "settlement_ts": _iso(timedelta(minutes=-5)),
    })
    assert result["status"] == "provider_not_configured"
    with patch.dict("os.environ", {"DATABASE_URL": "", "BOT_DB_PATH": str(db)}, clear=False):
        conn = connect_database()
        try:
            row = conn.execute(
                "SELECT evaluated_at,model_probability_yes FROM market_research_prediction_outcomes"
            ).fetchone()
            assert row is not None
            assert row[0] == first_prediction_at
            assert float(row[1]) == 0.73
            assert conn.execute("SELECT check_status FROM market_research_prediction_checks").fetchone()[0] == "resolved"
        finally:
            conn.close()


def test_collector_refuses_resolution_timestamp_before_prediction(tmp_path):
    db = tmp_path / "lookahead.sqlite3"
    _seed_prediction(db, evaluated_at=_iso(timedelta(hours=-2)))
    result = _run_collector(db, {"ticker": "KXTEST-RESOLVE", "status": "settled", "result": "yes",
                                "settlement_ts": _iso(timedelta(hours=-3))})
    with patch.dict("os.environ", {"DATABASE_URL": "", "BOT_DB_PATH": str(db)}, clear=False):
        conn = connect_database()
        try:
            assert conn.execute("SELECT COUNT(*) FROM market_research_prediction_outcomes").fetchone()[0] == 0
            status = conn.execute("SELECT check_status FROM market_research_prediction_checks").fetchone()[0]
            assert status == "resolved_before_prediction"
        finally:
            conn.close()


def test_unresolved_market_is_rate_limited_for_rechecks(tmp_path):
    db = tmp_path / "pending.sqlite3"
    _seed_prediction(db, evaluated_at=_iso(timedelta(hours=-3)))
    market = {"ticker": "KXTEST-RESOLVE", "status": "active", "result": ""}
    _run_collector(db, market)
    market_calls = []
    with patch.dict("os.environ", {"DATABASE_URL": "", "BOT_DB_PATH": str(db), "MARKET_ANALYSIS_PROVIDER_URL": ""}, clear=False):
        pipeline = MarketDecisionPipeline(
            lambda: {"status": "complete", "analysis_candidates": [{"ticker": "KXTEST-RESOLVE", "category": "sports"}]},
            lambda _ticker: (market_calls.append(_ticker) or {"market": market}), lambda _ticker: {},
        )
        pipeline.run_once_for_tests()
    assert market_calls == []
