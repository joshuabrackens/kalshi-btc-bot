"""Authenticated Kalshi Order API V2 adapter, guarded by explicit execution policy.

No automatic retries are performed for order-submission POSTs: a timeout can mean
Kalshi accepted the order, so callers must reconcile by client_order_id instead of
blindly resubmitting. This module is covered by mocked API tests only until a Demo
account and end-to-end lifecycle checks are available.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Mapping
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import serialization

from kalshi_credentials import BASE_URLS, CredentialVaultError, _sign, normalize_environment, validate_private_key

ORDER_PATH = "/portfolio/events/orders"
MAX_RESPONSE_BYTES = 262_144


class KalshiOrderError(RuntimeError):
    """Order API rejected or could not complete a request."""


class OrderSubmissionUnknown(KalshiOrderError):
    """POST may have reached exchange; reconcile by client_order_id before any retry."""


def _headers(credentials: Mapping[str, Any], method: str, endpoint: str) -> tuple[str, dict[str, str]]:
    environment = normalize_environment(credentials.get("environment"))
    key_id, pem, _ = validate_private_key(credentials.get("api_key_id"), credentials.get("private_key_pem"))
    try:
        private_key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise CredentialVaultError("Saved private key is invalid.") from exc
    base = BASE_URLS[environment].rstrip("/")
    base_path = urlparse(base).path.rstrip("/")
    clean_endpoint = "/" + endpoint.lstrip("/").split("?", 1)[0]
    timestamp = str(int(time.time() * 1000))
    signature = _sign(private_key, timestamp, method, base_path + clean_endpoint)
    return environment, {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _request(credentials: Mapping[str, Any], method: str, endpoint: str, *, payload: Mapping[str, Any] | None = None, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    environment, headers = _headers(credentials, method, endpoint)
    base = BASE_URLS[environment].rstrip("/")
    url = base + "/" + endpoint.lstrip("/").split("?", 1)[0]
    timeout = max(2.0, min(float(os.getenv("KALSHI_ORDER_REQUEST_TIMEOUT_SECONDS", "8")), 15.0))
    try:
        response = requests.request(method, url, headers=headers, json=dict(payload) if payload is not None else None,
                                    params=dict(params or {}), timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        if method.upper() == "POST" and endpoint.lstrip("/").startswith("portfolio/events/orders"):
            raise OrderSubmissionUnknown("Order submission outcome is unknown. Do not retry until the order is reconciled by client_order_id.") from None
        raise KalshiOrderError(f"Kalshi order API network error ({type(exc).__name__}).") from None
    if response.is_redirect:
        raise KalshiOrderError("Kalshi order API redirect refused.")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise KalshiOrderError("Kalshi order API response exceeded the size limit.")
    if not 200 <= response.status_code < 300:
        # Do not expose exchange payloads because they may contain account data.
        raise KalshiOrderError(f"Kalshi order API returned HTTP {response.status_code}.")
    if response.status_code == 204 or not response.content:
        return {"ok": True, "http_status": response.status_code}
    try:
        data = response.json()
    except ValueError:
        raise KalshiOrderError("Kalshi order API returned invalid JSON.") from None
    if not isinstance(data, dict):
        raise KalshiOrderError("Kalshi order API returned an unexpected response shape.")
    return data


def submit_limit_order(
    credentials: Mapping[str, Any], *, ticker: str, outcome_side: str, contracts: int,
    limit_price_cents: float, client_order_id: str, demo_only: bool = True,
) -> dict[str, Any]:
    """Submit one capped V2 limit order; defaults to Demo-only operation.

    `outcome_side` is YES/NO for the selected outcome. The V2 endpoint quotes the
    YES contract book only: buying NO is represented as selling YES at 100-NO price.
    This function has no retry loop and never silently upgrades Demo to production.
    """
    environment = normalize_environment(credentials.get("environment"))
    if demo_only and environment != "demo":
        raise KalshiOrderError("Order adapter is in Demo-only mode; production submission is blocked.")
    if environment == "production":
        gates = {
            "LIVE_TRADING_ENABLED": os.getenv("LIVE_TRADING_ENABLED", "false"),
            "LIVE_TRADING_APPROVED": os.getenv("LIVE_TRADING_APPROVED", "false"),
            "MARKET_LIVE_EXECUTION_ENABLED": os.getenv("MARKET_LIVE_EXECUTION_ENABLED", "false"),
        }
        if any(str(value).strip().lower() not in {"1", "true", "yes", "on"} for value in gates.values()):
            raise KalshiOrderError("Production order submission is locked: three independent live-execution gates must be explicitly enabled after Demo validation.")
    if not isinstance(ticker, str) or not ticker.strip() or len(ticker) > 200 or not re.fullmatch(r"[A-Za-z0-9._-]+", ticker):
        raise ValueError("Invalid market ticker.")
    side = str(outcome_side or "").strip().upper()
    if side not in {"YES", "NO"}:
        raise ValueError("outcome_side must be YES or NO.")
    if isinstance(contracts, bool) or not isinstance(contracts, int) or not 1 <= contracts <= max(1, min(10, int(os.getenv("KALSHI_MAX_ORDER_CONTRACTS", "1")))):
        raise ValueError("Contract count exceeds the configured hard cap.")
    try:
        price_cents = float(limit_price_cents)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid order price.") from None
    max_price = max(1.0, min(99.0, float(os.getenv("KALSHI_MAX_ORDER_PRICE_CENTS", "90"))))
    if not 1 <= price_cents <= max_price:
        raise ValueError("Order price is outside the configured hard cap.")
    if not isinstance(client_order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", client_order_id):
        raise ValueError("client_order_id must be 8-64 ASCII letters, numbers, underscores, or hyphens.")
    # V2 side is the YES book side: buy YES => bid at YES limit; buy NO => ask/sell
    # YES at the complementary YES price. All API prices are fixed-point dollars.
    book_side = "bid" if side == "YES" else "ask"
    yes_price_cents = price_cents if side == "YES" else 100.0 - price_cents
    payload = {
        "ticker": ticker.strip(), "client_order_id": client_order_id,
        "side": book_side, "count": str(contracts), "price": f"{yes_price_cents / 100.0:.4f}",
        "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross",
        "post_only": False, "cancel_order_on_pause": True, "reduce_only": False,
        "subaccount": 0, "exchange_index": 0,
    }
    result = _request(credentials, "POST", ORDER_PATH, payload=payload)
    order = result.get("order") if isinstance(result.get("order"), dict) else result
    returned_client_id = order.get("client_order_id")
    if returned_client_id is not None and returned_client_id != client_order_id:
        raise KalshiOrderError("Exchange response client_order_id mismatch; reconcile manually.")
    return {
        "environment": environment, "order_id": order.get("order_id"),
        "client_order_id": client_order_id, "ticker": ticker.strip(),
        "outcome_side": side, "book_side": book_side, "limit_price_cents": price_cents,
        "fill_count": order.get("fill_count", order.get("fill_count_fp")),
        "remaining_count": order.get("remaining_count", order.get("remaining_count_fp")),
        "status": order.get("status", "accepted"), "response_received": True,
    }


def get_order(credentials: Mapping[str, Any], order_id: str) -> dict[str, Any]:
    if not isinstance(order_id, str) or not order_id or len(order_id) > 200:
        raise ValueError("Invalid order id.")
    return _request(credentials, "GET", f"/portfolio/orders/{order_id}")


def cancel_order(credentials: Mapping[str, Any], order_id: str, *, market_ticker: str) -> dict[str, Any]:
    if not isinstance(order_id, str) or not order_id or len(order_id) > 200:
        raise ValueError("Invalid order id.")
    if not isinstance(market_ticker, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", market_ticker):
        raise ValueError("Invalid market ticker.")
    return _request(credentials, "DELETE", f"{ORDER_PATH}/{order_id}", params={"market_ticker": market_ticker, "exchange_index": -1})
