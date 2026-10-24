"""Read-only diagnostic for public Kalshi event pagination; never prints cursor values."""
from __future__ import annotations

import argparse
import os
import time
from typing import Any
from urllib.parse import urlparse

import requests


def _summarize(label: str, response: Any, page_number: int = 1) -> list[str]:
    lines = [f"{label} page {page_number}: HTTP {getattr(response, 'status_code', 'unknown')}"]
    headers = getattr(response, "headers", {})
    lines.append(f"  content-type: {headers.get('content-type', 'unknown')}")
    try:
        payload = response.json()
    except (ValueError, AttributeError):
        lines.append("  JSON: invalid or unavailable (body omitted for safety)")
        return lines
    if not isinstance(payload, dict):
        lines.append("  JSON: top-level payload is not an object")
        return lines
    events = payload.get("events")
    if isinstance(events, list):
        nested_count = 0
        categories: set[str] = set()
        for event in events:
            if not isinstance(event, dict):
                continue
            markets = event.get("markets")
            if isinstance(markets, list):
                nested_count += sum(isinstance(market, dict) for market in markets)
            category = event.get("category")
            if isinstance(category, str) and category.strip():
                categories.add(category.strip()[:60])
        lines.append(f"  events returned: {len(events)}")
        lines.append(f"  nested market rows: {nested_count}")
        lines.append(f"  categories sampled: {', '.join(sorted(categories)[:12]) or '(none)'}")
    elif isinstance(payload.get("markets"), list):
        lines.append(f"  market rows: {len(payload['markets'])}")
    else:
        lines.append("  response schema: no events/markets array")
    cursor = payload.get("cursor")
    has_cursor = isinstance(cursor, str) and bool(cursor.strip())
    lines.append(f"  cursor present: {has_cursor}")
    lines.append(f"  cursor length: {len(cursor) if isinstance(cursor, str) else 'not text'} (value never displayed)")
    if has_cursor and len(cursor) > 2048:
        lines.append("  warning: cursor is longer than the application's 2048-character safety limit")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only diagnostic for the standard and multivariate Kalshi catalogs.")
    parser.add_argument("--pages", type=int, default=3, help="Pages per feed to test, 1-20 (default: 3).")
    args = parser.parse_args()
    pages_to_test = max(1, min(int(args.pages), 20))
    base = os.getenv("KALSHI_BASE_URL", "https://external-api.kalshi.com/trade-api/v2").strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or parsed.hostname not in {"external-api.kalshi.com", "api.elections.kalshi.com"}:
        print("KALSHI_BASE_URL must be an HTTPS Kalshi API host. Request not sent.")
        return 2
    feeds = [
        ("standard events", "/events", {"status": "open", "limit": 200, "with_nested_markets": "true"}),
        ("multivariate events", "/events/multivariate", {"limit": 200, "with_nested_markets": "true"}),
    ]
    failures = 0
    session = requests.Session()
    delay = 0.5
    try:
        delay = max(0.0, min(float(os.getenv("KALSHI_MIN_REQUEST_INTERVAL_SECONDS", "0.5")), 2.0))
    except (TypeError, ValueError, OverflowError):
        pass
    for label, path, base_params in feeds:
        cursor: str | None = None
        for page_number in range(1, pages_to_test + 1):
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            try:
                response = session.get(base + path, params=params, timeout=20, allow_redirects=False)
            except requests.RequestException as exc:
                print(f"{label} page {page_number}: network error ({type(exc).__name__}); no private data involved")
                failures += 1
                break
            print("\n".join(_summarize(label, response, page_number)))
            if response.status_code < 200 or response.status_code >= 300:
                failures += 1
                print("  upstream returned non-2xx; response body omitted. Stop this feed here.")
                break
            try:
                payload = response.json()
            except ValueError:
                failures += 1
                break
            next_cursor = payload.get("cursor") if isinstance(payload, dict) else None
            if not isinstance(next_cursor, str) or not next_cursor.strip():
                print("  feed exhausted before requested diagnostic page count")
                break
            if len(next_cursor) > 2048:
                failures += 1
                print("  next cursor exceeds application safety length; stop this feed")
                break
            if next_cursor == cursor:
                failures += 1
                print("  repeated cursor detected; stop this feed")
                break
            cursor = next_cursor
            if delay:
                time.sleep(delay)
    print(f"\nCatalog diagnostic result: {'PASS' if failures == 0 else 'FAIL'} ({failures} endpoint/page failure(s)). No orders were placed.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
