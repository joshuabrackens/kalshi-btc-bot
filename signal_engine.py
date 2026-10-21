"""
Kalshi BTC 15-minute bot — signal engine prototype
=================================================

This is the first, offline component of the bot. It does NOT connect to Kalshi,
TradingView, Telegram, or place orders. It evaluates supplied OHLCV candles and
market details using a transparent rule-based strategy inspired by the user's
described chart methodology.

Dependencies: pandas, numpy
Install: pip install pandas numpy

Expected candle DataFrame columns:
    timestamp, open, high, low, close, volume
Rows should be oldest -> newest. Supply separate 1m, 5m, and 15m candles.

Important:
- score is a heuristic score, NOT a calibrated probability of winning.
- Live use requires correct Kalshi market/settlement data, fee and order-book
  handling, testing, and safeguards.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional, Literal
import math

import numpy as np
import pandas as pd


Direction = Literal["BUY_UP", "BUY_DOWN", "WAIT"]


@dataclass
class MarketContext:
    strike_price: float
    seconds_remaining: int
    up_ask_cents: Optional[float] = None
    down_ask_cents: Optional[float] = None
    spread_cents: Optional[float] = None
    # Optional estimate of round-trip fees, expressed as cents per contract.
    estimated_fee_cents: float = 0.0


@dataclass
class Signal:
    direction: Direction
    score: int
    reasons: list[str]
    warnings: list[str]
    btc_price: float
    strike_price: float
    distance_from_strike: float
    seconds_remaining: int
    max_entry_cents: Optional[float]
    disclaimer: str = (
        "Heuristic score only; not a calibrated win probability or a promise "
        "of profitability. Paper-test before real trading."
    )

    def to_dict(self) -> dict:
        return asdict(self)


def _validate_candles(df: pd.DataFrame, label: str) -> pd.DataFrame:
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{label} candles missing columns: {sorted(missing)}")
    if len(df) < 20:
        raise ValueError(f"Need at least 20 {label} candles; got {len(df)}.")
    out = df.copy()
    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close", "volume"])
    if len(out) < 20:
        raise ValueError(f"Need at least 20 valid {label} candles.")
    return out.reset_index(drop=True)


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    last_loss = float(loss.iloc[-1])
    last_gain = float(gain.iloc[-1])
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0
    rs = last_gain / last_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _vwap(df: pd.DataFrame, lookback: int = 20) -> float:
    recent = df.tail(lookback)
    typical = (recent["high"] + recent["low"] + recent["close"]) / 3
    volume_sum = float(recent["volume"].sum())
    if volume_sum <= 0:
        return float(recent["close"].mean())
    return float((typical * recent["volume"]).sum() / volume_sum)


def _trend(df: pd.DataFrame) -> int:
    """Return +1 bullish, -1 bearish, 0 mixed using EMA and recent structure."""
    close = df["close"]
    ema_fast = float(close.ewm(span=8, adjust=False).mean().iloc[-1])
    ema_slow = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
    last = float(close.iloc[-1])
    prior = float(close.iloc[-4])
    bullish = last > ema_fast > ema_slow and last > prior
    bearish = last < ema_fast < ema_slow and last < prior
    return 1 if bullish else (-1 if bearish else 0)


def _candle_warnings(df: pd.DataFrame) -> list[str]:
    row = df.iloc[-1]
    candle_range = float(row["high"] - row["low"])
    if candle_range <= 0:
        return []
    body = abs(float(row["close"] - row["open"]))
    upper_wick = float(row["high"] - max(row["open"], row["close"]))
    lower_wick = float(min(row["open"], row["close"]) - row["low"])
    warnings = []
    if body / candle_range < 0.25 and candle_range > 0:
        warnings.append("Latest candle has a small body relative to its range.")
    if upper_wick / candle_range > 0.55:
        warnings.append("Long upper wick: possible rejection of higher prices.")
    if lower_wick / candle_range > 0.55:
        warnings.append("Long lower wick: possible rejection of lower prices.")

    if len(df) >= 6:
        recent_vol = float(df["volume"].tail(3).mean())
        baseline_vol = float(df["volume"].tail(20).head(17).mean())
        price_move = float(df["close"].iloc[-1] - df["close"].iloc[-4])
        if baseline_vol > 0 and recent_vol > baseline_vol * 1.8 and abs(price_move) < candle_range * 0.5:
            warnings.append("Volume spike without sustained directional movement.")
    return warnings


def evaluate_signal(
    candles_1m: pd.DataFrame,
    candles_5m: pd.DataFrame,
    candles_15m: pd.DataFrame,
    market: MarketContext,
    *,
    minimum_score: int = 59,
    max_entry_cents: float = 78.0,
    max_spread_cents: float = 5.0,
    min_seconds_remaining: int = 30,
    max_seconds_remaining: int = 15 * 60,
) -> Signal:
    """
    Evaluate one potential decision.

    `minimum_score` is a strictness threshold for the heuristic score, not a
    claimed win-rate threshold. Entry price is capped separately.
    """
    c1 = _validate_candles(candles_1m, "1-minute")
    c5 = _validate_candles(candles_5m, "5-minute")
    c15 = _validate_candles(candles_15m, "15-minute")

    price = float(c1["close"].iloc[-1])
    distance = price - market.strike_price
    warnings = _candle_warnings(c1)
    reasons: list[str] = []

    if market.seconds_remaining < min_seconds_remaining:
        return Signal("WAIT", 0, ["Too little time remains for a fresh entry."],
                      warnings, price, market.strike_price, distance,
                      market.seconds_remaining, None)
    if market.seconds_remaining > max_seconds_remaining:
        warnings.append("Time remaining is outside the expected 15-minute market window.")

    if market.spread_cents is not None and market.spread_cents > max_spread_cents:
        return Signal("WAIT", 0, ["Kalshi spread exceeds the configured limit."],
                      warnings, price, market.strike_price, distance,
                      market.seconds_remaining, None)

    trends = [_trend(c1), _trend(c5), _trend(c15)]
    score_up = 0
    score_down = 0

    # Multi-timeframe agreement: the longer chart carries more weight.
    for label, trend, weight in (("1m", trends[0], 1), ("5m", trends[1], 2), ("15m", trends[2], 2)):
        if trend > 0:
            score_up += weight
            reasons.append(f"{label} trend is bullish.")
        elif trend < 0:
            score_down += weight
            reasons.append(f"{label} trend is bearish.")
        else:
            reasons.append(f"{label} trend is mixed.")

    rsi = _rsi(c1["close"])
    vwap = _vwap(c1)
    last_close = float(c1["close"].iloc[-1])
    avg_vol = float(c1["volume"].tail(20).mean())
    recent_vol = float(c1["volume"].tail(3).mean())

    if last_close > vwap:
        score_up += 1
        reasons.append("Price is above the recent 1m VWAP.")
    elif last_close < vwap:
        score_down += 1
        reasons.append("Price is below the recent 1m VWAP.")

    if 50 <= rsi <= 68:
        score_up += 1
        reasons.append(f"1m RSI supports upward momentum without being extremely overbought ({rsi:.1f}).")
    elif 32 <= rsi < 50:
        score_down += 1
        reasons.append(f"1m RSI supports downward momentum without being extremely oversold ({rsi:.1f}).")
    elif rsi > 75:
        warnings.append(f"RSI is high ({rsi:.1f}); chasing an upward move may be risky.")
    elif rsi < 25:
        warnings.append(f"RSI is very low ({rsi:.1f}); chasing a downward move may be risky.")

    if avg_vol > 0 and recent_vol > avg_vol * 1.1:
        if last_close > float(c1["close"].iloc[-4]):
            score_up += 1
            reasons.append("Recent volume is above baseline during an upward move.")
        elif last_close < float(c1["close"].iloc[-4]):
            score_down += 1
            reasons.append("Recent volume is above baseline during a downward move.")
    else:
        warnings.append("Recent volume does not clearly confirm a directional move.")

    # Strike proximity is context, not a directional signal by itself.
    if distance > 0:
        reasons.append(f"BTC is {distance:.2f} above the strike; this alone does not guarantee UP settlement.")
    elif distance < 0:
        reasons.append(f"BTC is {abs(distance):.2f} below the strike; this alone does not guarantee DOWN settlement.")
    else:
        warnings.append("BTC is currently at the strike; direction is especially uncertain.")

    # Penalize a move that looks extended relative to recent 1m volatility.
    returns = c1["close"].pct_change().dropna().tail(20)
    volatility = float(returns.std()) if len(returns) > 1 else 0.0
    if volatility > 0:
        move_3 = abs(float(c1["close"].iloc[-1] / c1["close"].iloc[-4] - 1.0))
        if move_3 > 2.5 * volatility * math.sqrt(3):
            warnings.append("Recent move is unusually large; avoid chasing a huge candle.")
            score_up = max(0, score_up - 1)
            score_down = max(0, score_down - 1)

    # Reversal warnings reduce confidence in both directions.
    if any("Long upper wick" in w or "Long lower wick" in w for w in warnings):
        score_up = max(0, score_up - 1)
        score_down = max(0, score_down - 1)

    # Convert weighted evidence to a 0–100 heuristic score.
    max_possible = 10.0
    if score_up > score_down:
        direction: Direction = "BUY_UP"
        raw_score = round(100 * score_up / max_possible)
        opposite = score_down
        entry = market.up_ask_cents
    elif score_down > score_up:
        direction = "BUY_DOWN"
        raw_score = round(100 * score_down / max_possible)
        opposite = score_up
        entry = market.down_ask_cents
    else:
        direction, raw_score, opposite, entry = "WAIT", 0, 0, None

    # Require at least 2 points of directional separation.
    if direction != "WAIT" and (max(score_up, score_down) - opposite) < 2:
        direction, raw_score, entry = "WAIT", 0, None
        reasons.append("Evidence is too conflicted to choose a direction.")

    if direction != "WAIT" and raw_score < minimum_score:
        reasons.append(f"Signal score {raw_score}/100 is below the configured {minimum_score}/100 threshold.")
        direction, entry = "WAIT", None

    if direction != "WAIT":
        if entry is None:
            warnings.append("No live ask price was supplied; cannot validate an entry.")
            direction = "WAIT"
        elif entry > max_entry_cents:
            reasons.append(f"Contract ask {entry:.1f}¢ exceeds the configured {max_entry_cents:.1f}¢ entry cap.")
            direction, entry = "WAIT", None
        elif entry + market.estimated_fee_cents >= 100:
            warnings.append("Entry plus estimated fees leaves no positive maximum payout margin.")
            direction, entry = "WAIT", None

    return Signal(
        direction=direction,
        score=int(raw_score if direction != "WAIT" else 0),
        reasons=reasons,
        warnings=warnings,
        btc_price=price,
        strike_price=market.strike_price,
        distance_from_strike=distance,
        seconds_remaining=market.seconds_remaining,
        max_entry_cents=max_entry_cents if direction != "WAIT" else None,
    )


if __name__ == "__main__":
    print("Signal engine module loaded.")
    print("Import evaluate_signal() and provide current 1m/5m/15m candles plus Kalshi market context.")
    print("This prototype does not connect to live feeds or place trades.")
