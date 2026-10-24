"""Fail-closed registry for routing markets into the original BTC strategy.

Only BTC 15-minute is enabled for the original signal/worker path. A separate
market_decision_pipeline can optionally record isolated research-only paper simulations
when a category-specific external model/evidence provider passes its own gates. Neither
module places live orders.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CategoryProfile:
    key: str
    label: str
    paper_strategy_enabled: bool
    market_identifiers: tuple[str, ...]
    required_evidence: tuple[str, ...]
    validation_gaps: tuple[str, ...]
    notes: str


_PROFILES: dict[str, CategoryProfile] = {
    "btc_15m": CategoryProfile(
        key="btc_15m", label="BTC 15-minute up/down", paper_strategy_enabled=True,
        market_identifiers=("KXBTC15M-",),
        required_evidence=("Binance closed 1m/5m/15m candles", "validated Kalshi order book", "fresh TradingView/GainzAlgo alert", "official contract settlement"),
        validation_gaps=("Continue out-of-sample paper evaluation", "Keep score/probability calibration separate"),
        notes="Existing BTC-only paper pipeline. The normal entry, freshness, spread, and risk gates still apply.",
    ),
    "sports": CategoryProfile(
        key="sports", label="Sports", paper_strategy_enabled=False, market_identifiers=(),
        required_evidence=("sport/event identity", "current lineups and injury reports", "time-stamped player/team statistics", "market-specific settlement rules"),
        validation_gaps=("Category-specific probability model", "Source reliability and stale-news handling", "Backtest and calibration"),
        notes="Research-only profile; never feed this category to the BTC signal engine.",
    ),
    "politics": CategoryProfile(
        key="politics", label="Politics and elections", paper_strategy_enabled=False, market_identifiers=(),
        required_evidence=("exact question and resolution criteria", "dated polling or official results", "source provenance", "market close and settlement rules"),
        validation_gaps=("Event-specific model", "Polling bias and correlated-source treatment", "Historical out-of-sample calibration"),
        notes="Research-only profile; no political-market trades are enabled.",
    ),
    "economics": CategoryProfile(
        key="economics", label="Economic releases and indicators", paper_strategy_enabled=False, market_identifiers=(),
        required_evidence=("official release calendar", "consensus definition", "revision policy", "official release and threshold semantics"),
        validation_gaps=("Release-specific forecasting model", "Revision and release-time handling", "Historical calibration"),
        notes="Research-only profile; official source and settlement definition must match exactly.",
    ),
    "weather": CategoryProfile(
        key="weather", label="Weather and climate", paper_strategy_enabled=False, market_identifiers=(),
        required_evidence=("target location and measurement station", "official weather observations/forecast", "time horizon", "resolution measurement and unit"),
        validation_gaps=("Location-specific forecast model", "Station differences and forecast calibration", "Historical validation"),
        notes="Research-only profile; location and measurement definitions are mandatory.",
    ),
    "crypto_event": CategoryProfile(
        key="crypto_event", label="Crypto event markets", paper_strategy_enabled=False, market_identifiers=(),
        required_evidence=("exact event/threshold", "timestamped exchange/index source", "resolution oracle", "market settlement criteria"),
        validation_gaps=("Event-specific model", "Exchange/oracle discrepancy handling", "Out-of-sample calibration"),
        notes="Research-only profile; BTC 15-minute rules do not transfer to crypto event contracts.",
    ),
    "financial_markets": CategoryProfile(
        key="financial_markets", label="Other financial markets", paper_strategy_enabled=False, market_identifiers=(),
        required_evidence=("instrument and price source", "time horizon", "corporate-action/roll rules", "contract-specific settlement criteria"),
        validation_gaps=("Instrument-specific model", "Market-hours and basis handling", "Historical calibration"),
        notes="Research-only profile; requires a separate model for every instrument family.",
    ),
    "entertainment": CategoryProfile(
        key="entertainment", label="Entertainment and culture", paper_strategy_enabled=False, market_identifiers=(),
        required_evidence=("precise event definition", "reliable primary sources", "deadline and resolution criteria"),
        validation_gaps=("Domain-specific evidence model", "Rumor/source reliability scoring", "Historical validation"),
        notes="Research-only profile; no BTC logic is reused.",
    ),
}

_ALIASES = {
    "btc": "btc_15m", "bitcoin": "btc_15m", "crypto": "crypto_event",
    "sports": "sports", "sport": "sports", "politics": "politics", "elections": "politics",
    "economics": "economics", "economic": "economics", "weather": "weather", "climate": "weather",
    "financial": "financial_markets", "finance": "financial_markets", "financial_markets": "financial_markets",
    "entertainment": "entertainment", "culture": "entertainment",
}


def category_profiles() -> list[dict[str, Any]]:
    """Return profile metadata for diagnostics/UI; non-BTC profiles remain disabled."""
    results = []
    for profile in _PROFILES.values():
        item = asdict(profile)
        item["market_identifiers"] = list(profile.market_identifiers)
        item["required_evidence"] = list(profile.required_evidence)
        item["validation_gaps"] = list(profile.validation_gaps)
        item["status"] = "paper_enabled" if profile.paper_strategy_enabled else "research_only"
        results.append(item)
    return results


def classify_market(ticker: str | None, title: str | None = None, explicit_category: str | None = None) -> dict[str, Any]:
    """Classify conservatively; unknown markets are never routed to a trading strategy."""
    # These values originate in public metadata; accept only string fields and
    # bound work before normalization. Unexpected object types stay unclassified.
    ticker_text = ticker[:120].strip().upper() if isinstance(ticker, str) else ""
    title_text = title[:600].strip().lower() if isinstance(title, str) else ""
    key = None
    if ticker_text.startswith("KXBTC15M-") and ("btc" in title_text or "bitcoin" in title_text):
        key = "btc_15m"
    elif explicit_category:
        category_text = explicit_category[:80].strip().lower().replace(" ", "_") if isinstance(explicit_category, str) else ""
        key = _ALIASES.get(category_text)
        if key == "btc_15m":
            # Do not allow a caller-provided category label to bypass BTC ticker/title validation.
            key = None
    if key is None or key not in _PROFILES:
        return {
            "category": "unclassified", "label": "Unclassified market", "status": "blocked_unclassified",
            "paper_strategy_enabled": False, "market_ticker": ticker_text or None,
            "reason": "No validated category identifier matched. Do not route this market to a strategy.",
        }
    profile = _PROFILES[key]
    return {
        "category": profile.key, "label": profile.label,
        "status": "paper_enabled" if profile.paper_strategy_enabled else "research_only",
        "paper_strategy_enabled": profile.paper_strategy_enabled,
        "market_ticker": ticker_text or None,
        "reason": profile.notes if profile.paper_strategy_enabled else "Category-specific model and validation requirements are not complete; no paper entries allowed.",
    }
