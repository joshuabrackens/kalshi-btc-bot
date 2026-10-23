"""Regression tests for informational WAIT webhook events (paper-only)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

# This coordinated package intentionally does not ship every project-specific
# connector module. Stub missing market/feed connectors only; these tests never
# access external services or place any orders.
def _install_missing_stub(module_name, **attrs):
    if module_name in sys.modules:
        return
    # Prefer real project source files over process-global placeholder modules.
    if Path(__file__).with_name(f"{module_name}.py").is_file():
        return
    try:
        spec = importlib.util.find_spec(module_name)
    except (ValueError, ModuleNotFoundError):
        spec = None
    if spec is None:
        mod = types.ModuleType(module_name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[module_name] = mod

class _PlaceholderError(Exception):
    pass

_install_missing_stub("market_discovery", discover=lambda: {"candidates": []})
_install_missing_stub("kalshi_api", get_market=lambda *_a, **_k: {}, get_orderbook=lambda *_a, **_k: {}, KalshiAPIError=_PlaceholderError)
_install_missing_stub("candle_feed", fetch_signal_candles=lambda: {}, fetch_closed_candles=lambda *_a, **_k: {}, CandleFeedError=_PlaceholderError)
_install_missing_stub("trend_stop", update_trend_stop=lambda *_a, **_k: {})

from main import _normalize_webhook_direction
import paper_worker
from db_store import connect_database, ensure_schema


class WaitAlertTests(unittest.TestCase):
    def test_webhook_normalizes_wait_without_breaking_directions(self):
        self.assertEqual(_normalize_webhook_direction({"direction": "WAIT"}), "WAIT")
        self.assertEqual(_normalize_webhook_direction({"signal": "WAIT THIS WINDOW"}), "WAIT")
        self.assertEqual(_normalize_webhook_direction({"action": "NO_TRADE"}), "WAIT")
        self.assertEqual(_normalize_webhook_direction({"direction": "UP"}), "UP")
        self.assertEqual(_normalize_webhook_direction({"signal": "BUY_DOWN"}), "DOWN")

    def test_wait_event_is_recorded_and_never_opens_trade(self):
        old = {key: os.environ.get(key) for key in (
            "DATABASE_URL", "BOT_DB_PATH", "PAPER_SIGNAL_SOURCE", "TRADING_MODE",
            "LIVE_TRADING_ENABLED", "PAPER_SHARED_SQLITE_OK", "PAPER_MINIMUM_SCORE", "MAX_TRADE_DOLLARS",
            "MAX_DAILY_LOSS_DOLLARS",
        )}
        try:
            os.environ.pop("DATABASE_URL", None)
            os.environ["PAPER_SIGNAL_SOURCE"] = "hybrid"
            os.environ["TRADING_MODE"] = "paper"
            os.environ["LIVE_TRADING_ENABLED"] = "false"
            os.environ["PAPER_SHARED_SQLITE_OK"] = "true"
            os.environ["MAX_TRADE_DOLLARS"] = "1.00"
            os.environ["MAX_DAILY_LOSS_DOLLARS"] = "10.00"
            with tempfile.TemporaryDirectory() as temp:
                os.environ["BOT_DB_PATH"] = str(Path(temp) / "wait-test.sqlite3")
                conn = connect_database()
                ensure_schema(conn)
                conn.execute(
                    """INSERT INTO gainzalgo_signals
                       (created_at,direction,symbol,timeframe,payload_json)
                       VALUES (?,?,?,?,?)""",
                    (datetime.now(timezone.utc).isoformat(), "WAIT", "BTCUSDT", "1m",
                     json.dumps({"direction": "WAIT", "symbol": "BTCUSDT", "timeframe": "1m"})),
                )
                conn.commit()
                conn.close()

                # WAIT processing should not depend on live providers. Always patch
                # the already-imported function, even when candle_feed is installed.
                with patch.object(paper_worker, "_manage_open", return_value=None), \
                     patch.object(paper_worker, "_refresh_btc_trend_monitor", return_value={
                         "status": "ok", "monitor_only": True, "updated_at": datetime.now(timezone.utc).isoformat(),
                         "candidate_bias": "MIXED", "evidence_score": 0, "timeframes": {},
                     }):
                    result = paper_worker.run_once()

                self.assertEqual(result.get("action"), "wait")
                self.assertFalse(result.get("trade_placed", True))
                conn = connect_database()
                ensure_schema(conn)
                alert = conn.execute(
                    "SELECT processing_status,processing_note FROM gainzalgo_signals ORDER BY id DESC LIMIT 1"
                ).fetchone()
                trade_count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
                conn.close()
                self.assertEqual(alert[0], "wait")
                self.assertIn("no paper trade was opened", str(alert[1]).lower())
                self.assertEqual(trade_count, 0)
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
