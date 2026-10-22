"""Offline BTC-chart trailing-stop calculations for paper-trading only.

This module calculates a monotonic ATR + confirmed-swing trailing stop from
fully closed candles. It never contacts Kalshi or submits orders.
"""
from __future__ import annotations

from statistics import mean
from typing import Any

import pandas as pd

_REQUIRED = {"open", "high", "low", "close", "volume"}


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    highs = [float(v) for v in df["high"]]
    lows = [float(v) for v in df["low"]]
    closes = [float(v) for v in df["close"]]
    true_ranges: list[float] = []
    for i in range(1, len(df)):
        true_ranges.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    selected = true_ranges[-period:]
    if len(selected) < period:
        raise ValueError(f"Need at least {period + 1} candles to calculate ATR")
    value = mean(selected)
    if value <= 0:
        raise ValueError("ATR must be positive")
    return value


def _confirmed_swings(df: pd.DataFrame, lookback: int, pivot_width: int = 2) -> tuple[float | None, float | None]:
    """Return latest confirmed pivot low/high within lookback candles.

    A pivot needs two later candles to confirm it, avoiding use of a still-forming
    pivot. The caller should pass closed candles ordered oldest to newest.
    """
    start = max(pivot_width, len(df) - max(lookback, 2 * pivot_width + 1))
    last_center = len(df) - pivot_width - 1
    lows = [float(v) for v in df["low"]]
    highs = [float(v) for v in df["high"]]
    pivot_low = None
    pivot_high = None
    for i in range(start, last_center + 1):
        window_low = lows[i - pivot_width : i + pivot_width + 1]
        window_high = highs[i - pivot_width : i + pivot_width + 1]
        if lows[i] == min(window_low) and lows[i] < min(lows[i - pivot_width : i]) and lows[i] <= min(lows[i + 1 : i + pivot_width + 1]):
            pivot_low = lows[i]
        if highs[i] == max(window_high) and highs[i] > max(highs[i - pivot_width : i]) and highs[i] >= max(highs[i + 1 : i + pivot_width + 1]):
            pivot_high = highs[i]
    return pivot_low, pivot_high


def update_trend_stop(
    candles: pd.DataFrame,
    direction: str,
    high_water_btc: float | None = None,
    low_water_btc: float | None = None,
    trailing_stop_btc: float | None = None,
    *,
    atr_multiplier: float = 2.5,
    swing_lookback: int = 30,
) -> dict[str, Any]:
    """Update chart-based stop for a Kalshi BTC UP or DOWN paper position.

    UP positions use the greater of (a) the previous stop, (b) highest close
    minus ATR multiple, and (c) the latest confirmed swing low. DOWN positions
    use the symmetric minimum. Stops only ratchet in the position's favor.
    Triggering is based on the latest *closed candle close*, not an intrabar wick.

    Returns current price, ATR, watermarks, stop, trigger flag, and explanation.
    """
    side = str(direction).upper()
    if side not in {"UP", "DOWN"}:
        raise ValueError("direction must be UP or DOWN")
    if not isinstance(candles, pd.DataFrame) or not _REQUIRED.issubset(candles.columns):
        raise ValueError(f"candles must include columns: {sorted(_REQUIRED)}")
    if len(candles) < 20:
        raise ValueError("Need at least 20 closed candles for trend-stop calculation")
    if atr_multiplier < 1.0 or atr_multiplier > 6.0:
        raise ValueError("atr_multiplier must be between 1.0 and 6.0")
    if swing_lookback < 8:
        raise ValueError("swing_lookback must be at least 8")

    df = candles.copy().reset_index(drop=True)
    for col in _REQUIRED:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if df[list(_REQUIRED)].isna().any().any():
        raise ValueError("candle data has missing or non-numeric OHLCV values")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("candle prices must be positive")
    if (df["high"] < df[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError("candle high is inconsistent with OHLC values")
    if (df["low"] > df[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("candle low is inconsistent with OHLC values")
    if (df["volume"] < 0).any():
        raise ValueError("candle volume cannot be negative")

    current = float(df["close"].iloc[-1])
    atr = _atr(df, 14)
    prev_high = float(high_water_btc) if high_water_btc is not None else current
    prev_low = float(low_water_btc) if low_water_btc is not None else current
    high_water = max(prev_high, current)
    low_water = min(prev_low, current)
    swing_low, swing_high = _confirmed_swings(df, swing_lookback)

    if side == "UP":
        candidates = [high_water - atr_multiplier * atr]
        if trailing_stop_btc is not None:
            candidates.append(float(trailing_stop_btc))
        # Keep the last confirmed support even after a breach: a close below it
        # should trigger the stop, not erase the broken level from consideration.
        if swing_low is not None:
            candidates.append(swing_low)
        stop = max(candidates)
        triggered = current <= stop
        explanation = (
            f"UP trend stop: closed BTC price {current:.2f}; stop {stop:.2f}; "
            f"high-water {high_water:.2f}; ATR(14) {atr:.2f}; "
            f"confirmed swing low {swing_low if swing_low is not None else 'unavailable'}"
        )
    else:
        candidates = [low_water + atr_multiplier * atr]
        if trailing_stop_btc is not None:
            candidates.append(float(trailing_stop_btc))
        # Keep the last confirmed resistance after a breach so a close above it
        # can trigger the short-side stop.
        if swing_high is not None:
            candidates.append(swing_high)
        stop = min(candidates)
        triggered = current >= stop
        explanation = (
            f"DOWN trend stop: closed BTC price {current:.2f}; stop {stop:.2f}; "
            f"low-water {low_water:.2f}; ATR(14) {atr:.2f}; "
            f"confirmed swing high {swing_high if swing_high is not None else 'unavailable'}"
        )

    return {
        "direction": side,
        "btc_close": current,
        "atr_14": atr,
        "high_water_btc": high_water,
        "low_water_btc": low_water,
        "trailing_stop_btc": stop,
        "triggered": bool(triggered),
        "swing_low_btc": swing_low,
        "swing_high_btc": swing_high,
        "reason": explanation,
    }
