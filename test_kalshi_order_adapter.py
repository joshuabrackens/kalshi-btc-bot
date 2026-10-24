from __future__ import annotations

import json
import os
from unittest.mock import Mock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

import kalshi_order_adapter as adapter


def _credentials(environment="demo"):
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(encoding=serialization.Encoding.PEM,
                            format=serialization.PrivateFormat.PKCS8,
                            encryption_algorithm=serialization.NoEncryption()).decode()
    return {"environment": environment, "api_key_id": "test-key-id", "private_key_pem": pem}


def _response(payload):
    response = Mock(status_code=201, content=json.dumps(payload).encode(), is_redirect=False)
    response.json.return_value = payload
    return response


def test_demo_order_uses_v2_endpoint_fixed_point_prices_and_signed_headers():
    credentials = _credentials("demo")
    fake = _response({"order": {"order_id": "ord-1", "client_order_id": "client_order_0001",
                                  "fill_count": "0.00", "remaining_count": "1.00", "status": "resting"}})
    with patch.dict(os.environ, {"KALSHI_MAX_ORDER_CONTRACTS": "1", "KALSHI_MAX_ORDER_PRICE_CENTS": "90"}, clear=False), \
         patch.object(adapter.requests, "request", return_value=fake) as request:
        result = adapter.submit_limit_order(credentials, ticker="KXTEST-MARKET", outcome_side="NO", contracts=1,
                                            limit_price_cents=40, client_order_id="client_order_0001", demo_only=True)
    assert result["environment"] == "demo"
    assert result["outcome_side"] == "NO" and result["book_side"] == "ask"
    args, kwargs = request.call_args
    assert args[0] == "POST"
    assert args[1] == "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/events/orders"
    assert kwargs["json"]["ticker"] == "KXTEST-MARKET"
    assert kwargs["json"]["side"] == "ask"
    assert kwargs["json"]["price"] == "0.6000"
    assert kwargs["json"]["client_order_id"] == "client_order_0001"
    assert kwargs["headers"]["KALSHI-ACCESS-SIGNATURE"]
    assert kwargs["allow_redirects"] is False


def test_production_order_is_blocked_when_demo_only_is_default():
    with pytest.raises(adapter.KalshiOrderError, match="Demo-only"):
        adapter.submit_limit_order(_credentials("production"), ticker="KXTEST-MARKET", outcome_side="YES",
                                   contracts=1, limit_price_cents=50, client_order_id="client_order_0001")


def test_production_order_requires_three_independent_explicit_gates():
    with patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "true", "LIVE_TRADING_APPROVED": "false",
                                 "MARKET_LIVE_EXECUTION_ENABLED": "true"}, clear=False):
        with pytest.raises(adapter.KalshiOrderError, match="locked"):
            adapter.submit_limit_order(_credentials("production"), ticker="KXTEST-MARKET", outcome_side="YES",
                                       contracts=1, limit_price_cents=50, client_order_id="client_order_0001", demo_only=False)


def test_timeout_on_post_is_ambiguous_and_must_not_be_retried_automatically():
    import requests
    with patch.object(adapter.requests, "request", side_effect=requests.Timeout("uncertain")) as request:
        with pytest.raises(adapter.OrderSubmissionUnknown):
            adapter.submit_limit_order(_credentials("demo"), ticker="KXTEST-MARKET", outcome_side="YES",
                                       contracts=1, limit_price_cents=50, client_order_id="client_order_0001")
        assert request.call_count == 1


def test_order_adapter_rejects_price_contract_and_client_id_violations():
    credentials = _credentials("demo")
    with pytest.raises(ValueError):
        adapter.submit_limit_order(credentials, ticker="KXTEST-MARKET", outcome_side="YES", contracts=11,
                                   limit_price_cents=50, client_order_id="client_order_0001")
    with pytest.raises(ValueError):
        adapter.submit_limit_order(credentials, ticker="KXTEST-MARKET", outcome_side="YES", contracts=1,
                                   limit_price_cents=99, client_order_id="client_order_0001")
    with pytest.raises(ValueError):
        adapter.submit_limit_order(credentials, ticker="KXTEST-MARKET", outcome_side="YES", contracts=1,
                                   limit_price_cents=50, client_order_id="bad id")
