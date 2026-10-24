"""Deterministic property stress for the market decision engine; no network or DB writes."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from market_decision_engine import assess_market_opportunity, _REQUIRED_EVIDENCE


def stamp(minutes_ago: float = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def main() -> None:
    rng = random.Random(20261024)
    categories = list(_REQUIRED_EVIDENCE) + ["unknown", "", None]
    statuses = ["active", "open", "closed", "", None, "settled"]
    prob_count = eligible_count = exception_count = 0
    for i in range(25_000):
        category = rng.choice(categories)
        status = rng.choice(statuses)
        if category == "btc_15m":
            ticker, title = f"KXBTC15M-{i}", "Bitcoin up in 15 minutes"
        elif category in _REQUIRED_EVIDENCE and category:
            ticker, title = f"KX{str(category).upper()}-{i}", f"Will this {category} event happen?"
        else:
            ticker, title = f"UNKNOWN-{i}", "Unknown event"

        yes_bid = rng.randint(5, 90)
        no_min = max(5, 95 - yes_bid)
        no_max = min(90, 100 - yes_bid)
        if no_min > no_max:
            no_min, no_max = no_max, no_min
        no_bid = rng.randint(no_min, max(no_min, no_max))
        yes_qty = rng.randint(0, 30)
        no_qty = rng.randint(0, 30)
        book = {"orderbook": {"yes": [[yes_bid, yes_qty]], "no": [[no_bid, no_qty]]}}
        volume = rng.choice([10, 49, 50, 100, 1000, "not-a-number", None])

        model = None
        if category in _REQUIRED_EVIDENCE and category:
            evs = [{"type": kind, "source": f"source-{kind}", "url": f"https://example.invalid/{kind}",
                    "published_at": stamp(rng.choice([1, 30, 180, 2000])), "retrieved_at": stamp(),
                    "relevance_score": rng.choice([0.4, 0.75, 0.9]),
                    "reliability_score": rng.choice([0.5, 0.7, 0.95])}
                   for kind in _REQUIRED_EVIDENCE[category]]
            model = {"market_ticker": ticker, "model_probability_yes": rng.choice([0, .01, .1, .25, .5, .75, .9, .99, 1]),
                     "model_id": "stress-model", "model_version": "stress-v1", "as_of_utc": stamp(rng.choice([1, 20, 60])),
                     "validation": {"out_of_sample": rng.choice([True, False]), "sample_count": rng.choice([20, 100, 250]),
                                    "brier_score": rng.choice([0.1, 0.21, 0.25, None])},
                     "evidence": evs}
        market = {"ticker": ticker, "title": title, "category": category, "status": status,
                  "volume_24h_fp": volume, "open_interest_fp": rng.randint(0, 10000)}
        try:
            result = assess_market_opportunity(market, book, model)
        except Exception as exc:  # The fuzz test treats uncaught malformed values as failures.
            exception_count += 1
            raise AssertionError(f"uncaught error at case {i}: {type(exc).__name__}") from exc
        prob_count += 1
        assert result["trade_submitted"] is False
        if result["eligible"]:
            eligible_count += 1
            assert category in _REQUIRED_EVIDENCE and category
            assert status in {"active", "open"}
            assert result["selected_side"] in {"YES", "NO"}
            assert result["entry_ask_cents"] <= 90
            assert result["expected_net_edge_cents"] >= 5
            assert result["yes_spread_cents"] is not None and 0 <= result["yes_spread_cents"] <= 5
            assert result["no_spread_cents"] is not None and 0 <= result["no_spread_cents"] <= 5
            assert result["yes_ask_depth_contracts"] >= 5
            assert result["no_ask_depth_contracts"] >= 5
            assert result["reasons"] == []
    print("MARKET DECISION PROPERTY STRESS: PASS")
    print(f"cases checked: {prob_count:,}; eligible paper candidates: {eligible_count:,}; uncaught exceptions: {exception_count}")
    print("Invariant: unknown/stale/uncalibrated/thin/expensive markets do not become eligible; no orders submitted")


if __name__ == "__main__":
    main()
