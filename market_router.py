"""Research-only market classification/router foundation.

Only the existing BTC 15-minute profile can route to an existing strategy. Other
markets are classified for inventory/readiness visibility but are never handed to
the BTC engine and never produce a trade from this module.
"""
from __future__ import annotations

from collections import Counter
from itertools import islice
from typing import Any, Iterable

from market_category_registry import classify_market


def _bounded_text(value: Any, limit: int) -> str:
    """Accept text fields only and truncate before stripping to bound work on public data."""
    if not isinstance(value, str):
        return ""
    return value[:limit].strip()


def classify_and_route(market: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(market, dict):
        market = {}
    ticker = _bounded_text(market.get("ticker") or market.get("market_ticker"), 120)
    title_parts = [_bounded_text(market.get(key), 600) for key in ("title", "subtitle", "yes_sub_title", "no_sub_title")]
    if not any(title_parts):
        title_parts.extend(_bounded_text(market.get(key), 600) for key in ("event_title", "event_sub_title"))
    title = " ".join(part for part in title_parts if part)[:600]
    category_hint = _bounded_text(market.get("category") or market.get("market_category"), 80)
    classification = classify_market(ticker, title, category_hint or None)

    routable = classification.get("category") == "btc_15m" and classification.get("paper_strategy_enabled") is True
    if routable:
        route_status = "btc_paper_strategy_only"
        strategy_key = "btc_15m"
        reason = "Routes to the existing BTC-only paper strategy; all current entry and risk gates still apply."
    elif classification.get("status") == "research_only":
        route_status = "research_only"
        strategy_key = None
        reason = "Classified for readiness inventory only; no strategy is enabled for this category."
    else:
        route_status = "blocked_unclassified"
        strategy_key = None
        reason = "The market could not be matched to a validated strategy profile. It cannot be traded."

    return {
        "ticker": ticker or None,
        "title": title or None,
        "category": classification.get("category", "unclassified"),
        "category_label": classification.get("label", "Unclassified market"),
        "category_status": classification.get("status", "blocked_unclassified"),
        "route_status": route_status,
        "strategy_key": strategy_key,
        "paper_strategy_enabled": bool(routable),
        "trade_submitted": False,
        "reason": reason,
    }


def scan_market_catalog(markets: Iterable[dict[str, Any]], *, max_results: int = 50) -> dict[str, Any]:
    """Summarize market categories without trading; output size is strictly bounded."""
    # Bound consumption as well as output. Avoid materializing a potentially
    # unbounded generator before applying the cap.
    rows = [classify_and_route(market) for market in islice(markets, 500) if isinstance(market, dict)]
    counts = Counter(row["category"] for row in rows)
    route_counts = Counter(row["route_status"] for row in rows)
    capped = max(1, min(int(max_results), 100))
    return {
        "mode": "RESEARCH_ONLY",
        "candidate_count": len(rows),
        "returned_count": min(len(rows), capped),
        "category_counts": dict(sorted(counts.items())),
        "route_counts": dict(sorted(route_counts.items())),
        "trade_submitted": False,
        "active_strategy": "btc_15m",
        "markets": rows[:capped],
        "safety_note": "Inventory only. Non-BTC categories are not paper-enabled, no order endpoint is called, and unknown markets remain blocked.",
    }
