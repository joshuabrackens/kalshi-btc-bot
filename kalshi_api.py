"""
Small Kalshi API client using the official API's public market-data endpoints.
Authenticated order placement is deliberately NOT implemented in this starter.
Check the current Kalshi API documentation before using this against a live account.
"""
from __future__ import annotations
import os
import requests

BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
TIMEOUT = 12


class KalshiAPIError(RuntimeError):
    pass


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise KalshiAPIError(f"Kalshi request failed: {exc}") from exc


def get_markets(status: str = "open", limit: int = 100, cursor: str | None = None,
                series_ticker: str | None = None) -> dict:
    params = {"status": status, "limit": max(1, min(int(limit), 1000))}
    if cursor:
        params["cursor"] = cursor
    if series_ticker:
        params["series_ticker"] = series_ticker
    return _get("/markets", params)


def get_market(ticker: str) -> dict:
    return _get(f"/markets/{ticker}")


def get_orderbook(ticker: str) -> dict:
    return _get(f"/markets/{ticker}/orderbook")



def find_open_btc_markets() -> list[dict]:
    """Return only markets from Kalshi's Bitcoin 15-minute series."""
    found = []
    cursor = None

    for _ in range(5):
        payload = get_markets(
            status="open",
            limit=200,
            cursor=cursor,
            series_ticker="KXBTC15M",
        )

        for market in payload.get("markets", []):
            ticker = str(market.get("ticker", "")).upper()
            event_ticker = str(market.get("event_ticker", "")).upper()

            # Require the actual BTC 15-minute series identifier.
            if (
                ticker.startswith("KXBTC15M-")
                and "KXBTC15M" in event_ticker
                and market.get("status") == "active"
            ):
                found.append(market)

        cursor = payload.get("cursor")
        if not cursor:
            break

    return found

