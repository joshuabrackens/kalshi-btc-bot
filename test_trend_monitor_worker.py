"""Worker-level test with stubbed network providers; no real market/order calls."""
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path
import unittest
from unittest.mock import patch

import pandas as pd


def _candles(n, interval, drift):
    close = [81000.0 + drift * i for i in range(n)]
    open_ = [close[0]] + close[:-1]
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-10-23T00:00:00Z", periods=n, freq=f"{interval}min"),
        "open": open_,
        "high": [max(a, b) + 2 for a, b in zip(open_, close)],
        "low": [min(a, b) - 2 for a, b in zip(open_, close)],
        "close": close,
        "volume": [100.0 + (i % 3) for i in range(n)],
    })


_provider_calls = {"count": 0}


def _fetch_signal_candles():
    _provider_calls["count"] += 1
    return {
        "candles_1m": _candles(100, 1, 1.2),
        "candles_5m": _candles(50, 5, 2.0),
        "candles_15m": _candles(40, 15, 4.0),
    }


def _stub_if_missing(name, **attrs):
    if name in sys.modules:
        return
    # The project source in this directory must win over a process-wide test stub.
    if Path(__file__).with_name(f"{name}.py").is_file():
        return
    try:
        spec = importlib.util.find_spec(name)
    except (ValueError, ModuleNotFoundError):
        spec = None
    if spec is None:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod

_stub_if_missing("candle_feed", fetch_signal_candles=_fetch_signal_candles,
                 fetch_closed_candles=lambda *_a, **_k: None, CandleFeedError=RuntimeError)
_stub_if_missing("trend_stop", update_trend_stop=lambda *_a, **_k: {})
_stub_if_missing("market_discovery", discover=lambda: {"candidates": []})

import paper_worker  # noqa: E402
from db_store import connect_database, ensure_schema  # noqa: E402


class TrendMonitorWorkerTests(unittest.TestCase):
    def test_monitor_persists_shared_snapshot_and_reuses_fresh_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db = os.environ.get("BOT_DB_PATH")
            old_url = os.environ.get("DATABASE_URL")
            old_interval = os.environ.get("BTC_TREND_MONITOR_INTERVAL_SECONDS")
            os.environ["DATABASE_URL"] = ""
            os.environ["BOT_DB_PATH"] = os.path.join(tmp, "journal.sqlite3")
            os.environ["BTC_TREND_MONITOR_INTERVAL_SECONDS"] = "30"
            try:
                paper_worker._LATEST_SIGNAL_BUNDLE = None
                paper_worker._LATEST_SIGNAL_BUNDLE_AT = None
                conn = connect_database()
                ensure_schema(conn)
                start_calls = _provider_calls["count"]
                # Always patch the real imported provider, even when candle_feed
                # is installed. Unit tests must never make public network calls.
                with patch.object(paper_worker, "fetch_signal_candles", side_effect=_fetch_signal_candles):
                    snapshot = paper_worker._refresh_btc_trend_monitor(conn)
                    self.assertEqual(snapshot["status"], "ok")
                self.assertTrue(snapshot["monitor_only"])
                self.assertEqual(_provider_calls["count"], start_calls + 1)
                stored = conn.execute("SELECT status, snapshot_json FROM btc_trend_monitor WHERE monitor_key=?", ("btc_15m",)).fetchone()
                self.assertEqual(stored[0], "ok")
                self.assertIn('"timeframes"', stored[1])
                # A fresh cached snapshot should not force a second provider request.
                with patch.object(paper_worker, "fetch_signal_candles", side_effect=_fetch_signal_candles):
                    second = paper_worker._refresh_btc_trend_monitor(conn)
                    self.assertEqual(second["status"], "ok")
                self.assertEqual(_provider_calls["count"], start_calls + 1)
                conn.close()
            finally:
                if old_db is None:
                    os.environ.pop("BOT_DB_PATH", None)
                else:
                    os.environ["BOT_DB_PATH"] = old_db
                if old_url is None:
                    os.environ.pop("DATABASE_URL", None)
                else:
                    os.environ["DATABASE_URL"] = old_url
                if old_interval is None:
                    os.environ.pop("BTC_TREND_MONITOR_INTERVAL_SECONDS", None)
                else:
                    os.environ["BTC_TREND_MONITOR_INTERVAL_SECONDS"] = old_interval


if __name__ == "__main__":
    unittest.main()
