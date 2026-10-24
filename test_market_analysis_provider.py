import json
import os
from unittest.mock import Mock, patch

import pytest

from market_analysis_provider import MarketAnalysisProviderError, analyze_market, provider_configured


class _Response:
    def __init__(self, status=200, payload=None, content=None, redirect=False):
        self.status_code = status
        self._payload = payload
        self.content = content if content is not None else json.dumps(payload or {}).encode()
        self.is_redirect = redirect

    def json(self):
        return self._payload


def test_missing_provider_disables_external_model():
    with patch.dict(os.environ, {"MARKET_ANALYSIS_PROVIDER_URL": ""}, clear=False):
        assert provider_configured() is False
        with pytest.raises(MarketAnalysisProviderError, match="not_configured"):
            analyze_market({"ticker": "M"}, {}, [])


def test_provider_rejects_insecure_nonlocal_url_and_url_credentials():
    for url in ("http://example.com/analyze", "https://user:pass@example.com/analyze", "https://example.com/analyze?token=x"):
        with patch.dict(os.environ, {"MARKET_ANALYSIS_PROVIDER_URL": url}, clear=False):
            with pytest.raises(MarketAnalysisProviderError):
                analyze_market({"ticker": "M"}, {}, [])


def test_provider_sends_bounded_contract_and_checks_ticker_match():
    payload = {"market_ticker": "M-1", "model_probability_yes": 0.7}
    response = _Response(payload=payload)
    with patch.dict(os.environ, {"MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze", "MARKET_ANALYSIS_PROVIDER_API_KEY": "secret"}, clear=False), \
         patch("market_analysis_provider.requests.post", return_value=response) as post:
        result = analyze_market({"ticker": "M-1"}, {"yes_ask_cents": 50}, ["recent_news"])
    assert result["market_ticker"] == "M-1"
    args, kwargs = post.call_args
    assert args[0] == "https://model.example/analyze"
    assert kwargs["headers"]["Authorization"] == "Bearer secret"
    assert kwargs["allow_redirects"] is False
    assert kwargs["json"]["contract"] == "kalshi-market-model-v1"
    assert kwargs["json"]["required_evidence_types"] == ["recent_news"]


def test_provider_rejects_redirect_invalid_json_oversize_and_ticker_mismatch():
    scenarios = [
        _Response(status=302, payload={}, redirect=True),
        _Response(status=200, payload=None, content=b"not-json"),
        _Response(status=200, payload={"market_ticker": "M"}, content=b"x" * 262145),
        _Response(status=200, payload={"market_ticker": "OTHER"}),
    ]
    with patch.dict(os.environ, {"MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze"}, clear=False):
        for response in scenarios:
            with patch("market_analysis_provider.requests.post", return_value=response):
                with pytest.raises(MarketAnalysisProviderError):
                    analyze_market({"ticker": "M"}, {}, [])
