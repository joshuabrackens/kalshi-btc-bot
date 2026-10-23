"""TradingView-primary rule-based signal fusion for the Kalshi BTC 15m PAPER bot.

This module does not estimate win probability and never places orders. In hybrid
mode, an external alert proposes the direction; internal evidence may veto only
clear, pre-defined major contradictions. Ordinary low internal scores or WAITs
are not automatic vetoes, matching the documented TradingView-primary policy.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Mapping
import math


@dataclass
class FusionDecision:
    approved: bool
    direction: str
    reasons: list[str]
    warnings: list[str]
    alert_age_seconds: float | None
    entry_ask_cents: float | None
    max_entry_cents: float
    spread_cents: float | None
    vetoed_by_internal: bool = False
    disclaimer: str = (
        "TradingView-primary paper candidate; internal rules veto only major conflicts. "
        "No probability calibration or profitability guarantee is implied."
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def major_flaw_veto(direction: str, internal_payload: Mapping[str, Any], minimum_opposing_points: int = 4) -> tuple[bool, list[str]]:
    """Veto only strong conflicting trend/evidence or a confirmed opposite breakout."""
    wanted = 1 if str(direction).upper() == "UP" else -1
    strategies = internal_payload.get("strategies") or {}
    reasons: list[str] = []
    trends = (strategies.get("multi_timeframe_trend") or {}).get("timeframes") or {}
    trend_5m, trend_15m = trends.get("5m"), trends.get("15m")
    totals = strategies.get("evidence_totals") or {}
    up_points, down_points = int(totals.get("up_points", 0) or 0), int(totals.get("down_points", 0) or 0)
    wanted_points = up_points if wanted > 0 else down_points
    opposing_points = down_points if wanted > 0 else up_points
    margin = opposing_points - wanted_points
    if trend_5m == -wanted and trend_15m == -wanted and margin >= max(2, int(minimum_opposing_points)):
        opposing_text = "bearish" if wanted > 0 else "bullish"
        reasons.append(
            f"Major conflict: both 5m and 15m trends are {opposing_text}, and opposing evidence leads by {margin} points."
        )
    box = strategies.get("box_theory") or {}
    structure = strategies.get("market_structure") or {}
    volume = strategies.get("volume_confirmation") or {}
    if (box.get("direction") == -wanted
            and box.get("state") in {"bullish_breakout", "bearish_breakout"}
            and bool(volume.get("confirmed"))
            and structure.get("direction") == -wanted):
        breakout = "bearish" if wanted > 0 else "bullish"
        reasons.append(f"Major conflict: a volume-confirmed {breakout} breakout agrees with opposing market structure.")
    return bool(reasons), reasons


def _trend_monitor_gate(
    direction: str,
    snapshot: Mapping[str, Any],
    *,
    now: datetime,
    max_age_seconds: float,
    minimum_score: int,
    minimum_margin: int,
) -> tuple[bool, str, bool]:
    """Validate a fresh chart snapshot and veto only strong opposite alignment.

    Returns (passed, audit_reason, was_directional_veto). It does not calculate
    or imply a probability of settlement.
    """
    status = str(snapshot.get("status") or "").strip().lower()
    if status != "ok":
        return False, f"BTC trend monitor is not healthy (status={status or 'missing'}); hybrid entry blocked.", False
    updated = _parse_dt(snapshot.get("updated_at"))
    if updated is None:
        return False, "BTC trend monitor timestamp is missing/invalid; hybrid entry blocked.", False
    age = (now.astimezone(timezone.utc) - updated).total_seconds()
    if age < -10 or age > max(15.0, float(max_age_seconds)):
        return False, f"BTC trend monitor is stale/invalid ({age:.1f}s old; limit {max_age_seconds:.1f}s); hybrid entry blocked.", False

    bias = str(snapshot.get("candidate_bias") or "MIXED").strip().upper()
    if bias not in {"UP", "DOWN", "MIXED"}:
        return False, f"BTC trend monitor bias is invalid ({bias!r}); hybrid entry blocked.", False
    points = snapshot.get("evidence_points") or {}
    try:
        up_points = int(points.get("up", 0) or 0)
        down_points = int(points.get("down", 0) or 0)
        score = int(float(snapshot.get("evidence_score", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        return False, "BTC trend monitor evidence is invalid; hybrid entry blocked.", False
    if not (0 <= score <= 100 and 0 <= up_points <= 100 and 0 <= down_points <= 100):
        return False, "BTC trend monitor evidence is outside expected bounds; hybrid entry blocked.", False

    timeframes = snapshot.get("timeframes") or {}
    if not isinstance(timeframes, Mapping):
        return False, "BTC trend monitor timeframe data is invalid; hybrid entry blocked.", False
    opposite_bias = "DOWN" if direction == "UP" else "UP"
    opposite_label = "bearish" if opposite_bias == "DOWN" else "bullish"
    opposite_tf_count = sum(
        1 for tf in ("1m", "5m", "15m")
        if str(timeframes.get(tf, "")).strip().lower() == opposite_label
    )
    margin = (down_points - up_points) if direction == "UP" else (up_points - down_points)

    if (bias == opposite_bias and score >= max(0, int(minimum_score))
            and margin >= max(1, int(minimum_margin)) and opposite_tf_count >= 2):
        return False, (
            f"BTC trend monitor veto: strong {bias} chart bias conflicts with {direction} alert "
            f"(evidence {score}/100, opposing point margin {margin}, "
            f"{opposite_tf_count}/3 timeframes {opposite_label})."
        ), True

    if bias == opposite_bias:
        return True, (
            f"BTC trend monitor shows {bias}, opposite to {direction}, but the conflict is below veto "
            f"threshold (evidence {score}/100, point margin {margin}, "
            f"{opposite_tf_count}/3 opposing timeframes); alert not vetoed by trend alone."
        ), False
    if bias == "MIXED":
        return True, f"BTC trend monitor is mixed (evidence {score}/100); no directional trend veto applied.", False
    return True, f"BTC trend monitor aligns with {direction} (bias {bias}, evidence {score}/100, snapshot age {age:.1f}s).", False


def evaluate_fusion(
    *,
    direction: str,
    alert_created_at: str,
    symbol: str | None,
    timeframe: str | None,
    internal_payload: Mapping[str, Any],
    market_ticker: str,
    seconds_remaining: int,
    up_ask_cents: float | None,
    down_ask_cents: float | None,
    spread_cents: float | None,
    fee_cents_per_contract: float,
    max_entry_cents: float = 78.0,
    max_spread_cents: float = 5.0,
    max_alert_age_seconds: int = 300,
    min_seconds_remaining: int = 30,
    max_seconds_remaining: int = 900,
    minimum_opposing_points: int = 4,
    trend_snapshot: Mapping[str, Any] | None = None,
    max_trend_snapshot_age_seconds: float = 45.0,
    require_trend_snapshot: bool = False,
    minimum_trend_conflict_score: int = 65,
    minimum_trend_conflict_margin: int = 4,
    now: datetime | None = None,
) -> FusionDecision:
    """Return a transparent accept/reject decision for a paper entry candidate."""
    reasons: list[str] = []
    warnings: list[str] = []
    side = str(direction or "").strip().upper()
    if side not in {"UP", "DOWN"}:
        return FusionDecision(False, "WAIT", ["External alert direction is missing or unsupported."], warnings, None,
                              None, max_entry_cents, spread_cents)

    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    created = _parse_dt(alert_created_at)
    age = (now_utc.astimezone(timezone.utc) - created).total_seconds() if created else None
    ask = up_ask_cents if side == "UP" else down_ask_cents

    def reject(reason: str, veto: bool = False) -> FusionDecision:
        return FusionDecision(False, "WAIT", reasons + [reason], warnings.copy(),
                              round(age, 2) if age is not None else None,
                              float(ask) if _finite(ask) else None, float(max_entry_cents),
                              float(spread_cents) if _finite(spread_cents) else None, veto)

    reasons.append(f"TradingView/GainzAlgo proposes {side}.")
    if not created or age is None:
        return reject("Alert timestamp is missing, invalid, or lacks a timezone.")
    if age < -10:
        return reject("Alert timestamp is too far in the future.")
    if age > max_alert_age_seconds:
        return reject(f"Alert is stale ({age:.1f}s old; limit {max_alert_age_seconds}s).")
    reasons.append(f"Alert freshness passed ({max(0.0, age):.1f}s old).")

    sym = str(symbol or "").upper()
    if not any(token in sym for token in ("BTC", "BITCOIN")):
        return reject("Alert symbol is missing or does not identify BTC/Bitcoin.")
    tf = str(timeframe or "").strip().lower().replace(" ", "")
    if tf and tf not in {"1", "1m", "1min", "1minute", "1minutes", "15", "15m", "15min", "15minute", "15minutes"}:
        return reject(f"Alert timeframe {tf!r} is unsupported; use 1m or 15m alerts.")
    if not tf:
        warnings.append("Alert timeframe was not supplied; verify TradingView alert configuration.")
    if not market_ticker or not str(market_ticker).upper().startswith("KXBTC15M-"):
        return reject("No validated BTC 15-minute market ticker was supplied.")
    if not min_seconds_remaining <= int(seconds_remaining) <= max_seconds_remaining:
        return reject("Time remaining is outside the allowed 15-minute entry window.")
    if not _finite(spread_cents) or float(spread_cents) < 0 or float(spread_cents) > max_spread_cents:
        return reject(f"Spread is missing/invalid or exceeds {max_spread_cents:.2f}¢.")
    if not _finite(fee_cents_per_contract) or float(fee_cents_per_contract) < 0:
        return reject("Estimated contract fee is invalid.")
    if not _finite(ask) or not 0 < float(ask) < 100:
        return reject("The alerted side has no valid ask price.")
    if float(ask) > max_entry_cents:
        return reject(f"Contract ask {float(ask):.2f}¢ exceeds the configured {max_entry_cents:.2f}¢ cap.")
    if float(ask) + float(fee_cents_per_contract) >= 100:
        return reject("Ask plus estimated entry fee leaves no payout margin before exit costs.")

    # In hybrid mode the continuously refreshed chart snapshot is now a separate
    # check, not merely a dashboard metric. Missing/stale monitor data fails closed
    # when the caller requires the monitor. A strong opposing bias can veto; a weak
    # or mixed bias is recorded but does not override the external indicator.
    if trend_snapshot is None:
        if require_trend_snapshot:
            return reject("BTC trend monitor snapshot is missing; hybrid entry blocked until fresh chart analysis is available.")
        reasons.append("No separate BTC trend snapshot supplied; using the internal fusion checks only.")
    else:
        monitor_ok, monitor_reason, monitor_veto = _trend_monitor_gate(
            side,
            trend_snapshot,
            now=now_utc,
            max_age_seconds=max_trend_snapshot_age_seconds,
            minimum_score=minimum_trend_conflict_score,
            minimum_margin=minimum_trend_conflict_margin,
        )
        if not monitor_ok:
            return reject(monitor_reason, veto=monitor_veto)
        reasons.append(monitor_reason)

    vetoed, veto_reasons = major_flaw_veto(side, internal_payload, minimum_opposing_points)
    if vetoed:
        return reject("Internal system vetoed a major contradiction: " + "; ".join(veto_reasons), veto=True)
    reasons.append("Internal analysis found no configured major-flaw veto; ordinary WAIT/low score is not itself a veto in hybrid mode.")
    warnings.append("Hybrid approval is not an expected-value estimate; the internal score is not a win probability.")
    return FusionDecision(True, "BUY_UP" if side == "UP" else "BUY_DOWN", reasons, warnings,
                          round(age, 2), float(ask), float(max_entry_cents), float(spread_cents), False)
