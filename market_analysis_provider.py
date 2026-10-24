"""Optional normalized category-model API adapter. Defaults to unavailable/offline."""
from __future__ import annotations

import os
from typing import Any, Mapping
from urllib.parse import urlparse

import requests


class MarketAnalysisProviderError(RuntimeError):
    """Provider unavailable or returned unsafe/invalid data."""


def provider_configured() -> bool:
    value = os.getenv("MARKET_ANALYSIS_PROVIDER_URL", "").strip()
    if not value:
        return False
    parsed = urlparse(value)
    is_local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    return parsed.scheme == "https" or (is_local and parsed.scheme == "http")


def analyze_market(market: Mapping[str, Any], orderbook_summary: Mapping[str, Any], required_evidence_types: list[str]) -> dict[str, Any]:
    """Call an explicitly configured model/evidence service; never guess a probability.

    The external endpoint is configured only by the deployment operator, must use
    HTTPS (except loopback tests), and returns a normalized documented schema.
    """
    raw_url = os.getenv("MARKET_ANALYSIS_PROVIDER_URL", "").strip()
    parsed = urlparse(raw_url)
    is_local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if not raw_url or not (parsed.scheme == "https" or (is_local and parsed.scheme == "http")):
        raise MarketAnalysisProviderError("probability_and_evidence_provider_not_configured")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MarketAnalysisProviderError("invalid_probability_provider_url")
    api_key = os.getenv("MARKET_ANALYSIS_PROVIDER_API_KEY", "").strip()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key[:4096]}"
    timeout = 8.0
    try:
        timeout = max(2.0, min(float(os.getenv("MARKET_ANALYSIS_PROVIDER_TIMEOUT_SECONDS", "8")), 15.0))
    except (TypeError, ValueError, OverflowError):
        pass
    payload = {
        "market": dict(market),
        "orderbook_summary": dict(orderbook_summary),
        "required_evidence_types": list(required_evidence_types[:10]),
        "contract": "kalshi-market-model-v1",
    }
    try:
        response = requests.post(raw_url, json=payload, headers=headers, timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        raise MarketAnalysisProviderError(f"probability_provider_network_error:{type(exc).__name__}") from None
    if response.is_redirect:
        raise MarketAnalysisProviderError("probability_provider_redirect_refused")
    if response.status_code < 200 or response.status_code >= 300:
        raise MarketAnalysisProviderError(f"probability_provider_http_{response.status_code}")
    if len(response.content) > 262_144:
        raise MarketAnalysisProviderError("probability_provider_response_too_large")
    try:
        result = response.json()
    except ValueError:
        raise MarketAnalysisProviderError("probability_provider_invalid_json") from None
    if not isinstance(result, dict):
        raise MarketAnalysisProviderError("probability_provider_invalid_payload")
    supplied_ticker = result.get("market_ticker")
    expected_ticker = market.get("ticker") or market.get("market_ticker")
    if not isinstance(supplied_ticker, str) or supplied_ticker != expected_ticker:
        raise MarketAnalysisProviderError("probability_provider_ticker_mismatch")
    return result
