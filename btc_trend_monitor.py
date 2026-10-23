"""Pure helper for preparing a BTC chart-trend snapshot ahead of TradingView alerts.

This module never places trades. Its direction is a pre-analysis bias only; the
worker must still validate a fresh TradingView alert, a live Kalshi order book,
entry-price/spread limits, current market window, open-position/risk rules, and
hybrid veto checks before any paper entry.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from signal_engine import MarketContext, evaluate_signal


def _candle_asof(df: Any) -> str | None:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    if "timestamp" not in df.columns:
        return None
    value = df["timestamp"].iloc[-1]
    try:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, (int, float)):
            # Candle providers normally return ISO timestamps; support epoch in ms/s.
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000.0
            return datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif isinstance(value, datetime):
            parsed = value
        else:
            return str(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return str(value)


def build_trend_snapshot(bundle: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Convert closed 1m/5m/15m candles into a dashboard-safe pre-analysis snapshot."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    required = ("candles_1m", "candles_5m", "candles_15m")
    missing = [key for key in required if not isinstance(bundle.get(key), pd.DataFrame) or bundle[key].empty]
    if missing:
        raise ValueError("Trend monitor received missing or empty candle frames: " + ", ".join(missing))

    c1 = bundle["candles_1m"]
    btc_price = float(pd.to_numeric(c1["close"], errors="raise").iloc[-1])
    if not (btc_price > 0):
        raise ValueError("Trend monitor BTC price was not positive")

    # A same-price strike keeps this a chart-only analysis, not an estimate for a
    # particular Kalshi contract. Quote checks are intentionally absent here.
    signal = evaluate_signal(
        bundle["candles_1m"], bundle["candles_5m"], bundle["candles_15m"],
        MarketContext(strike_price=btc_price, seconds_remaining=900),
        minimum_score=0, max_entry_cents=80.0, max_spread_cents=5.0,
    )
    strategies = signal.strategies if isinstance(signal.strategies, dict) else {}
    multi = strategies.get("multi_timeframe_trend") or {}
    timeframe_values = multi.get("timeframes") or {}
    normalize = lambda value: "bullish" if value == 1 else "bearish" if value == -1 else "mixed"
    evidence = strategies.get("evidence_totals") or {}
    up_points = int(evidence.get("up_points") or 0)
    down_points = int(evidence.get("down_points") or 0)
    net = up_points - down_points
    # This is a directional bias label only. No buy decision or probability is output.
    bias = "UP" if net >= 2 else "DOWN" if net <= -2 else "MIXED"
    momentum = strategies.get("momentum") or {}
    volume = strategies.get("volume_confirmation") or {}
    box = strategies.get("box_theory") or {}
    structure = strategies.get("market_structure") or {}
    volatility = strategies.get("volatility") or {}

    return {
        "status": "ok",
        "monitor_only": True,
        "updated_at": now.isoformat(),
        "provider": "Binance public REST",
        "symbol": "BTCUSDT",
        "btc_price": round(btc_price, 2),
        "candidate_bias": bias,
        "evidence_score": round(max(up_points, down_points) / 11 * 100),
        "evidence_points": {"up": up_points, "down": down_points, "net": net, "max_directional_points": 11},
        "timeframes": {tf: normalize(timeframe_values.get(tf, 0)) for tf in ("1m", "5m", "15m")},
        "candle_asof": {
            "1m": _candle_asof(bundle["candles_1m"]),
            "5m": _candle_asof(bundle["candles_5m"]),
            "15m": _candle_asof(bundle["candles_15m"]),
        },
        "momentum": {
            "rsi_1m": momentum.get("rsi_1m"),
            "vwap_1m": momentum.get("vwap_1m"),
            "direction": momentum.get("direction", 0),
        },
        "volume_confirmation": {
            "confirmed": bool(volume.get("confirmed", False)),
            "recent_mean": volume.get("recent_3_mean"),
            "baseline_mean": volume.get("baseline_20_mean"),
        },
        "market_structure": {"direction": structure.get("direction", 0), "label": structure.get("label", "unknown")},
        "box_theory": {"state": box.get("state", "unknown"), "direction": box.get("direction", 0)},
        "volatility": {
            "chase_warning": bool(volatility.get("chase_warning", False)),
            "three_candle_abs_move_pct": volatility.get("three_candle_abs_move_pct"),
        },
        "reasons": list(signal.reasons)[:12],
        "warnings": list(signal.warnings)[:8],
        "disclaimer": "Pre-analysis only, not a trading signal or calibrated win probability. A fresh alert and all live-price/risk checks are still required.",
    }
