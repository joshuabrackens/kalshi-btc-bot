import json
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import market_analysis_provider_service as provider


def test_invalid_provider_output_logs_reason_without_model_text(caplog, tmp_path):
    secret_like = "private-model-output-test-only"
    payload = {"output": [{"type": "message", "content": [{"type": "output_text", "text": secret_like}]}]}
    response = Mock(status_code=200, content=json.dumps(payload).encode(), is_redirect=False)
    response.json.return_value = payload
    with patch.dict("os.environ", {
        "MARKET_ANALYSIS_PROVIDER_API_KEY": "shared-test-token",
        "OPENAI_API_KEY": "fake-key-for-test",
        "OPENAI_MODEL": "gpt-test",
        "MARKET_MODEL_VALIDATION_REPORT_PATH": str(tmp_path / "empty.json"),
        "DATABASE_URL": "",
    }, clear=False), patch.object(provider.requests, "post", return_value=response) as post:
        with TestClient(provider.app) as client:
            result = client.post("/analyze", json={
                "market": {"ticker": "KXTEST-1", "title": "Test market", "category": "sports", "status": "active"},
                "orderbook_summary": {}, "required_evidence_types": [],
            }, headers={"Authorization": "Bearer shared-test-token"})
    assert result.status_code == 502
    assert post.call_count == 1
    assert "reason=invalid_structured_output" in caplog.text
    assert "KXTEST-1" in caplog.text
    assert secret_like not in caplog.text
    assert "fake-key-for-test" not in caplog.text
