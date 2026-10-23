"""Offline tests for per-source paper-trade performance bookkeeping."""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# Dashboard-route tests do not call these external/data providers, so keep the
# test isolated from the network and from optional worker dependencies.
class _ProviderError(Exception):
    pass

for _name, _attrs in {
    "market_discovery": {"discover": lambda: {"candidates": []}},
    "kalshi_api": {"get_market": lambda *_a, **_k: {}, "get_orderbook": lambda *_a, **_k: {}, "KalshiAPIError": _ProviderError},
    "candle_feed": {"fetch_signal_candles": lambda: {}, "fetch_closed_candles": lambda *_a, **_k: {}, "CandleFeedError": _ProviderError},
}.items():
    if _name not in sys.modules and not Path(__file__).with_name(f"{_name}.py").is_file():
        _module = types.ModuleType(_name)
        for _key, _value in _attrs.items():
            setattr(_module, _key, _value)
        sys.modules[_name] = _module

import db_store
from main import _aggregate_strategy_performance, _strategy_source_label


class StrategyPerformanceAuditTests(unittest.TestCase):
    def test_legacy_trade_notes_are_classified_without_inventing_unknown_source(self):
        self.assertEqual(_strategy_source_label("hybrid", "anything"), "hybrid")
        self.assertEqual(_strategy_source_label(None, "TradingView-primary hybrid paper entry; signal_id=8"), "hybrid")
        self.assertEqual(_strategy_source_label(None, "AUTO PAPER ENTRY; heuristic score is not a win probability"), "internal_legacy")
        self.assertEqual(_strategy_source_label(None, "unrecognized historical note"), "legacy_unknown")

    def test_performance_groups_use_net_pnl_sign_not_exit_reason_text(self):
        rows = [
            ("hybrid", "TradingView-primary hybrid paper entry", "2026-10-23T01:00:00+00:00", 23.8),
            ("hybrid", "TradingView-primary hybrid paper entry", "2026-10-23T01:10:00+00:00", -29.0),
            (None, "AUTO PAPER ENTRY; heuristic score is not a win probability", "2026-10-22T00:00:00+00:00", 219.0),
            (None, "AUTO PAPER ENTRY; heuristic score is not a win probability", "2026-10-22T00:15:00+00:00", -39.0),
            (None, "unknown note", None, None),
        ]
        groups = {row["source"]: row for row in _aggregate_strategy_performance(rows)}
        self.assertEqual(groups["hybrid"]["settled_trades"], 2)
        self.assertEqual(groups["hybrid"]["wins"], 1)
        self.assertEqual(groups["hybrid"]["losses"], 1)
        self.assertAlmostEqual(groups["hybrid"]["net_pnl_dollars"], -0.052)
        self.assertEqual(groups["internal_legacy"]["wins"], 1)
        self.assertEqual(groups["internal_legacy"]["losses"], 1)
        self.assertEqual(groups["legacy_unknown"]["open_positions"], 1)
        self.assertIn("Small sample", groups["hybrid"]["sample_note"])


    def test_dashboard_and_trade_ledger_expose_source_score_and_performance(self):
        from main import dashboard_data, paper_trades

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "dashboard-test.sqlite3")
            with patch.dict(os.environ, {"BOT_DB_PATH": db_path, "DATABASE_URL": "", "PAPER_SIGNAL_SOURCE": "hybrid"}):
                conn = db_store.connect_database()
                try:
                    db_store.ensure_schema(conn)
                    conn.execute(
                        """INSERT INTO paper_trades
                           (created_at, settled_at, market_ticker, direction, contracts, entry_cents,
                            fee_cents_per_contract, outcome, pnl_cents, notes, timeframe,
                            entry_signal_source, external_signal_id, entry_signal_log_id, entry_signal_score, entry_btc_price)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        ("2026-10-23T00:00:00+00:00", "2026-10-23T00:05:00+00:00", "KXBTC15M-TEST-A",
                         "DOWN", 1, 80.0, 2.0, "TREND_STOP_LOSS", -29.0,
                         "TradingView-primary hybrid paper entry; signal_id=10", "1m", "hybrid", 10, 42, 18, 81339.05),
                    )
                    conn.execute(
                        """INSERT INTO paper_trades
                           (created_at, settled_at, market_ticker, direction, contracts, entry_cents,
                            fee_cents_per_contract, outcome, pnl_cents, notes, timeframe)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        ("2026-10-22T00:00:00+00:00", "2026-10-22T00:05:00+00:00", "KXBTC15M-TEST-B",
                         "DOWN", 1, 25.0, 2.0, "DOWN", 219.0,
                         "AUTO PAPER ENTRY; heuristic score is not a win probability", "15m"),
                    )
                    conn.commit()
                finally:
                    conn.close()
                dashboard = dashboard_data()
                by_source = {r["source"]: r for r in dashboard["strategy_performance"]}
                self.assertEqual(by_source["hybrid"]["settled_trades"], 1)
                self.assertEqual(by_source["hybrid"]["losses"], 1)
                self.assertEqual(by_source["internal_legacy"]["wins"], 1)
                trade = {r["market_ticker"]: r for r in dashboard["trades"]}["KXBTC15M-TEST-A"]
                self.assertEqual(trade["entry_signal_score"], 18)
                self.assertEqual(trade["entry_signal_log_id"], 42)
                self.assertEqual(trade["market_implied_probability_percent"], 80.0)
                ledger = paper_trades(limit=10)["trades"]
                self.assertTrue(any(r["entry_signal_source"] == "hybrid" and r["entry_signal_score"] == 18 for r in ledger))

    def test_schema_migration_adds_signal_score_linkage_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "audit-test.sqlite3")
            with patch.dict(os.environ, {"BOT_DB_PATH": db_path, "DATABASE_URL": ""}):
                conn = db_store.connect_database()
                try:
                    db_store.ensure_schema(conn)
                    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
                    self.assertIn("entry_signal_log_id", columns)
                    self.assertIn("entry_signal_score", columns)
                    cur = conn.execute(
                        """INSERT INTO paper_trades
                           (created_at, market_ticker, direction, contracts, entry_cents, fee_cents_per_contract,
                            entry_signal_log_id, entry_signal_score)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        ("2026-10-23T00:00:00+00:00", "KXBTC15M-TEST", "DOWN", 1, 80.0, 2.0, 4, 18),
                    )
                    conn.commit()
                    row = conn.execute(
                        "SELECT entry_signal_log_id, entry_signal_score FROM paper_trades WHERE id = ?",
                        (cur.lastrowid,),
                    ).fetchone()
                    self.assertEqual(tuple(row), (4, 18))
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
