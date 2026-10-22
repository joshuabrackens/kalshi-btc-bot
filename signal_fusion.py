"""Conservative signal fusion for a Kalshi BTC 15-minute PAPER bot.

Offline only: no HTTP clients, credentials, order placement, or market-data fetching.

Purpose:
- Treat TradingView/GainzAlgo as a trade *candidate*, not an instruction.
- Require an independently computed internal signal to agree.
- Reject stale/untimestamped/mismatched alerts and poor contract conditions.
- Return an explainable paper-only decision. This is NOT a calibrated probability
  model and does NOT establish positive expected value.

Integration: call fuse_signals(...) from paper_worker.py after it has parsed an
alert, run evaluate_signal(...), and fetched/supplied the contract market context.
The function is intentionally stateless; the caller must deduplicate alert IDs.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from hashlib import sha256
import math
from typing import Any, Mapping, Optional, Literal

Direction = Literal["BUY_UP", "BUY_DOWN", "WAIT"]


@dataclass(frozen=True)
class FusionConfig:
    minimum_internal_score: int = 55
    max_alert_age_seconds: int = 90
    max_future_skew_seconds: int = 5
    min_seconds_remaining: int = 30
    max_seconds_remaining: int = 900
    max_entry_cents: float = 78.0
    max_spread_cents: float = 5.0
    max_fee_cents_per_contract: float = 5.0
    allowed_timeframes: tuple[str, ...] = ("1", "1M", "5", "5M", "15", "15M", "1MIN", "5MIN", "15MIN")
    require_btc_symbol: bool = True

    def validate(self) -> None:
        if not 0 <= self.minimum_internal_score <= 100:
            raise ValueError("minimum_internal_score must be in [0, 100].")
        if self.max_alert_age_seconds < 0 or self.max_future_skew_seconds < 0:
            raise ValueError("Alert age/skew settings must be non-negative.")
        if not 0 <= self.min_seconds_remaining <= self.max_seconds_remaining:
            raise ValueError("Invalid seconds-remaining bounds.")
        if not math.isfinite(self.max_entry_cents) or not 0 < self.max_entry_cents < 100:
            raise ValueError("max_entry_cents must be between 0 and 100, exclusive.")
        if not math.isfinite(self.max_spread_cents) or self.max_spread_cents < 0:
            raise ValueError("max_spread_cents must be finite and non-negative.")
        if not math.isfinite(self.max_fee_cents_per_contract) or self.max_fee_cents_per_contract < 0:
            raise ValueError("max_fee_cents_per_contract must be finite and non-negative.")


@dataclass
class FusionDecision:
    direction: Direction
    approved_for_paper_candidate: bool
    reasons: list[str]
    warnings: list[str]
    alert_direction: Direction
    internal_direction: Direction
    internal_score: Optional[int]
    market_ticker: Optional[str]
    entry_ask_cents: Optional[float]
    max_entry_cents: float
    spread_cents: Optional[float]
    fee_cents_per_contract: float
    seconds_remaining: Optional[int]
    alert_age_seconds: Optional[float]
    alert_fingerprint: Optional[str]
    disclaimer: str = (
        "Rule-based paper candidate only. The score is not a win probability; "
        "agreement does not prove positive expected value or profitability."
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _direction(value: Any) -> Direction:
    raw = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if raw in {"BUY_UP", "UP", "LONG", "BULL", "BULLISH", "CALL"}:
        return "BUY_UP"
    if raw in {"BUY_DOWN", "DOWN", "SHORT", "BEAR", "BEARISH", "PUT"}:
        return "BUY_DOWN"
    return "WAIT"


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    # Reject naive timestamps rather than silently assuming a timezone.
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _alert_fingerprint(alert: Mapping[str, Any]) -> str:
    # Exclude secrets and arbitrary payloads; stable fields only.
    stable = "|".join(str(alert.get(k, "")) for k in
                      ("source", "symbol", "timeframe", "direction", "action", "signal", "timestamp", "created_at", "id"))
    return sha256(stable.encode("utf-8", errors="replace")).hexdigest()[:24]


def fuse_signals(
    alert: Mapping[str, Any],
    internal_signal: Mapping[str, Any],
    market: Mapping[str, Any],
    *,
    config: FusionConfig = FusionConfig(),
    now: Optional[datetime] = None,
) -> FusionDecision:
    """Combine one external alert, one internal signal and one market snapshot.

    Expected alert fields: direction/action/signal, timestamp or created_at,
    optional symbol/timeframe/source. Expected internal fields: direction, score.
    Expected market fields: market_ticker, seconds_remaining, up_ask_cents,
    down_ask_cents, spread_cents, estimated_fee_cents.

    Missing critical evidence causes WAIT. This function does not estimate win
    probability and intentionally does not auto-size a position.
    """
    config.validate()
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    now_utc = now_utc.astimezone(timezone.utc)
    reasons: list[str] = []
    warnings: list[str] = []

    alert_dir = _direction(alert.get("direction") or alert.get("action") or alert.get("signal"))
    internal_dir = _direction(internal_signal.get("direction"))
    score_raw = internal_signal.get("score")
    score: Optional[int] = int(score_raw) if _finite(score_raw) else None
    if score is not None and not 0 <= score <= 100:
        score = None

    ticker = str(market.get("market_ticker") or "").strip() or None
    seconds_raw = market.get("seconds_remaining")
    seconds = int(seconds_raw) if _finite(seconds_raw) else None
    fee_raw = market.get("estimated_fee_cents", 0.0)
    fee = float(fee_raw) if _finite(fee_raw) else float("nan")
    spread_raw = market.get("spread_cents")
    spread = float(spread_raw) if _finite(spread_raw) else None
    alert_time = _parse_timestamp(alert.get("timestamp") or alert.get("created_at"))
    age: Optional[float] = (now_utc - alert_time).total_seconds() if alert_time else None
    fingerprint = _alert_fingerprint(alert)

    def decision(ok: bool = False, direction: Direction = "WAIT") -> FusionDecision:
        # Even on a veto, report the ask for the alerted side so diagnostics
        # never accidentally display the opposite contract's price.
        reported_direction = direction if direction != "WAIT" else alert_dir
        side = "up" if reported_direction == "BUY_UP" else "down"
        ask_key = f"{side}_ask_cents"
        ask_raw = market.get(ask_key)
        ask = float(ask_raw) if _finite(ask_raw) else None
        return FusionDecision(
            direction=direction if ok else "WAIT",
            approved_for_paper_candidate=bool(ok), reasons=reasons.copy(), warnings=warnings.copy(),
            alert_direction=alert_dir, internal_direction=internal_dir, internal_score=score,
            market_ticker=ticker, entry_ask_cents=ask, max_entry_cents=config.max_entry_cents,
            spread_cents=spread, fee_cents_per_contract=fee if math.isfinite(fee) else 0.0,
            seconds_remaining=seconds, alert_age_seconds=round(age, 3) if age is not None else None,
            alert_fingerprint=fingerprint,
        )

    if alert_dir == "WAIT":
        reasons.append("External alert direction is missing or unsupported.")
        return decision()
    reasons.append(f"External alert proposes {alert_dir}.")

    if alert_time is None:
        reasons.append("Alert timestamp is missing, invalid, or lacks a timezone; freshness cannot be verified.")
        return decision()
    if age is not None and age < -config.max_future_skew_seconds:
        reasons.append("Alert timestamp is too far in the future.")
        return decision()
    if age is not None and age > config.max_alert_age_seconds:
        reasons.append(f"Alert is stale ({age:.1f}s old; limit {config.max_alert_age_seconds}s).")
        return decision()
    reasons.append(f"Alert freshness passed ({max(0.0, age or 0.0):.1f}s old).")

    symbol = str(alert.get("symbol") or "").strip().upper()
    if config.require_btc_symbol and (not symbol or not any(token in symbol for token in ("BTC", "BITCOIN"))):
        reasons.append("Alert symbol is missing or not clearly a BTC/Bitcoin symbol.")
        return decision()
    timeframe = str(alert.get("timeframe") or "").strip().upper().replace(" ", "")
    if timeframe and timeframe not in {v.upper() for v in config.allowed_timeframes}:
        reasons.append(f"Alert timeframe {timeframe!r} is not allowed.")
        return decision()
    if not timeframe:
        warnings.append("Alert timeframe was not supplied; verify the alert configuration.")

    if internal_dir != alert_dir:
        reasons.append(f"Signal disagreement: external={alert_dir}, internal={internal_dir}; vetoed.")
        return decision()
    reasons.append("External and internal directions agree.")
    if score is None:
        reasons.append("Internal evidence score is missing or invalid.")
        return decision()
    if score < config.minimum_internal_score:
        reasons.append(f"Internal score {score}/100 is below the {config.minimum_internal_score} threshold.")
        return decision()
    reasons.append(f"Internal score passed ({score}/100; heuristic only).")

    if seconds is None or not config.min_seconds_remaining <= seconds <= config.max_seconds_remaining:
        reasons.append("Time remaining is missing or outside the allowed market window.")
        return decision()
    if not ticker:
        reasons.append("Market ticker is missing.")
        return decision()
    if spread is None:
        reasons.append("Spread is missing or invalid; liquidity filter cannot be checked.")
        return decision()
    if spread < 0 or spread > config.max_spread_cents:
        reasons.append(f"Spread {spread:.2f}¢ exceeds the allowed {config.max_spread_cents:.2f}¢ or is invalid.")
        return decision()
    if not math.isfinite(fee) or fee < 0 or fee > config.max_fee_cents_per_contract:
        reasons.append("Estimated fee is missing, invalid, or above the configured cap.")
        return decision()

    side = "up" if alert_dir == "BUY_UP" else "down"
    ask_raw = market.get(f"{side}_ask_cents")
    if not _finite(ask_raw):
        reasons.append(f"{side.upper()} ask is missing or invalid.")
        return decision()
    ask = float(ask_raw)
    if not 0 < ask < 100:
        reasons.append("Contract ask must be strictly between 0¢ and 100¢.")
        return decision()
    if ask > config.max_entry_cents:
        reasons.append(f"Contract ask {ask:.2f}¢ exceeds cap {config.max_entry_cents:.2f}¢.")
        return decision()
    if ask + fee >= 100:
        reasons.append("Ask plus estimated fee leaves no gross payout margin before other costs/errors.")
        return decision()

    warnings.append("This is a paper candidate, not an estimated positive-EV trade; no calibrated probability model is present.")
    reasons.append("All configured fusion gates passed; eligible for paper evaluation only.")
    return decision(True, alert_dir)
