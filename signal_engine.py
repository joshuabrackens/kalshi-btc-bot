"""Explainable rule-based signal engine for a Kalshi BTC 15-minute PAPER bot.

Offline only: no network access, authenticated endpoints, or order placement.
The score is a heuristic evidence score, NOT a calibrated win probability.

Drop-in replacement for the previous signal_engine.py. Keeps evaluate_signal(),
MarketContext, Signal, and the existing output fields. Adds entry_ask_cents so
outputs distinguish the observed ask from the configured maximum entry cap.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Optional, Literal
import math
import pandas as pd

Direction = Literal["BUY_UP", "BUY_DOWN", "WAIT"]
_REQUIRED_CANDLE_COLUMNS = {"open", "high", "low", "close", "volume"}
# Maximum directional evidence: trend(5) + VWAP(1) + RSI(1) + volume(1)
# + box breakout(2) + market structure(1) = 11 points.
_MAX_DIRECTIONAL_POINTS = 11


@dataclass
class MarketContext:
    strike_price: float
    seconds_remaining: int
    up_ask_cents: Optional[float] = None
    down_ask_cents: Optional[float] = None
    spread_cents: Optional[float] = None
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
    strategies: dict = field(default_factory=dict)
    disclaimer: str = (
        "Heuristic evidence score only; not a calibrated win probability "
        "or a promise of profitability. Paper-test before real trading."
    )
    # Actual ask considered by the signal. max_entry_cents remains the configured cap.
    entry_ask_cents: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _finite_number(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _validate_candles(df: pd.DataFrame, label: str) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"{label} candles must be a pandas DataFrame.")
    missing = _REQUIRED_CANDLE_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"{label} candles missing columns: {sorted(missing)}")
    if len(df) < 20:
        raise ValueError(f"Need at least 20 {label} candles; got {len(df)}.")

    out = df.copy()
    for col in _REQUIRED_CANDLE_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    # Inf values are not valid market observations; treat them as missing rows.
    out = out.replace([float("inf"), float("-inf")], float("nan"))
    out = out.dropna(subset=list(_REQUIRED_CANDLE_COLUMNS)).reset_index(drop=True)
    if len(out) < 20:
        raise ValueError(f"Need at least 20 valid {label} candles.")
    if (out[["high", "low", "open", "close"]] <= 0).any().any():
        raise ValueError(f"{label} candles contain non-positive prices.")
    if (out["volume"] < 0).any():
        raise ValueError(f"{label} candles contain negative volume.")
    if (out["high"] < out[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError(f"{label} candles contain inconsistent OHLC values (high too low).")
    if (out["low"] > out[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError(f"{label} candles contain inconsistent OHLC values (low too high).")
    return out


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    gl, ll = float(gain.iloc[-1]), float(loss.iloc[-1])
    if not math.isfinite(gl) or not math.isfinite(ll):
        return 50.0
    if ll == 0:
        return 100.0 if gl > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gl / ll)


def _vwap(df: pd.DataFrame, lookback: int = 20) -> float:
    recent = df.tail(lookback)
    typical = (recent.high + recent.low + recent.close) / 3
    vol = float(recent.volume.sum())
    return float((typical * recent.volume).sum() / vol) if vol > 0 else float(recent.close.mean())


def _trend(df: pd.DataFrame) -> int:
    close = df.close
    fast = float(close.ewm(span=8, adjust=False).mean().iloc[-1])
    slow = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
    last, prior = float(close.iloc[-1]), float(close.iloc[-4])
    if last > fast > slow and last > prior:
        return 1
    if last < fast < slow and last < prior:
        return -1
    return 0


def _candle_warnings(df: pd.DataFrame) -> list[str]:
    row = df.iloc[-1]
    rng = float(row.high - row.low)
    if rng <= 0:
        return ["Latest candle has zero range."]
    body = abs(float(row.close - row.open))
    upper = float(row.high - max(row.open, row.close))
    lower = float(min(row.open, row.close) - row.low)
    out: list[str] = []
    if body / rng < 0.25:
        out.append("Latest candle has a small body relative to its range.")
    if upper / rng > 0.55:
        out.append("Long upper wick: possible rejection of higher prices.")
    if lower / rng > 0.55:
        out.append("Long lower wick: possible rejection of lower prices.")
    baseline = float(df.volume.tail(20).head(17).mean())
    recent = float(df.volume.tail(3).mean())
    move = float(df.close.iloc[-1] - df.close.iloc[-4])
    if baseline > 0 and recent > baseline * 1.8 and abs(move) < rng * 0.5:
        out.append("Volume spike without sustained directional movement.")
    return out


def _market_structure(df: pd.DataFrame) -> dict:
    older = df.iloc[-12:-6]
    newer = df.iloc[-6:]
    oh, ol = float(older.high.max()), float(older.low.min())
    nh, nl = float(newer.high.max()), float(newer.low.min())
    if nh > oh and nl > ol:
        direction, label = 1, "higher highs and higher lows"
    elif nh < oh and nl < ol:
        direction, label = -1, "lower highs and lower lows"
    else:
        direction, label = 0, "mixed / range-like swing structure"
    return {"direction": direction, "label": label, "older_high": oh,
            "older_low": ol, "recent_high": nh, "recent_low": nl}


def _box_theory(df: pd.DataFrame, lookback: int = 20) -> dict:
    # Exclude the current candle from the range. With exactly 20 rows, use
    # the 19 available prior rows rather than silently creating an empty box.
    prior_count = min(lookback, max(0, len(df) - 1))
    prior = df.iloc[-(prior_count + 1):-1]
    last = df.iloc[-1]
    if prior.empty:
        return {"state": "invalid_box", "top": None, "bottom": None,
                "width": None, "direction": 0, "prior_candles": 0}
    top, bottom = float(prior.high.max()), float(prior.low.min())
    width = top - bottom
    if not math.isfinite(width) or width <= 0:
        return {"state": "invalid_box", "top": top, "bottom": bottom,
                "width": width, "direction": 0, "prior_candles": len(prior)}
    if float(last.close) > top:
        state, direction = "bullish_breakout", 1
    elif float(last.close) < bottom:
        state, direction = "bearish_breakout", -1
    elif float(last.high) > top or float(last.low) < bottom:
        state, direction = "wick_break_or_rejection", 0
    else:
        state, direction = "inside_box", 0
    return {"state": state, "top": top, "bottom": bottom, "width": width,
            "direction": direction, "prior_candles": len(prior)}


def evaluate_signal(
    candles_1m: pd.DataFrame,
    candles_5m: pd.DataFrame,
    candles_15m: pd.DataFrame,
    market: MarketContext,
    *, minimum_score: int = 55,
    max_entry_cents: float = 78.0,
    max_spread_cents: float = 5.0,
    min_seconds_remaining: int = 30,
    max_seconds_remaining: int = 15 * 60,
) -> Signal:
    """Evaluate three candle timeframes and return an explainable paper signal.

    A BUY signal is a rule-based direction candidate, not an estimate of the
    chance the contract settles in that direction. No orders are submitted.
    """
    c1 = _validate_candles(candles_1m, "1-minute")
    c5 = _validate_candles(candles_5m, "5-minute")
    c15 = _validate_candles(candles_15m, "15-minute")

    if not isinstance(market, MarketContext):
        raise TypeError("market must be a MarketContext instance.")
    if not _finite_number(market.strike_price) or float(market.strike_price) <= 0:
        raise ValueError("Market strike price must be positive and finite.")
    if not _finite_number(market.seconds_remaining):
        raise ValueError("seconds_remaining must be finite.")
    if not 0 <= minimum_score <= 100:
        raise ValueError("minimum_score must be between 0 and 100.")
    if not _finite_number(max_entry_cents) or not 0 < float(max_entry_cents) < 100:
        raise ValueError("max_entry_cents must be greater than 0 and less than 100.")
    if not _finite_number(max_spread_cents) or float(max_spread_cents) < 0:
        raise ValueError("max_spread_cents must be finite and non-negative.")
    if min_seconds_remaining < 0 or max_seconds_remaining < min_seconds_remaining:
        raise ValueError("Invalid seconds-remaining bounds.")
    if not _finite_number(market.estimated_fee_cents) or market.estimated_fee_cents < 0:
        raise ValueError("estimated_fee_cents must be finite and non-negative.")

    for label, ask in (("up_ask_cents", market.up_ask_cents),
                       ("down_ask_cents", market.down_ask_cents)):
        if ask is not None and (not _finite_number(ask) or not 0 < float(ask) < 100):
            raise ValueError(f"{label} must be None or a finite value strictly between 0 and 100.")
    if market.spread_cents is not None and (
        not _finite_number(market.spread_cents) or float(market.spread_cents) < 0
    ):
        raise ValueError("spread_cents must be None or a finite non-negative value.")

    price = float(c1.close.iloc[-1])
    strike = float(market.strike_price)
    distance = price - strike
    reasons: list[str] = []
    warnings = _candle_warnings(c1)
    strategies: dict = {}

    def result(direction: Direction = "WAIT", score: int = 0,
               allowed_cap: Optional[float] = None,
               observed_ask: Optional[float] = None) -> Signal:
        return Signal(
            direction=direction, score=int(score), reasons=reasons.copy(),
            warnings=warnings.copy(), btc_price=price, strike_price=strike,
            distance_from_strike=distance, seconds_remaining=int(market.seconds_remaining),
            max_entry_cents=allowed_cap, strategies=strategies.copy(),
            entry_ask_cents=observed_ask,
        )

    seconds = int(market.seconds_remaining)
    if seconds < min_seconds_remaining:
        reasons.append("Too little time remains for a fresh entry.")
        return result()
    # Outside the configured 15-minute window is a hard veto, not just a warning.
    if seconds > max_seconds_remaining:
        reasons.append("Time remaining is outside the configured market window; entry blocked.")
        return result()
    if market.spread_cents is None:
        warnings.append("No spread supplied; spread filter could not be checked.")
    elif float(market.spread_cents) > max_spread_cents:
        reasons.append("Kalshi spread exceeds the configured limit.")
        return result()

    scores = {"up": 0, "down": 0}
    trends = {"1m": _trend(c1), "5m": _trend(c5), "15m": _trend(c15)}
    trend_points = 0
    for label, weight in (("1m", 1), ("5m", 2), ("15m", 2)):
        trend = trends[label]
        if trend > 0:
            scores["up"] += weight
            trend_points += weight
            reasons.append(f"{label} trend is bullish.")
        elif trend < 0:
            scores["down"] += weight
            trend_points -= weight
            reasons.append(f"{label} trend is bearish.")
        else:
            reasons.append(f"{label} trend is mixed.")
    strategies["multi_timeframe_trend"] = {
        "direction": 1 if trend_points > 0 else (-1 if trend_points < 0 else 0),
        "points": trend_points, "timeframes": trends,
    }

    rsi = _rsi(c1.close)
    vwap = _vwap(c1)
    last = float(c1.close.iloc[-1])
    avg_vol = float(c1.volume.tail(20).mean())
    recent_vol = float(c1.volume.tail(3).mean())
    if last > vwap:
        scores["up"] += 1
        reasons.append("Price is above recent 1m VWAP.")
    elif last < vwap:
        scores["down"] += 1
        reasons.append("Price is below recent 1m VWAP.")
    if 50 <= rsi <= 68:
        scores["up"] += 1
        reasons.append(f"1m RSI supports upward momentum ({rsi:.1f}).")
    elif 32 <= rsi < 50:
        scores["down"] += 1
        reasons.append(f"1m RSI supports downward momentum ({rsi:.1f}).")
    elif rsi > 75:
        warnings.append(f"RSI is high ({rsi:.1f}); chasing an upward move may be risky.")
    elif rsi < 25:
        warnings.append(f"RSI is very low ({rsi:.1f}); chasing a downward move may be risky.")
    strategies["momentum"] = {
        "rsi_1m": round(rsi, 2), "vwap_1m": round(vwap, 2),
        "direction": 1 if last > vwap and rsi >= 50 else (-1 if last < vwap and rsi < 50 else 0),
    }

    volume_confirmed = avg_vol > 0 and recent_vol > avg_vol * 1.1
    if volume_confirmed:
        if last > float(c1.close.iloc[-4]):
            scores["up"] += 1
            reasons.append("Recent volume is above baseline during an upward move.")
        elif last < float(c1.close.iloc[-4]):
            scores["down"] += 1
            reasons.append("Recent volume is above baseline during a downward move.")
        else:
            warnings.append("Volume is elevated but price has no net direction over the recent window.")
    else:
        warnings.append("Recent volume does not clearly confirm a directional move.")
    strategies["volume_confirmation"] = {
        "recent_3_mean": round(recent_vol, 4),
        "baseline_20_mean": round(avg_vol, 4),
        "confirmed": bool(volume_confirmed),
    }

    box = _box_theory(c1)
    structure = _market_structure(c1)
    strategies["box_theory"] = box
    strategies["market_structure"] = structure
    if box["state"] in ("bullish_breakout", "bearish_breakout"):
        if volume_confirmed:
            if box["direction"] > 0:
                scores["up"] += 2
                reasons.append("Box theory: close broke above the prior range with volume confirmation.")
            else:
                scores["down"] += 2
                reasons.append("Box theory: close broke below the prior range with volume confirmation.")
        else:
            warnings.append("Price broke the prior box, but volume did not confirm; breakout points withheld.")
    elif box["state"] == "inside_box":
        reasons.append("Box theory: price remains inside the recent consolidation range.")
    else:
        warnings.append("Box theory: wick breached a box boundary but the candle did not close beyond it; possible rejection/false break.")
    if structure["direction"] > 0:
        scores["up"] += 1
        reasons.append("Market structure shows higher highs and higher lows.")
    elif structure["direction"] < 0:
        scores["down"] += 1
        reasons.append("Market structure shows lower highs and lower lows.")
    else:
        reasons.append("Market structure is mixed; no directional structure points awarded.")

    returns = c1.close.pct_change().dropna().tail(20)
    volatility = float(returns.std()) if len(returns) > 1 else 0.0
    move_3 = abs(float(c1.close.iloc[-1] / c1.close.iloc[-4] - 1.0))
    chase_warning = volatility > 0 and move_3 > 2.5 * volatility * math.sqrt(3)
    if chase_warning:
        warnings.append("Recent move is unusually large; avoid chasing a huge candle.")
        scores["up"] = max(0, scores["up"] - 1)
        scores["down"] = max(0, scores["down"] - 1)
    strategies["volatility"] = {
        "recent_return_std": round(volatility, 7),
        "three_candle_abs_move_pct": round(move_3 * 100, 4),
        "chase_warning": bool(chase_warning),
    }
    if any("Long upper wick" in w or "Long lower wick" in w for w in warnings):
        scores["up"] = max(0, scores["up"] - 1)
        scores["down"] = max(0, scores["down"] - 1)

    if distance > 0:
        reasons.append(f"BTC is {distance:.2f} above the strike; this alone does not guarantee UP settlement.")
    elif distance < 0:
        reasons.append(f"BTC is {abs(distance):.2f} below the strike; this alone does not guarantee DOWN settlement.")
    else:
        warnings.append("BTC is currently at the strike; direction is especially uncertain.")

    strategies["evidence_totals"] = {
        "up_points": scores["up"], "down_points": scores["down"],
        "net_points": scores["up"] - scores["down"],
        "maximum_theoretical_directional_points": _MAX_DIRECTIONAL_POINTS,
    }
    if scores["up"] == scores["down"]:
        reasons.append("Evidence is tied or conflicted; no trade is recommended.")
        return result()

    side = "up" if scores["up"] > scores["down"] else "down"
    winning = scores[side]
    opposite = scores["down"] if side == "up" else scores["up"]
    separation = winning - opposite
    # Use the corrected 11-point theoretical maximum, not the previous 12-point divisor.
    score = max(0, min(100, round(winning / _MAX_DIRECTIONAL_POINTS * 100)))
    candidate_ask = market.up_ask_cents if side == "up" else market.down_ask_cents
    if separation < 2:
        reasons.append("Evidence is too conflicted to choose a direction (need 2-point separation).")
        return result(observed_ask=candidate_ask)
    if score < minimum_score:
        reasons.append(f"Evidence score {score}/100 is below the configured {minimum_score}/100 threshold.")
        # Preserve the calculated evidence score on WAIT decisions so analytics
        # can distinguish weak evidence from a data/market-window veto. This is
        # a heuristic score, not a settlement probability.
        return result(score=score, observed_ask=candidate_ask)
    if candidate_ask is None:
        warnings.append("No ask price was supplied for the candidate side; cannot validate an entry.")
        return result(observed_ask=None)
    if candidate_ask > max_entry_cents:
        reasons.append(f"Contract ask {candidate_ask:.1f}¢ exceeds the configured {max_entry_cents:.1f}¢ entry cap.")
        return result(observed_ask=candidate_ask)
    if candidate_ask + market.estimated_fee_cents >= 100:
        warnings.append("Entry plus estimated fees leaves no positive maximum-payout margin.")
        return result(observed_ask=candidate_ask)

    reasons.append(
        f"Final decision: BUY_{side.upper()} based on {winning} directional evidence points "
        f"versus {opposite}; score is not a win probability."
    )
    # max_entry_cents is the configured cap; entry_ask_cents is the quote observed.
    return result("BUY_UP" if side == "up" else "BUY_DOWN", score,
                  allowed_cap=float(max_entry_cents), observed_ask=float(candidate_ask))


if __name__ == "__main__":
    print("Signal engine module loaded. Paper-only, rule-based, explainable; no trading connections.")
