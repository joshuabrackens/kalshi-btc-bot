"""Per-category performance reporting for the isolated market-research paper journal.

Research-market results are intentionally separate from the BTC 15-minute strategy
journal. A display group is for reporting only and never grants trade eligibility.
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterable

from market_category_registry import market_display_group

_GROUP_ORDER = [
    "NFL", "NBA", "WNBA", "NHL", "MLB", "NCAA Sports", "Other Sports",
    "Politics", "Crypto Events", "BTC 15-minute", "Economics", "Weather",
    "Financial Markets", "Entertainment", "Companies", "World Events",
    "Science & Tech", "Other / Unclassified",
]


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _decision_snapshot(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def build_research_category_performance(rows: Iterable[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """Build distinct, fee/slippage-aware paper metrics by category display group.

    Each row is (category, status, settled_at, pnl_cents, source_snapshot_json, ticker).
    Missing outcomes are never counted as wins or losses. Empty groups are returned
    explicitly with zero counts so the dashboard never suggests unrecorded results.
    """
    groups: dict[str, dict[str, Any]] = {}

    def ensure(label: str, category: str | None = None) -> dict[str, Any]:
        if label not in groups:
            groups[label] = {
                "display_group": label,
                "category": category or "unclassified",
                "total_trades": 0,
                "open_trades": 0,
                "settled_trades": 0,
                "wins": 0,
                "losses": 0,
                "break_even": 0,
                "net_pnl_cents": 0.0,
            }
        elif category and groups[label]["category"] == "unclassified":
            groups[label]["category"] = category
        return groups[label]

    for label in _GROUP_ORDER:
        ensure(label)

    for row in rows or []:
        if row is None or len(row) < 6:
            continue
        category = str(row[0] or "unclassified").strip().lower()
        status = str(row[1] or "").strip().lower()
        settled_at = row[2]
        pnl = _finite(row[3])
        snapshot = _decision_snapshot(row[4])
        decision = snapshot.get("decision") if isinstance(snapshot.get("decision"), dict) else snapshot
        ticker = str(row[5] or decision.get("ticker") or "")
        label = decision.get("display_group")
        if not isinstance(label, str) or not label.strip():
            label = market_display_group(
                category, ticker, decision.get("title"), decision.get("event_title"),
                decision.get("event_ticker"), decision.get("event_sub_title"),
            )
        label = label.strip()[:80] or "Other / Unclassified"
        group = ensure(label, category)
        group["total_trades"] += 1
        if status != "settled" or settled_at is None:
            group["open_trades"] += 1
            continue
        group["settled_trades"] += 1
        if pnl is None:
            # A row marked settled without P&L is data-quality debt, not a win.
            group["break_even"] += 1
            continue
        group["net_pnl_cents"] += pnl
        if pnl > 0:
            group["wins"] += 1
        elif pnl < 0:
            group["losses"] += 1
        else:
            group["break_even"] += 1

    results: list[dict[str, Any]] = []
    ordered = [label for label in _GROUP_ORDER if label in groups]
    ordered.extend(sorted(label for label in groups if label not in _GROUP_ORDER))
    for label in ordered:
        group = groups[label]
        settled = int(group["settled_trades"])
        group["net_pnl_cents"] = round(float(group["net_pnl_cents"]), 3)
        group["net_pnl_dollars"] = round(group["net_pnl_cents"] / 100, 4)
        group["average_net_pnl_cents"] = round(group["net_pnl_cents"] / settled, 3) if settled else None
        group["win_rate_percent"] = round(100 * group["wins"] / settled, 2) if settled else None
        group["sample_status"] = (
            "No paper trades recorded" if group["total_trades"] == 0 else
            "No settled outcomes yet" if settled == 0 else
            "Small sample; do not tune from this group alone" if settled < 20 else
            "Descriptive result; continue out-of-sample validation"
        )
        results.append(group)
    return results
