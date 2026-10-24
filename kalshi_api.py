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
def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(value, maximum))


MAX_RETRIES = _int_env("KALSHI_GET_MAX_RETRIES", 4, 0, 5)
_RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
_SESSION = requests.Session()
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0
_RATE_LIMITED_UNTIL = 0.0


class KalshiAPIError(RuntimeError):
    """A public Kalshi market-data request failed, with safe structured diagnostics."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code if isinstance(status_code, int) else None
        self.retryable = bool(retryable)


def _request_spacing_seconds() -> float:
    try:
        value = float(os.getenv("KALSHI_MIN_REQUEST_INTERVAL_SECONDS", "1.0"))
    except (TypeError, ValueError, OverflowError):
        value = 0.5
    return max(0.0, min(value, 2.0))


def _wait_for_request_slot() -> None:
    """Serialize requests and re-check a rate-limit cooldown that changes while waiting."""
    global _NEXT_REQUEST_AT
    spacing = _request_spacing_seconds()
    while True:
        with _RATE_LOCK:
            now = time.monotonic()
            slot = max(now, _NEXT_REQUEST_AT, _RATE_LIMITED_UNTIL)
            wait = max(0.0, slot - now)
            _NEXT_REQUEST_AT = slot + spacing
            if wait <= 0:
                return
        time.sleep(wait)
        # Do not re-apply this caller's own reserved slot after sleeping; only
        # loop if another request extended the shared server cooldown meanwhile.
        with _RATE_LOCK:
            cooldown_remaining = _RATE_LIMITED_UNTIL - time.monotonic()
        if cooldown_remaining <= 0:
            return


def _activate_rate_limit_cooldown(delay_seconds: float) -> None:
    """Pause all API callers in this process after a 429, not only the failing request."""
    global _NEXT_REQUEST_AT, _RATE_LIMITED_UNTIL
    delay = max(0.25, min(float(delay_seconds), 300.0))
    with _RATE_LOCK:
        deadline = time.monotonic() + delay
        _RATE_LIMITED_UNTIL = max(_RATE_LIMITED_UNTIL, deadline)
        _NEXT_REQUEST_AT = max(_NEXT_REQUEST_AT, _RATE_LIMITED_UNTIL)


def _default_rate_limit_delay(attempt: int) -> float:
    """Use a slower bounded fallback when the API omits Retry-After on HTTP 429."""
    try:
        base = float(os.getenv("KALSHI_RATE_LIMIT_COOLDOWN_SECONDS", "30"))
    except (TypeError, ValueError, OverflowError):
        base = 30.0
    base = max(5.0, min(base, 300.0))
    return min(base * (2 ** max(0, int(attempt))), 300.0)


def _retry_after_seconds(value: str | None, attempt: int, *, status_code: int | None = None) -> float:
    """Return the server's Retry-After (up to 5 minutes) or an appropriate fallback."""
    if value:
        try:
            return max(0.25, min(float(value), 300.0))
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0.25, min((parsed - datetime.now(timezone.utc)).total_seconds(), 300.0))
            except (TypeError, ValueError, OverflowError):
                pass
    if status_code == 429:
        return _default_rate_limit_delay(attempt)
    return min(1.0 * (2 ** attempt), 8.0)


