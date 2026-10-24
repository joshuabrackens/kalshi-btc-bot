"""Public Kalshi market-data client with conservative rate handling.

This module is deliberately read-only: it contains no authenticated order route.
It spaces GET requests, retries transient network/429/5xx responses with bounded
backoff, and respects Retry-After when the server supplies it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import os
import threading
import time
from typing import Any

import requests

BASE_URL = os.getenv("KALSHI_BASE_URL", "https://external-api.kalshi.com/trade-api/v2")
TIMEOUT = max(2.0, min(float(os.getenv("KALSHI_REQUEST_TIMEOUT_SECONDS", "12")), 30.0))
MAX_RETRIES = max(0, min(int(os.getenv("KALSHI_GET_MAX_RETRIES", "2")), 4))
_RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
_SESSION = requests.Session()
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


class KalshiAPIError(RuntimeError):
    """A public Kalshi market-data request failed."""


def _request_spacing_seconds() -> float:
    try:
        value = float(os.getenv("KALSHI_MIN_REQUEST_INTERVAL_SECONDS", "0.25"))
    except (TypeError, ValueError, OverflowError):
        value = 0.25
    return max(0.0, min(value, 2.0))


def _wait_for_request_slot() -> None:
    """Space requests within this process to reduce accidental bursts."""
    global _NEXT_REQUEST_AT
    spacing = _request_spacing_seconds()
    with _RATE_LOCK:
        now = time.monotonic()
        wait = max(0.0, _NEXT_REQUEST_AT - now)
        _NEXT_REQUEST_AT = max(now, _NEXT_REQUEST_AT) + spacing
    if wait > 0:
        time.sleep(wait)


def _retry_after_seconds(value: str | None, attempt: int) -> float:
    """Return a bounded server-requested delay or exponential fallback."""
    if value:
        try:
            return max(0.25, min(float(value), 30.0))
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0.25, min((parsed - datetime.now(timezone.utc)).total_seconds(), 30.0))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(1.0 * (2 ** attempt), 8.0)


def _get(path: str, params: dict | None = None) -> dict[str, Any]:
    url = f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        _wait_for_request_slot()
        response = None
        try:
            response = _SESSION.get(url, params=params, timeout=TIMEOUT)
            if response.status_code in _RETRYABLE_HTTP_STATUS:
                if attempt < MAX_RETRIES:
                    time.sleep(_retry_after_seconds(response.headers.get("Retry-After"), attempt))
                    continue
                response.raise_for_status()
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise KalshiAPIError("Kalshi returned a non-object JSON response.")
            return payload
        except requests.RequestException as exc:
            last_error = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status in _RETRYABLE_HTTP_STATUS or status is None
            if attempt < MAX_RETRIES and retryable:
                retry_after = None
                if response is not None:
                    retry_after = response.headers.get("Retry-After")
                time.sleep(_retry_after_seconds(retry_after, attempt))
                continue
            break
        except ValueError as exc:
            # Malformed JSON may be transient but retry at most within this bounded loop.
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(_retry_after_seconds(None, attempt))
                continue
            break
    raise KalshiAPIError(f"Kalshi request failed after bounded retries: {last_error}") from last_error


def get_markets(status: str = "open", limit: int = 100, cursor: str | None = None,
                series_ticker: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"status": status, "limit": max(1, min(int(limit), 200))}
    if cursor:
        params["cursor"] = cursor
    if series_ticker:
        params["series_ticker"] = series_ticker
    return _get("/markets", params)


def get_market(ticker: str) -> dict[str, Any]:
    return _get(f"/markets/{ticker}")


def get_orderbook(ticker: str) -> dict[str, Any]:
    return _get(f"/markets/{ticker}/orderbook")


def find_open_btc_markets() -> list[dict[str, Any]]:
    """Fetch the open BTC 15m market set, following a small bounded number of pages."""
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    # Active BTC 15m series normally fits in one page. Keep pagination bounded
    # for unusual periods, but avoid the older 5-page scan by default.
    max_pages = max(1, min(int(os.getenv("KALSHI_MARKET_DISCOVERY_MAX_PAGES", "2")), 5))
    for _ in range(max_pages):
        payload = get_markets(status="open", limit=200, cursor=cursor, series_ticker="KXBTC15M")
        for market in payload.get("markets", []):
            ticker = str(market.get("ticker", "")).upper()
            market_status = str(market.get("status", "")).lower()
            if ticker.startswith("KXBTC15M-") and market_status in {"active", "open"}:
                found.append(market)
        cursor = payload.get("cursor")
        if not cursor:
            break
    return found
