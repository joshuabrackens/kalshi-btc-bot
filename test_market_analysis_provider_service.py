from __future__ import annotations

import json

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import market_analysis_provider_service as provider


def _response(ticker="KXSPORTS-TEST", url="https://espn.com/story"):
    now = datetime.now(timezone.utc).isoformat()
    text = json.dumps({"market_ticker": ticker, "model_probability_yes": 0.67,
                       "rationale": "Cited recent evidence is mixed.",
                       "evidence": [{"type": "recent_news", "url": url, "published_at": now, "relevance_score": 0.9}]})
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text,
            "annotations": [{"type": "url_citation", "url": url, "title": "ESPN report"}]}]}]}


def _market_payload():
    return {"market": {"ticker": "KXSPORTS-TEST", "title": "Will Team A win?", "category": "sports",
                       "status": "active", "yes_ask_dollars": "0.5000", "no_ask_dollars": "0.5000"},
            "orderbook_summary": {"yes_ask_cents": 50.0, "no_ask_cents": 50.0},
            "required_evidence_types": ["injury_report", "lineup", "recent_news", "recent_stats"]}


@pytest.fixture(autouse=True)
def _reset_quota_circuit_breaker():
    provider._clear_quota_cooldown()
    yield
    provider._clear_quota_cooldown()


def test_provider_auth_and_missing_api_key_fail_closed():
    with patch.dict("os.environ", {"MARKET_ANALYSIS_PROVIDER_API_KEY": "shared-secret",
                                    "OPENAI_API_KEY": "", "OPENAI_MODEL": "gpt-5.1"}, clear=False):
        with TestClient(provider.app) as client:
            assert client.post("/analyze", json=_market_payload()).status_code == 401
            response = client.post("/analyze", json=_market_payload(), headers={"Authorization": "Bearer shared-secret"})
            assert response.status_code == 503


