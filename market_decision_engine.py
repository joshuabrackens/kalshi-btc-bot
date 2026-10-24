"""Fail-closed, fee-aware market opportunity assessment for Kalshi paper research.

This module does not make a probability from headlines or market prices. It accepts
only an explicitly calibrated category model with out-of-sample validation and fresh,
source-attributed evidence. It never submits a live order.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping

_REQUIRED_EVIDENCE: dict[str, set[str]] = {
    "sports": {"injury_report", "lineup", "recent_news", "recent_stats"},
    "politics": {"official", "polling", "recent_news"},
    "economics": {"official_release", "consensus", "recent_news"},
    "weather": {"forecast", "observation"},
    "crypto_event": {"exchange_data", "official", "recent_news"},
    "financial_markets": {"price_data", "official", "recent_news"},
    "entertainment": {"official", "recent_news"},
    "btc_15m": {"exchange_data", "recent_news"},
}
_CATEGORY_MAX_AGE_HOURS = {
    "sports": 12, "politics": 48, "economics": 48, "weather": 12,
    "crypto_event": 6, "financial_markets": 6, "entertainment": 24, "btc_15m": 2,
}


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if isfinite(value) else None


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _price(market: Mapping[str, Any], side: str, kind: str) -> float | None:
    raw = market.get(f"{side}_{kind}_dollars")
    dollars = raw is not None
    if raw is None:
        raw = market.get(f"{side}_{kind}")
    number = _num(raw)
    if number is None:
        return None
    number = number * 100 if dollars else number
    return round(number, 4) if 0 <= number <= 100 else None


def _orderbook_summary(orderbook: Mapping[str, Any] | None, market: Mapping[str, Any]) -> dict[str, float | None]:
    """Derive executable-ish top-of-book quotes from Kalshi bid-only yes/no books."""
    payload: Mapping[str, Any] = orderbook if isinstance(orderbook, Mapping) else {}
    nested = payload.get("orderbook")
    if isinstance(nested, Mapping):
        payload = nested

    def levels(key: str) -> list[tuple[float, float]]:
        raw = payload.get(key)
        if not isinstance(raw, list):
            return []
        out: list[tuple[float, float]] = []
        for item in raw[:100]:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            price, qty = _num(item[0]), _num(item[1])
            if price is None or qty is None or not 0 <= price <= 100 or qty <= 0:
                continue
            out.append((price, qty))
        return out

    yes_levels, no_levels = levels("yes"), levels("no")
    yes_bid = max((p for p, _ in yes_levels), default=_price(market, "yes", "bid"))
    no_bid = max((p for p, _ in no_levels), default=_price(market, "no", "bid"))
    # One side's best bid implies the other side's best ask in a binary orderbook.
    yes_ask = 100 - no_bid if no_bid is not None else _price(market, "yes", "ask")
    no_ask = 100 - yes_bid if yes_bid is not None else _price(market, "no", "ask")
    yes_depth = sum(q for p, q in no_levels if no_bid is not None and p == no_bid)
    no_depth = sum(q for p, q in yes_levels if yes_bid is not None and p == yes_bid)
    yes_spread = (yes_ask - yes_bid) if yes_ask is not None and yes_bid is not None else None
    no_spread = (no_ask - no_bid) if no_ask is not None and no_bid is not None else None
    return {
        "yes_bid_cents": yes_bid, "yes_ask_cents": yes_ask,
        "no_bid_cents": no_bid, "no_ask_cents": no_ask,
        "yes_spread_cents": yes_spread, "no_spread_cents": no_spread,
        "yes_ask_depth_contracts": yes_depth or None,
        "no_ask_depth_contracts": no_depth or None,
    }


def validate_model_evidence(
    model: Any,
    category: str,
    *,
    now: datetime | None = None,
    min_validation_samples: int = 100,
    max_brier_score: float = 0.22,
    max_model_age_minutes: int = 30,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Require calibrated, time-valid, source-provenanced model output."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reasons: list[str] = []
    if not isinstance(model, Mapping):
        return False, ["probability_provider_unavailable"], {}
    probability = _num(model.get("model_probability_yes"))
    if probability is None or not 0 < probability < 1:
        reasons.append("invalid_model_probability")
    model_id = model.get("model_id")
    model_version = model.get("model_version")
    if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 100:
        reasons.append("missing_model_id")
    if not isinstance(model_version, str) or not model_version.strip() or len(model_version) > 100:
        reasons.append("missing_model_version")
    validation = model.get("validation") if isinstance(model.get("validation"), Mapping) else {}
    sample_count = _num(validation.get("sample_count"))
    brier = _num(validation.get("brier_score"))
    if validation.get("out_of_sample") is not True:
        reasons.append("model_not_validated_out_of_sample")
    if sample_count is None or sample_count < min_validation_samples:
        reasons.append("insufficient_out_of_sample_validation")
    if brier is None or not 0 <= brier <= max_brier_score:
        reasons.append("brier_score_unavailable_or_too_high")
    model_as_of = _stamp(model.get("as_of_utc"))
    if model_as_of is None:
        reasons.append("model_timestamp_missing")
    else:
        age_minutes = (now - model_as_of).total_seconds() / 60
        if age_minutes < -2:
            reasons.append("model_timestamp_in_future")
        elif age_minutes > max_model_age_minutes:
            reasons.append("model_output_stale")

    required = _REQUIRED_EVIDENCE.get(category, set())
    evidence = model.get("evidence") if isinstance(model.get("evidence"), list) else []
    valid_types: set[str] = set()
    valid_sources: list[dict[str, Any]] = []
    max_age_hours = _CATEGORY_MAX_AGE_HOURS.get(category, 24)
    for item in evidence[:50]:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        source = item.get("source")
        url = item.get("url")
        published = _stamp(item.get("published_at"))
        retrieved = _stamp(item.get("retrieved_at"))
        relevance, reliability = _num(item.get("relevance_score")), _num(item.get("reliability_score"))
        if not isinstance(kind, str) or len(kind) > 60:
            continue
        if not isinstance(source, str) or not source.strip() or len(source) > 180:
            continue
        if not isinstance(url, str) or not url.startswith("https://") or len(url) > 2048:
            continue
        if published is None or retrieved is None:
            continue
        age_h = (now - published).total_seconds() / 3600
        retrieval_age_min = (now - retrieved).total_seconds() / 60
        if age_h < -0.05 or age_h > max_age_hours or retrieval_age_min < -2 or retrieval_age_min > 30:
            continue
        if relevance is None or reliability is None or relevance < 0.75 or reliability < 0.70:
            continue
        valid_types.add(kind.strip().lower())
        valid_sources.append({
            "type": kind.strip().lower(), "source": source.strip(), "url": url,
            "published_at": published.isoformat(), "retrieved_at": retrieved.isoformat(),
            "relevance_score": relevance, "reliability_score": reliability,
        })
    missing = sorted(required - valid_types)
    if missing:
        reasons.append("required_evidence_missing:" + ",".join(missing))
    return not reasons, reasons, {
        "model_id": model_id.strip() if isinstance(model_id, str) else None,
        "model_version": model_version.strip() if isinstance(model_version, str) else None,
        "model_probability_yes": probability,
        "validation_sample_count": int(sample_count) if sample_count is not None else None,
        "validation_brier_score": brier,
        "evidence_types": sorted(valid_types),
        "evidence_count": len(valid_sources),
        "evidence": valid_sources,
        "model_as_of_utc": model_as_of.isoformat() if model_as_of else None,
    }


def assess_market_opportunity(
    market: Mapping[str, Any],
    orderbook: Mapping[str, Any] | None,
    model: Any,
    *,
    min_volume_24h: float = 50,
    min_depth_contracts: float = 5,
    max_spread_cents: float = 5.0,
    min_net_edge_cents: float = 5.0,
    estimated_fee_cents: float = 2.0,
    slippage_cents: float = 1.0,
    max_entry_cents: float = 90.0,
    min_validation_samples: int = 100,
    max_brier_score: float = 0.22,
) -> dict[str, Any]:
    """Return an explainable paper-only go/no-go decision; never place an order."""
    from market_router import classify_and_route

    market = market if isinstance(market, Mapping) else {}
    ticker = market.get("ticker") or market.get("market_ticker")
    routed = classify_and_route(dict(market))
    category = str(routed.get("category") or "unclassified")
    quotes = _orderbook_summary(orderbook, market)
    # Configuration values may tighten these baselines, but cannot disable the
    # minimum validation/liquidity/cost safeguards via a typo or an unsafe env value.
    min_volume_24h = max(50.0, min(1_000_000_000.0, _num(min_volume_24h) or 50.0))
    min_depth_contracts = max(5.0, min(1_000_000.0, _num(min_depth_contracts) or 5.0))
    max_spread_cents = min(5.0, max(0.0, _num(max_spread_cents) if _num(max_spread_cents) is not None else 5.0))
    min_net_edge_cents = max(5.0, min(99.0, _num(min_net_edge_cents) or 5.0))
    # One-contract base taker fee is up to ~2¢ under Kalshi's general schedule;
    # special markets may differ, so operators should set a higher conservative
    # estimate when the current contract schedule requires it. Never permit zero.
    estimated_fee_cents = max(2.0, min(100.0, _num(estimated_fee_cents) if _num(estimated_fee_cents) is not None else 2.0))
    slippage_cents = max(1.0, min(100.0, _num(slippage_cents) if _num(slippage_cents) is not None else 1.0))
    max_entry_cents = min(90.0, max(1.0, _num(max_entry_cents) if _num(max_entry_cents) is not None else 90.0))
    try:
        min_validation_samples = max(100, min(10000, int(min_validation_samples)))
    except (TypeError, ValueError, OverflowError):
        min_validation_samples = 100
    max_brier_score = min(0.22, max(0.0, _num(max_brier_score) if _num(max_brier_score) is not None else 0.22))
    reasons: list[str] = []
    status = str(market.get("status") or "").lower()
    if category not in _REQUIRED_EVIDENCE:
        reasons.append("category_not_supported")
    if not isinstance(ticker, str) or not ticker.strip() or len(ticker) > 200:
        reasons.append("invalid_market_ticker")
    if status not in {"open", "active"}:
        reasons.append("market_not_open_or_status_unknown")
    volume = _num(market.get("volume_24h_fp", market.get("volume_24h")))
    if volume is None or volume < min_volume_24h:
        reasons.append("insufficient_or_unknown_24h_volume")
    yes_ask = quotes.get("yes_ask_cents")
    no_ask = quotes.get("no_ask_cents")
    yes_spread = quotes.get("yes_spread_cents")
    no_spread = quotes.get("no_spread_cents")
    if yes_ask is None or no_ask is None:
        reasons.append("executable_top_of_book_unavailable")
    if yes_spread is None or yes_spread < 0 or yes_spread > max_spread_cents:
        reasons.append("yes_spread_too_wide_or_unknown")
    if no_spread is None or no_spread < 0 or no_spread > max_spread_cents:
        reasons.append("no_spread_too_wide_or_unknown")
    yes_depth = _num(quotes.get("yes_ask_depth_contracts"))
    no_depth = _num(quotes.get("no_ask_depth_contracts"))
    if yes_depth is None or yes_depth < min_depth_contracts:
        reasons.append("insufficient_yes_ask_depth")
    if no_depth is None or no_depth < min_depth_contracts:
        reasons.append("insufficient_no_ask_depth")
    model_ok, model_reasons, model_snapshot = validate_model_evidence(
        model, category, min_validation_samples=min_validation_samples, max_brier_score=max_brier_score,
    )
    # Enforce exact model-to-contract binding here as well as in the HTTP adapter;
    # this protects offline integrations, alternate adapters, and future refactors.
    if not isinstance(model, Mapping) or model.get("market_ticker") != ticker:
        model_ok = False
        model_reasons.append("model_ticker_mismatch")
    reasons.extend(model_reasons)

    p_yes = _num(model_snapshot.get("model_probability_yes")) if model_ok else None
    yes_edge = no_edge = None
    side = None
    chosen_ask = edge_net = None
    if p_yes is not None and yes_ask is not None and no_ask is not None:
        yes_edge = 100.0 * p_yes - yes_ask - estimated_fee_cents - slippage_cents
        no_edge = 100.0 * (1.0 - p_yes) - no_ask - estimated_fee_cents - slippage_cents
        offers = [("YES", yes_ask, yes_edge), ("NO", no_ask, no_edge)]
        side, chosen_ask, edge_net = max(offers, key=lambda x: x[2])
        if chosen_ask > max_entry_cents:
            reasons.append("entry_price_above_cap")
        if edge_net < min_net_edge_cents:
            reasons.append("net_edge_below_minimum")
    else:
        reasons.append("net_edge_not_calculable")

    # A calibrated model can generate a paper opportunity only if all source,
    # price, depth and open-status checks pass. Registry readiness isn't silently
    # promoted; per-category paper opt-in is enforced by the orchestrator.
    go = not reasons and side in {"YES", "NO"}
    return {
        "ticker": str(ticker).strip() if isinstance(ticker, str) else None,
        "title": str(market.get("title") or market.get("subtitle") or "")[:300],
        "category": category,
        "route_status": routed.get("route_status"),
        "status": "paper_candidate" if go else "blocked",
        "eligible": go,
        "trade_submitted": False,
        "selected_side": side if go else None,
        "model_probability_yes_pct": round(p_yes * 100, 3) if p_yes is not None else None,
        "market_implied_yes_probability_pct": round((100 + (_num(quotes.get("yes_bid_cents")) or 0) - (_num(quotes.get("no_bid_cents")) or 0)) / 2, 3) if quotes.get("yes_bid_cents") is not None and quotes.get("no_bid_cents") is not None else None,
        "yes_ask_cents": yes_ask, "no_ask_cents": no_ask,
        "yes_spread_cents": yes_spread, "no_spread_cents": no_spread,
        "yes_ask_depth_contracts": yes_depth, "no_ask_depth_contracts": no_depth,
        "volume_24h": volume,
        "estimated_fee_cents": estimated_fee_cents,
        "estimated_slippage_cents": slippage_cents,
        "yes_net_edge_cents": round(yes_edge, 3) if yes_edge is not None else None,
        "no_net_edge_cents": round(no_edge, 3) if no_edge is not None else None,
        "expected_net_edge_cents": round(edge_net, 3) if edge_net is not None else None,
        "entry_ask_cents": chosen_ask if go else None,
        "model": model_snapshot,
        "reasons": reasons,
        "rationale": str(model.get("rationale") or "")[:1200] if isinstance(model, Mapping) else "",
    }
