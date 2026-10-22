"""Explainable rule-based signal engine for the Kalshi BTC 15m PAPER bot.

Offline only: no network access, authenticated endpoints, or order placement.
The score is a heuristic evidence score, NOT a calibrated win probability.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Optional, Literal
import math
import pandas as pd

Direction = Literal["BUY_UP", "BUY_DOWN", "WAIT"]

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
    disclaimer: str = ("Heuristic evidence score only; not a calibrated win probability "
                       "or a promise of profitability. Paper-test before real trading.")
    def to_dict(self) -> dict:
        return asdict(self)

def _validate_candles(df: pd.DataFrame, label: str) -> pd.DataFrame:
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing: raise ValueError(f"{label} candles missing columns: {sorted(missing)}")
    if len(df) < 20: raise ValueError(f"Need at least 20 {label} candles; got {len(df)}.")
    out = df.copy()
    for col in required: out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=list(required)).reset_index(drop=True)
    if len(out) < 20: raise ValueError(f"Need at least 20 valid {label} candles.")
    if (out[["high", "low", "open", "close"]] <= 0).any().any():
        raise ValueError(f"{label} candles contain non-positive prices.")
    if (out["high"] < out[["open", "close", "low"]].max(axis=1)).any() or (out["low"] > out[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError(f"{label} candles contain inconsistent OHLC values.")
    return out

def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff(); gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    gl, ll = float(gain.iloc[-1]), float(loss.iloc[-1])
    if ll == 0: return 100.0 if gl > 0 else 50.0
    return 100.0 - 100.0/(1.0 + gl/ll)

def _vwap(df: pd.DataFrame, lookback: int = 20) -> float:
    recent=df.tail(lookback); typical=(recent.high+recent.low+recent.close)/3; vol=float(recent.volume.sum())
    return float((typical*recent.volume).sum()/vol) if vol > 0 else float(recent.close.mean())

def _trend(df: pd.DataFrame) -> int:
    close=df.close; fast=float(close.ewm(span=8, adjust=False).mean().iloc[-1]); slow=float(close.ewm(span=21, adjust=False).mean().iloc[-1]); last=float(close.iloc[-1]); prior=float(close.iloc[-4])
    return 1 if last > fast > slow and last > prior else (-1 if last < fast < slow and last < prior else 0)

def _candle_warnings(df: pd.DataFrame) -> list[str]:
    row=df.iloc[-1]; rng=float(row.high-row.low)
    if rng <= 0: return ["Latest candle has zero range."]
    body=abs(float(row.close-row.open)); upper=float(row.high-max(row.open,row.close)); lower=float(min(row.open,row.close)-row.low); out=[]
    if body/rng < .25: out.append("Latest candle has a small body relative to its range.")
    if upper/rng > .55: out.append("Long upper wick: possible rejection of higher prices.")
    if lower/rng > .55: out.append("Long lower wick: possible rejection of lower prices.")
    baseline=float(df.volume.tail(20).head(17).mean()); recent=float(df.volume.tail(3).mean()); move=float(df.close.iloc[-1]-df.close.iloc[-4])
    if baseline > 0 and recent > baseline*1.8 and abs(move) < rng*.5: out.append("Volume spike without sustained directional movement.")
    return out

def _market_structure(df: pd.DataFrame) -> dict:
    # Compare two recent, non-overlapping windows; deliberately descriptive, not a perfect pivot detector.
    older=df.iloc[-12:-6]; newer=df.iloc[-6:]
    oh, ol=float(older.high.max()), float(older.low.min()); nh, nl=float(newer.high.max()), float(newer.low.min())
    if nh > oh and nl > ol: direction=1; label="higher highs and higher lows"
    elif nh < oh and nl < ol: direction=-1; label="lower highs and lower lows"
    else: direction=0; label="mixed / range-like swing structure"
    return {"direction":direction,"label":label,"older_high":oh,"older_low":ol,"recent_high":nh,"recent_low":nl}

def _box_theory(df: pd.DataFrame, lookback: int = 20) -> dict:
    # Define the prior box without the current candle to avoid using the breakout candle to set its own boundary.
    prior=df.iloc[-(lookback+1):-1]; last=df.iloc[-1]; top=float(prior.high.max()); bottom=float(prior.low.min()); width=top-bottom
    if width <= 0: return {"state":"invalid_box","top":top,"bottom":bottom,"direction":0}
    # Require close beyond boundary; wick-only breach is a warning, not a breakout.
    if float(last.close) > top: state="bullish_breakout"; direction=1
    elif float(last.close) < bottom: state="bearish_breakout"; direction=-1
    elif float(last.high) > top or float(last.low) < bottom: state="wick_break_or_rejection"; direction=0
    else: state="inside_box"; direction=0
    return {"state":state,"top":top,"bottom":bottom,"width":width,"direction":direction}

def evaluate_signal(candles_1m: pd.DataFrame, candles_5m: pd.DataFrame, candles_15m: pd.DataFrame,
                    market: MarketContext, *, minimum_score: int = 55, max_entry_cents: float = 78.0,
                    max_spread_cents: float = 5.0, min_seconds_remaining: int = 30,
                    max_seconds_remaining: int = 15*60) -> Signal:
    c1=_validate_candles(candles_1m,"1-minute"); c5=_validate_candles(candles_5m,"5-minute"); c15=_validate_candles(candles_15m,"15-minute")
    price=float(c1.close.iloc[-1]); strike=float(market.strike_price); distance=price-strike; warnings=_candle_warnings(c1); reasons=[]
    strategies={}
    def result(direction="WAIT", score=0, entry=None):
        return Signal(direction, int(score), reasons.copy(), warnings.copy(), price, strike, distance,
                      int(market.seconds_remaining), entry, strategies.copy())
    if not math.isfinite(strike) or strike <= 0: raise ValueError("Market strike price must be positive and finite.")
    if market.seconds_remaining < min_seconds_remaining:
        reasons.append("Too little time remains for a fresh entry."); return result()
    if market.seconds_remaining > max_seconds_remaining: warnings.append("Time remaining is outside the expected 15-minute market window.")
    if market.spread_cents is not None and market.spread_cents > max_spread_cents:
        reasons.append("Kalshi spread exceeds the configured limit."); return result()

    scores={"up":0,"down":0}
    # 1) Multi-timeframe trend: 1m=1, 5m=2, 15m=2
    trends={"1m":_trend(c1),"5m":_trend(c5),"15m":_trend(c15)}
    trend_points=0
    for label, weight in (("1m",1),("5m",2),("15m",2)):
        t=trends[label]
        if t>0: scores["up"]+=weight; trend_points+=weight; reasons.append(f"{label} trend is bullish.")
        elif t<0: scores["down"]+=weight; trend_points-=weight; reasons.append(f"{label} trend is bearish.")
        else: reasons.append(f"{label} trend is mixed.")
    strategies["multi_timeframe_trend"]={"direction":1 if trend_points>0 else (-1 if trend_points<0 else 0),"points":trend_points,"timeframes":trends}

    # 2) VWAP and momentum
    rsi=_rsi(c1.close); vwap=_vwap(c1); last=float(c1.close.iloc[-1]); avg_vol=float(c1.volume.tail(20).mean()); recent_vol=float(c1.volume.tail(3).mean())
    if last>vwap: scores["up"]+=1; reasons.append("Price is above recent 1m VWAP.")
    elif last<vwap: scores["down"]+=1; reasons.append("Price is below recent 1m VWAP.")
    if 50<=rsi<=68: scores["up"]+=1; reasons.append(f"1m RSI supports upward momentum ({rsi:.1f}).")
    elif 32<=rsi<50: scores["down"]+=1; reasons.append(f"1m RSI supports downward momentum ({rsi:.1f}).")
    elif rsi>75: warnings.append(f"RSI is high ({rsi:.1f}); chasing an upward move may be risky.")
    elif rsi<25: warnings.append(f"RSI is very low ({rsi:.1f}); chasing a downward move may be risky.")
    strategies["momentum"]={"rsi_1m":round(rsi,2),"vwap_1m":round(vwap,2),"direction":1 if last>vwap and rsi>=50 else (-1 if last<vwap and rsi<50 else 0)}
    if avg_vol>0 and recent_vol>avg_vol*1.1:
        if last>float(c1.close.iloc[-4]): scores["up"]+=1; reasons.append("Recent volume is above baseline during an upward move.")
        elif last<float(c1.close.iloc[-4]): scores["down"]+=1; reasons.append("Recent volume is above baseline during a downward move.")
    else: warnings.append("Recent volume does not clearly confirm a directional move.")
    strategies["volume_confirmation"]={"recent_3_mean":round(recent_vol,4),"baseline_20_mean":round(avg_vol,4),"confirmed":bool(avg_vol>0 and recent_vol>avg_vol*1.1)}

    # 3) Box theory and structure. Breakouts count only if volume is elevated; otherwise warn and don't award direction points.
    box=_box_theory(c1); structure=_market_structure(c1); strategies["box_theory"]=box; strategies["market_structure"]=structure
    if box["state"] in ("bullish_breakout","bearish_breakout"):
        if avg_vol>0 and recent_vol>avg_vol*1.1:
            if box["direction"]>0: scores["up"]+=2; reasons.append(f"Box theory: close broke above the prior {20}-candle range with volume confirmation.")
            else: scores["down"]+=2; reasons.append(f"Box theory: close broke below the prior {20}-candle range with volume confirmation.")
        else: warnings.append("Price broke the prior box, but volume did not confirm; breakout points withheld.")
    elif box["state"]=="inside_box": reasons.append("Box theory: price remains inside the recent consolidation range.")
    else: warnings.append("Box theory: wick breached a box boundary but the candle did not close beyond it; possible rejection/false break.")
    if structure["direction"]>0: scores["up"]+=1; reasons.append("Market structure shows higher highs and higher lows.")
    elif structure["direction"]<0: scores["down"]+=1; reasons.append("Market structure shows lower highs and lower lows.")
    else: reasons.append("Market structure is mixed; no directional structure points awarded.")

    # 4) Volatility/chase filter
    returns=c1.close.pct_change().dropna().tail(20); volatility=float(returns.std()) if len(returns)>1 else 0.0
    move_3=abs(float(c1.close.iloc[-1]/c1.close.iloc[-4]-1.0))
    if volatility>0 and move_3>2.5*volatility*math.sqrt(3):
        warnings.append("Recent move is unusually large; avoid chasing a huge candle."); scores["up"]=max(0,scores["up"]-1); scores["down"]=max(0,scores["down"]-1)
    strategies["volatility"]={"recent_return_std":round(volatility,7),"three_candle_abs_move_pct":round(move_3*100,4),"chase_warning":any("unusually large" in w for w in warnings)}
    if any("Long upper wick" in w or "Long lower wick" in w for w in warnings):
        scores["up"]=max(0,scores["up"]-1); scores["down"]=max(0,scores["down"]-1)

    if distance>0: reasons.append(f"BTC is {distance:.2f} above the strike; this alone does not guarantee UP settlement.")
    elif distance<0: reasons.append(f"BTC is {abs(distance):.2f} below the strike; this alone does not guarantee DOWN settlement.")
    else: warnings.append("BTC is currently at the strike; direction is especially uncertain.")
    strategies["evidence_totals"]={"up_points":scores["up"],"down_points":scores["down"],"net_points":scores["up"]-scores["down"]}

    if scores["up"]==scores["down"]: reasons.append("Evidence is tied or conflicted; no trade is recommended."); return result()
    side="up" if scores["up"]>scores["down"] else "down"; opposite=scores["down"] if side=="up" else scores["up"]
    winning=scores[side]; separation=winning-opposite
    # 10 is the current theoretical maximum with all implemented directional evidence; clamp score for display.
    score=max(0,min(100,round(winning/12*100)))
    if separation<2: reasons.append("Evidence is too conflicted to choose a direction (need 2-point separation). "); return result()
    if score<minimum_score: reasons.append(f"Evidence score {score}/100 is below the configured {minimum_score}/100 threshold."); return result()
    entry=market.up_ask_cents if side=="up" else market.down_ask_cents
    if entry is None: warnings.append("No ask price was supplied; cannot validate an entry."); return result()
    if entry>max_entry_cents: reasons.append(f"Contract ask {entry:.1f}¢ exceeds the configured {max_entry_cents:.1f}¢ entry cap."); return result()
    if entry+market.estimated_fee_cents>=100: warnings.append("Entry plus estimated fees leaves no positive maximum payout margin."); return result()
    reasons.append(f"Final decision: BUY_{side.upper()} based on {winning} directional evidence points versus {opposite}; score is not a win probability.")
    return result("BUY_UP" if side=="up" else "BUY_DOWN", score, max_entry_cents)

if __name__ == "__main__":
    print("Signal engine module loaded. Paper-only, rule-based, explainable; no trading connections.")
