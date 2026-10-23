"""Conservative calendar and chart-regime labels for the BTC paper bot.

These labels organize historical observations. They do not forecast outcomes,
change signal weights, approve entries, or override execution/risk safeguards.
All calendar buckets use UTC so live snapshots and stored trade times are
interpretable and consistent across deployment hosts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping


def parse_utc_datetime(value: Any) -> datetime | None:
    """Parse ISO timestamps or epoch timestamps into aware UTC datetimes."""
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            stamp = float(value)
            if not isfinite(stamp):
                return None
            if stamp > 10_000_000_000:  # milliseconds
                stamp /= 1000.0
            parsed = datetime.fromtimestamp(stamp, tz=timezone.utc)
        elif isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def utc_calendar_context(value: Any) -> dict[str, Any]:
    """Return transparent weekday/weekend and six-hour time buckets in UTC."""
    stamp = parse_utc_datetime(value)
    if stamp is None:
        return {
            "calendar_context_timezone": "UTC",
            "day_type_utc": "unknown",
            "weekday_utc": "unknown",
            "utc_time_bucket": "unknown",
            "utc_hour": None,
        }
    hour = stamp.hour
    bucket_start = (hour // 6) * 6
    bucket_end = bucket_start + 5
    return {
        "calendar_context_timezone": "UTC",
        "day_type_utc": "weekend" if stamp.weekday() >= 5 else "weekday",
        "weekday_utc": stamp.strftime("%A"),
        "utc_time_bucket": f"{bucket_start:02d}:00–{bucket_end:02d}:59 UTC",
        "utc_hour": hour,
    }


def _direction(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
            if not isfinite(number):
                return None
            return "UP" if number > 0 else "DOWN" if number < 0 else "MIXED"
        except (TypeError, ValueError, OverflowError):
            return None
    text = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if text in {"UP", "BUY_UP", "BULL", "BULLISH", "LONG", "1", "+1"}:
        return "UP"
    if text in {"DOWN", "BUY_DOWN", "BEAR", "BEARISH", "SHORT", "-1"}:
        return "DOWN"
    if text in {"MIXED", "RANGE", "RANGING", "NEUTRAL", "0"}:
        return "MIXED"
    return None


def classify_market_regime(
    timeframes: Mapping[str, Any] | None,
    market_structure: Mapping[str, Any] | None,
    box_theory: Mapping[str, Any] | None,
    *,
    chase_warning: Any = None,
    three_candle_abs_move_pct: Any = None,
) -> dict[str, str]:
    """Label observed chart context without inventing a probability or trade.

    The volatility label reflects the signal engine's existing chase-warning
    boolean; it is not a claim that a statistically calibrated volatility
    regime has been detected. Missing context is deliberately kept unknown.
    """
    timeframes = timeframes if isinstance(timeframes, Mapping) else {}
    structure = market_structure if isinstance(market_structure, Mapping) else {}
    box = box_theory if isinstance(box_theory, Mapping) else {}
    known_inputs = bool(timeframes or structure or box)
    frame_directions = [
        _direction(timeframes.get(tf))
        for tf in ("1m", "5m", "15m")
        if timeframes.get(tf) is not None
    ]
    directional = [direction for direction in frame_directions if direction in {"UP", "DOWN"}]
    up_count = directional.count("UP")
    down_count = directional.count("DOWN")
    majority = "UP" if up_count >= 2 else "DOWN" if down_count >= 2 else None
    structure_direction = _direction(structure.get("direction"))
    box_direction = _direction(box.get("direction"))
    box_state = str(box.get("state") or "").strip().lower()
    structure_label = str(structure.get("label") or "").strip().lower()
    is_breakout = box_state in {"bullish_breakout", "bearish_breakout"}

    if not known_inputs:
        trend_regime = "unknown"
    elif majority:
        if structure_direction in {"UP", "DOWN"} and structure_direction != majority:
            trend_regime = "conflicted"
        elif is_breakout and box_direction in {"UP", "DOWN"} and box_direction != majority:
            trend_regime = "conflicted"
        elif is_breakout and box_direction == majority:
            trend_regime = "breakout_trend"
        elif structure_direction == majority:
            trend_regime = "aligned_trend"
        else:
            trend_regime = "directional_trend"
    elif (
        box_state in {"inside_box", "wick_break_or_rejection"}
        or "range" in structure_label
        or "mixed" in structure_label
        or any(direction == "MIXED" for direction in frame_directions)
    ):
        trend_regime = "range_or_consolidation"
    elif len(directional) >= 2 and up_count and down_count:
        trend_regime = "mixed_or_unclear"
    else:
        trend_regime = "mixed_or_unclear" if known_inputs else "unknown"

    if isinstance(chase_warning, bool):
        volatility_regime = "elevated_warning" if chase_warning else "not_flagged"
    else:
        # A raw move without a verified baseline is not enough to label its
        # volatility as high or low, so do not create a synthetic bucket.
        volatility_regime = "unknown"

    return {
        "trend_regime": trend_regime,
        "volatility_regime": volatility_regime,
        "regime_method_note": (
            "Descriptive labels from saved multi-timeframe trend, structure, box, and existing chase-warning evidence; "
            "not a calibrated forecast and not an automatic strategy/weight change."
        ),
    }