def test_provider_returns_only_cited_sources_and_never_self_certifies_calibration(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    fake = Mock(status_code=200, content=json.dumps(_response()).encode("utf-8"), is_redirect=False)
    fake.json.return_value = _response()
    with patch.dict("os.environ", {"MARKET_ANALYSIS_PROVIDER_API_KEY": "shared-secret", "OPENAI_API_KEY": "openai-test",
                                    "OPENAI_MODEL": "gpt-5.1", "MARKET_MODEL_VALIDATION_REPORT_PATH": str(tmp_path / "empty.json")}, clear=False), \
         patch.object(provider.requests, "post", return_value=fake) as post:
        with TestClient(provider.app) as client:
            response = client.post("/analyze", json=_market_payload(), headers={"Authorization": "Bearer shared-secret"})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["market_ticker"] == "KXSPORTS-TEST"
        assert len(data["evidence"]) == 1
        assert data["evidence"][0]["source"] == "ESPN report"
        assert data["evidence"][0]["reliability_score"] >= 0.7
        assert data["validation"]["out_of_sample"] is False
        assert data["validation"]["sample_count"] == 0
        assert data["trade_authorized"] is False
        assert "openai-test" not in json.dumps(data)
        sent = post.call_args.kwargs["json"]
        assert sent["tools"][0]["type"] == "web_search"
        assert sent["store"] is False


def test_provider_drops_evidence_url_not_present_in_actual_citations(tmp_path):
    response_data = _response(url="https://fake-unverified.example/story")
    response_data["output"][0]["content"][0]["annotations"] = [
        {"type": "url_citation", "url": "https://different.example/", "title": "Other source"}
    ]
    fake = Mock(status_code=200, content=json.dumps(response_data).encode("utf-8"), is_redirect=False)
    fake.json.return_value = response_data
    with patch.dict("os.environ", {"MARKET_ANALYSIS_PROVIDER_API_KEY": "shared-secret", "OPENAI_API_KEY": "openai-test",
                                    "OPENAI_MODEL": "gpt-5.1", "MARKET_MODEL_VALIDATION_REPORT_PATH": str(tmp_path / "empty.json")}, clear=False), \
         patch.object(provider.requests, "post", return_value=fake):
        with TestClient(provider.app) as client:
            response = client.post("/analyze", json=_market_payload(), headers={"Authorization": "Bearer shared-secret"})
        assert response.status_code == 200
        assert response.json()["evidence"] == []
        assert response.json()["validation"]["out_of_sample"] is False


def test_provider_rejects_mismatched_ticker_and_large_payload():
    fake_data = _response(ticker="ANOTHER-MARKET")
    fake = Mock(status_code=200, content=json.dumps(fake_data).encode("utf-8"), is_redirect=False)
    fake.json.return_value = fake_data
    with patch.dict("os.environ", {"MARKET_ANALYSIS_PROVIDER_API_KEY": "shared-secret", "OPENAI_API_KEY": "openai-test",
                                    "OPENAI_MODEL": "gpt-5.1"}, clear=False), patch.object(provider.requests, "post", return_value=fake):
        with TestClient(provider.app) as client:
            wrong = client.post("/analyze", json=_market_payload(), headers={"Authorization": "Bearer shared-secret"})
            large = client.post("/analyze", content=b"{" + b" " * 70000 + b"}", headers={"Authorization": "Bearer shared-secret", "Content-Type": "application/json"})
        assert wrong.status_code == 502
        assert large.status_code == 413


def test_provider_quota_429_activates_circuit_breaker_and_avoids_repeat_paid_calls(caplog, tmp_path):
    error_body = {
        "error": {
            "message": "You exceeded your current quota, please check your plan and billing details.",
            "type": "insufficient_quota",
            "code": "insufficient_quota",
        }
    }
    fake = Mock(status_code=429, content=json.dumps(error_body).encode("utf-8"), is_redirect=False)
    fake.json.return_value = error_body
    with (
        patch.dict(
            "os.environ",
            {
                "MARKET_ANALYSIS_PROVIDER_API_KEY": "shared-secret",
                "OPENAI_API_KEY": "openai-test-secret",
                "OPENAI_MODEL": "gpt-5.1",
                "OPENAI_QUOTA_COOLDOWN_SECONDS": "3600",
                "MARKET_MODEL_VALIDATION_REPORT_PATH": str(tmp_path / "empty.json"),
            },
            clear=False,
        ),
        patch.object(provider.requests, "post", return_value=fake) as post,
    ):
        with TestClient(provider.app) as client:
            first = client.post(
                "/analyze", json=_market_payload(),
                headers={"Authorization": "Bearer shared-secret"},
            )
            second = client.post(
                "/analyze", json=_market_payload(),
                headers={"Authorization": "Bearer shared-secret"},
            )
    assert first.status_code == 503
    assert first.headers["Retry-After"] == "3600"
    assert second.status_code == 503
    assert int(second.headers["Retry-After"]) > 0
    assert post.call_count == 1
    assert "classification=quota_or_billing" in caplog.text
    assert "code=insufficient_quota" in caplog.text
    assert "circuit breaker activated for 3600 seconds" in caplog.text
    assert "openai-test-secret" not in caplog.text
    assert "current quota" not in caplog.text


def test_provider_classifies_rate_limit_429_from_safe_error_fields():
    fake = Mock(status_code=429, content=b"{}", is_redirect=False)
    fake.json.return_value = {
        "error": {"message": "Rate limit reached for requests.", "type": "requests", "code": "rate_limit_exceeded"}
    }
    assert provider._openai_error_diagnostics(fake) == ("rate_limit", "rate_limit_exceeded", "requests")


def test_provider_uses_realized_database_outcomes_for_calibration(tmp_path, monkeypatch):
    import db_store
    from datetime import timedelta
    from pathlib import Path

    db_path = tmp_path / "calibration.sqlite3"
    monkeypatch.setenv("DATABASE_URL", "postgresql://test-shared-db")
    monkeypatch.setenv("BOT_DB_PATH", str(db_path))
    monkeypatch.setenv("MARKET_MODEL_MIN_VALIDATION_SAMPLES", "100")
    monkeypatch.setenv("MARKET_MODEL_MAX_BRIER_SCORE", "0.22")
    monkeypatch.setattr(db_store, "_database_url", lambda: "")
    db_store._INITIALIZED.clear()
    conn = db_store.connect_database()
    try:
        db_store.ensure_schema(conn)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(100):
            outcome = i % 2
            p = 0.9 if outcome else 0.1
            decision = base + timedelta(minutes=i)
            settled = decision + timedelta(hours=2)
            conn.execute(
                """INSERT INTO market_research_prediction_outcomes
                   (market_ticker,category,model_id,model_version,evaluated_at,settled_at,
                    model_probability_yes,actual_outcome_yes,outcome_source,recorded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (f"KXSPORTS-{i}", "sports", "openai-responses-websearch", "gpt-5.1:evidence-probability-v1",
                 decision.isoformat(), settled.isoformat(), p, outcome, "kalshi_api:test_fixture", settled.isoformat()),
            )
        conn.commit()
    finally:
        conn.close()
    provider._VALIDATION_CACHE.clear()
    result = provider._validation_for("sports", "openai-responses-websearch", "gpt-5.1:evidence-probability-v1")
    assert result["out_of_sample"] is True
    assert result["sample_count"] == 100
    assert result["brier_score"] == 0.01
    assert result["yes_outcomes"] == 50 and result["no_outcomes"] == 50
    assert len(result["dataset_sha256"]) == 64


def test_provider_calibration_database_failure_fails_closed(monkeypatch):
    import db_store
    monkeypatch.setenv("DATABASE_URL", "postgresql://unavailable")
    monkeypatch.setattr(db_store, "connect_database", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    provider._VALIDATION_CACHE.clear()
    result = provider._validation_for("sports", "model", "version")
    assert result["out_of_sample"] is False
    assert result["sample_count"] == 0
    assert result["evaluation_type"] == "database_unavailable"
