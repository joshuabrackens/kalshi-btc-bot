from __future__ import annotations
from typing import Any
from kalshi_api import find_open_btc_markets

def discover() -> dict[str, Any]:
    candidates=find_open_btc_markets()
    return {"candidate_count":len(candidates),"warning":"Discovery is best-effort. Verify the exact 15-minute contract, strike, close time, and settlement rules before using it.","candidates":[{"ticker":m.get("ticker"),"event_ticker":m.get("event_ticker"),"title":m.get("title"),"subtitle":m.get("subtitle"),"status":m.get("status"),"close_time":m.get("close_time"),"expiration_time":m.get("expiration_time"),"yes_ask":m.get("yes_ask",m.get("yes_ask_dollars")),"no_ask":m.get("no_ask",m.get("no_ask_dollars")),"yes_bid":m.get("yes_bid",m.get("yes_bid_dollars")),"no_bid":m.get("no_bid",m.get("no_bid_dollars")),"raw":m} for m in candidates]}
