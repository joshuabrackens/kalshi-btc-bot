"""Public Binance BTC/USDT candle feed for paper-trading signal evaluation.

Uses Binance's unauthenticated market-data-only REST endpoint.
Returns only fully closed candles, oldest first, in the columns expected by
signal_engine.evaluate_signal().
"""
from __future__ import annotations

from datetime import datetime, timezone
import time

import pandas as pd
import requests

BASE_URL = "https://data-api.binance.vision/api/v3/klines"
SYMBOL = "BTCUSDT"
TIMEOUT_SECONDS = 10
MIN_CANDLES = 25


class CandleFeedError(RuntimeError):
    """Raised when the candle feed is unavailable, stale, or malformed."""


def fetch_closed_candles(interval: str, limit: int = 100) -> pd.DataFrame:
    """Fetch recent closed candles for interval '1m', '5m', or '15m'."""
    if interval not in {"1m", "5m", "15m"}:
        raise ValueError("interval must be one of: 1m, 5m, 15m")
    limit = max(MIN_CANDLES, min(int(limit), 1000))

    try:
        response = requests.get(
            BASE_URL,
            params={"symbol": SYMBOL, "interval": interval, "limit": limit},
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": "kalshi-btc-bot/0.1"},
        )
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise CandleFeedError(f"Binance candle request failed for {interval}: {exc}") from exc

    if not isinstance(rows, list) or not rows:
        raise CandleFeedError(f"Binance returned no candle rows for {interval}.")

    # Binance kline fields: open time, open, high, low, close, volume,
    # close time, quote volume, trades, taker buy volume, taker buy quote, ignore.
    now_ms = int(time.time() * 1000)
    closed = [r for r in rows if isinstance(r, list) and len(r) >= 7 and int(r[6]) < now_ms]
    if len(closed) < 20:
        raise CandleFeedError(
            f"Only {len(closed)} closed {interval} candles returned; need at least 20."
        )

    data = []
    for row in closed:
        try:
            data.append({
                "timestamp": datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).isoformat(),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "_close_time_ms": int(row[6]),
            })
        except (TypeError, ValueError, OverflowError) as exc:
            raise CandleFeedError(f"Malformed {interval} candle returned by Binance.") from exc

    df = pd.DataFrame(data).sort_values("timestamp").reset_index(drop=True)
    if df[["open", "high", "low", "close", "volume"]].isna().any().any():
        raise CandleFeedError(f"Missing numeric values in {interval} candle data.")

    # Fail closed if the most recent fully closed candle is unexpectedly stale.
    interval_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000}[interval]
    age_ms = now_ms - int(df["_close_time_ms"].iloc[-1])
    if age_ms > interval_ms * 2 + 10_000:
        raise CandleFeedError(
            f"Latest {interval} candle is stale ({age_ms / 1000:.0f}s old)."
        )

    return df.drop(columns=["_close_time_ms"])


def fetch_signal_candles() -> dict[str, pd.DataFrame]:
    """Return the three closed-candle DataFrames required by signal_engine."""
    return {
        "candles_1m": fetch_closed_candles("1m", 100),
        "candles_5m": fetch_closed_candles("5m", 100),
        "candles_15m": fetch_closed_candles("15m", 100),
    }


if __name__ == "__main__":
    candles = fetch_signal_candles()
    for interval, frame in candles.items():
        print(
            f"{interval}: {len(frame)} closed candles; "
            f"latest={frame['timestamp'].iloc[-1]}; "
            f"close={frame['close'].iloc[-1]}"
        )