def _get(path: str, params: dict | None = None) -> dict[str, Any]:
    """GET a public endpoint with bounded retries and useful, secret-safe errors.

    Do not include the full URL in exceptions: market-list URLs may contain opaque
    pagination cursors. Diagnostics expose only the HTTP status and retryability.
    """
    url = f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    last_status: int | None = None
    last_exception_type: str | None = None
    last_retryable = False
    for attempt in range(MAX_RETRIES + 1):
        _wait_for_request_slot()
        response = None
        try:
            response = _SESSION.get(url, params=params, timeout=TIMEOUT, allow_redirects=False)
            last_status = int(response.status_code)
            if last_status in _RETRYABLE_HTTP_STATUS:
                last_retryable = True
                retry_delay = _retry_after_seconds(
                    response.headers.get("Retry-After"), attempt, status_code=last_status,
                )
                if last_status == 429:
                    _activate_rate_limit_cooldown(retry_delay)
                if attempt < MAX_RETRIES:
                    time.sleep(retry_delay)
                    continue
                raise KalshiAPIError(
                    f"Kalshi public API returned HTTP {last_status} after bounded retries.",
                    status_code=last_status, retryable=True,
                )
            if last_status < 200 or last_status >= 300:
                # Avoid including body, cursor, or request URL in client-visible diagnostics.
                raise KalshiAPIError(
                    f"Kalshi public API returned HTTP {last_status}.",
                    status_code=last_status, retryable=False,
                )
            try:
                payload = response.json()
            except ValueError:
                last_exception_type = "InvalidJSON"
                last_retryable = True
                if attempt < MAX_RETRIES:
                    time.sleep(_retry_after_seconds(response.headers.get("Retry-After"), attempt))
                    continue
                raise KalshiAPIError(
                    "Kalshi public API returned invalid JSON after bounded retries.",
                    status_code=last_status, retryable=True,
                ) from None
            if not isinstance(payload, dict):
                raise KalshiAPIError(
                    "Kalshi public API returned a non-object JSON response.",
                    status_code=last_status, retryable=False,
                )
            return payload
        except KalshiAPIError:
            raise
        except requests.RequestException as exc:
            last_exception_type = type(exc).__name__
            status = getattr(getattr(exc, "response", None), "status_code", None)
            last_status = status if isinstance(status, int) else None
            last_retryable = last_status in _RETRYABLE_HTTP_STATUS or last_status is None
            if attempt < MAX_RETRIES and last_retryable:
                retry_after = response.headers.get("Retry-After") if response is not None else None
                retry_delay = _retry_after_seconds(retry_after, attempt, status_code=last_status)
                if last_status == 429:
                    _activate_rate_limit_cooldown(retry_delay)
                time.sleep(retry_delay)
                continue
            break
    status_fragment = f"HTTP {last_status}" if last_status is not None else "network/transport error"
    detail = f" ({last_exception_type})" if last_exception_type else ""
    raise KalshiAPIError(
        f"Kalshi public API request failed after bounded retries: {status_fragment}{detail}.",
        status_code=last_status, retryable=last_retryable,
    ) from None


def get_markets(status: str = "open", limit: int = 100, cursor: str | None = None,
                series_ticker: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"status": status, "limit": max(1, min(int(limit), 1000))}
    if cursor:
        params["cursor"] = cursor
    if series_ticker:
        params["series_ticker"] = series_ticker
    return _get("/markets", params)


def get_events(
    status: str = "open",
    limit: int = 200,
    cursor: str | None = None,
    *,
    with_nested_markets: bool = True,
    tickers: list[str] | tuple[str, ...] | None = None,
    series_ticker: str | None = None,
) -> dict[str, Any]:
    """Read public Kalshi events and their category metadata.

    The event schema contains official category labels, unlike market rows. The API
    caps event pages at 200. This function intentionally does not authenticate or
    submit orders. A caller may pass exact event tickers for metadata hydration.
    """
    params: dict[str, Any] = {
        "status": status,
        "limit": max(1, min(int(limit), 200)),
        "with_nested_markets": "true" if with_nested_markets else "false",
    }
    if cursor:
        params["cursor"] = cursor
    if series_ticker:
        params["series_ticker"] = series_ticker
    if tickers:
        safe_tickers = [str(item).strip()[:120] for item in tickers if isinstance(item, str) and item.strip()]
        if safe_tickers:
            # URLs have practical length limits. Batch at most 40 identifiers.
            params["tickers"] = ",".join(safe_tickers[:40])
    return _get("/events", params)



def get_multivariate_events(
    limit: int = 200,
    cursor: str | None = None,
    *,
    with_nested_markets: bool = True,
) -> dict[str, Any]:
    """Read public Kalshi multivariate/combo events with bounded pagination.

    Kalshi exposes dynamically created multivariate events on a separate endpoint;
    they are not guaranteed to appear in the standard event catalog. This helper is
    public-data only and never submits or changes orders.
    """
    params: dict[str, Any] = {
        "limit": max(1, min(int(limit), 200)),
        "with_nested_markets": "true" if with_nested_markets else "false",
    }
    if cursor:
        params["cursor"] = cursor
    return _get("/events/multivariate", params)

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
