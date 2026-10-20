"""Public Kalshi market-data client. No authenticated order placement exists here."""
from __future__ import annotations
import os
import requests
BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
TIMEOUT = 12
class KalshiAPIError(RuntimeError): pass

def _get(path: str, params: dict | None = None) -> dict:
    url=f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    try:
        response=requests.get(url,params=params,timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise KalshiAPIError(f"Kalshi request failed: {exc}") from exc

def get_markets(status="open",limit=100,cursor=None,series_ticker=None):
    params={"status":status,"limit":max(1,min(int(limit),1000))}
    if cursor: params["cursor"]=cursor
    if series_ticker: params["series_ticker"]=series_ticker
    return _get("/markets",params)
def get_market(ticker): return _get(f"/markets/{ticker}")
def get_orderbook(ticker): return _get(f"/markets/{ticker}/orderbook")
def find_open_btc_markets():
    found=[]; cursor=None
    for _ in range(5):
        payload=get_markets(status="open",limit=200,cursor=cursor,series_ticker="KXBTC15M")
        for m in payload.get("markets",[]):
            ticker=str(m.get("ticker"," ")).upper(); status=str(m.get("status","")).lower()
            if ticker.startswith("KXBTC15M-") and status in ("active","open"): found.append(m)
        cursor=payload.get("cursor")
        if not cursor: break
    return found
