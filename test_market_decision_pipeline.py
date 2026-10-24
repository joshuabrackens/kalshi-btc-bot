from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from unittest.mock import patch

import db_store
from market_decision_pipeline import MarketDecisionPipeline


def _stamp(minutes_ago=1):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _model(market, _book, _required):
    category = market["category"]
    types = {
        "sports": ["injury_report", "lineup", "recent_news", "recent_stats"],
        "politics": ["official", "polling", "recent_news"],
        "economics": ["official_release", "consensus", "recent_news"],
        "weather": ["forecast", "observation"],
        "crypto_event": ["exchange_data", "official", "recent_news"],
        "financial_markets": ["price_data", "official", "recent_news"],
        "entertainment": ["official", "recent_news"],
    }.get(category, [])
    return {
        "market_ticker": market["ticker"], "model_probability_yes": 0.75,
        "model_id": "validated-test-model", "model_version": "test-v1", "as_of_utc": _stamp(),
        "validation": {"out_of_sample": True, "sample_count": 300, "brier_score": 0.16},
        "evidence": [{"type": k, "source": f"source-{k}", "url": f"https://evidence.example/{k}",
                      "published_at": _stamp(5), "retrieved_at": _stamp(),
                      "relevance_score": 0.95, "reliability_score": 0.95} for k in types],
    }


def _market(ticker="KXSPORTS-TEST", status="active"):
    return {"ticker": ticker, "title": "Will team A win?", "category": "sports", "status": status, "volume_24h_fp": "1000"}


def _book():
    return {"orderbook": {"yes": [[49, 15], [48, 10]], "no": [[49, 15], [48, 10]]}}


def test_pipeline_creates_separate_paper_trade_only_after_all_gates_and_deduplicates(tmp_path):
    with patch.dict(os.environ, {
        "DATABASE_URL": "", "BOT_DB_PATH": str(tmp_path / "pipeline.sqlite3"),
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze", "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token", "MARKET_AUTOPAPER_ENABLED": "true",
        "MARKET_PAPER_ENABLED_CATEGORIES": "sports", "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
        "MARKET_ANALYSIS_MAX_PER_CYCLE": "10",
    }, clear=False):
        def cat():
            return {"status": "complete", "analysis_candidates": [{"ticker": "KXSPORTS-TEST", "title": "Will team A win?", "category": "sports"}]}
        from runtime_controls import set_market_autopilot_enabled
        set_market_autopilot_enabled(True)
        pipeline = MarketDecisionPipeline(cat, lambda _t: {"market": _market()}, lambda _t: _book(), _model)
        try:
            first = pipeline.run_once_for_tests()
            assert first["status"] == "complete"
            assert first["paper_entries_created"] == 1
            assert first["live_orders_submitted"] == 0
            assert first["decisions"][0]["paper_trade_created"] is True
            second = pipeline.run_once_for_tests()
            assert second["paper_entries_created"] == 0
            conn = db_store.connect_database()
            try:
                count = conn.execute("SELECT COUNT(*) FROM market_research_paper_trades").fetchone()[0]
                assert count == 1
            finally:
                conn.close()
        finally:
            Path(tmp_path / "pipeline.sqlite3").unlink(missing_ok=True)
            Path(tmp_path / "pipeline.sqlite3-wal").unlink(missing_ok=True)
            Path(tmp_path / "pipeline.sqlite3-shm").unlink(missing_ok=True)


def test_pipeline_without_provider_never_fetches_book_or_creates_trade(tmp_path):
    with patch.dict(os.environ, {"DATABASE_URL": "", "BOT_DB_PATH": str(tmp_path / "no-provider.sqlite3"), "MARKET_ANALYSIS_PROVIDER_URL": "", "MARKET_AUTOPAPER_ENABLED": "true", "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false"}, clear=False):
        def fail(_ticker):
            raise AssertionError("must not call network when provider is absent")
        pipeline = MarketDecisionPipeline(lambda: {"status": "complete", "analysis_candidates": [{"ticker": "KXSPORTS-TEST", "category": "sports"}]}, fail, fail, _model)
        result = pipeline.run_once_for_tests()
        assert result["status"] == "provider_not_configured"
        assert result["paper_entries_created"] == 0
        assert result["live_orders_submitted"] == 0
        assert result["decisions"][0]["eligible"] is False


def test_pipeline_does_not_create_paper_trade_if_runtime_is_not_safe_paper(tmp_path):
    with patch.dict(os.environ, {
        "DATABASE_URL": "", "BOT_DB_PATH": str(tmp_path / "pipeline-block.sqlite3"),
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze", "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token", "MARKET_AUTOPAPER_ENABLED": "true",
        "MARKET_PAPER_ENABLED_CATEGORIES": "sports", "TRADING_MODE": "real", "LIVE_TRADING_ENABLED": "false",
    }, clear=False):
        pipeline = MarketDecisionPipeline(lambda: {"status": "complete", "analysis_candidates": [{"ticker": "KXSPORTS-TEST", "category": "sports"}]}, lambda _t: {"market": _market()}, lambda _t: _book(), _model)
        try:
            result = pipeline.run_once_for_tests()
            assert result["paper_entries_created"] == 0
            conn = db_store.connect_database()
            try:
                assert conn.execute("SELECT COUNT(*) FROM market_research_paper_trades").fetchone()[0] == 0
            finally:
                conn.close()
        finally:
            Path(tmp_path / "pipeline-block.sqlite3").unlink(missing_ok=True)
            Path(tmp_path / "pipeline-block.sqlite3-wal").unlink(missing_ok=True)
            Path(tmp_path / "pipeline-block.sqlite3-shm").unlink(missing_ok=True)


def test_pipeline_does_not_create_paper_trade_when_live_flag_is_true(tmp_path):
    with patch.dict(os.environ, {
        "DATABASE_URL": "", "BOT_DB_PATH": str(tmp_path / "live-flag.sqlite3"),
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze", "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token",
        "MARKET_AUTOPAPER_ENABLED": "true", "MARKET_PAPER_ENABLED_CATEGORIES": "sports",
        "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "true",
    }, clear=False):
        pipeline = MarketDecisionPipeline(
            lambda: {"status": "complete", "analysis_candidates": [{"ticker": "KXSPORTS-TEST", "category": "sports"}]},
            lambda _t: {"market": _market()}, lambda _t: _book(), _model,
        )
        result = pipeline.run_once_for_tests(paper_entry_once=True)
        assert result["paper_entries_created"] == 0
        assert result["live_orders_submitted"] == 0
        assert result["decisions"][0]["paper_trade_created"] is False


def test_pipeline_category_allowlist_blocks_before_market_book_or_model_calls(tmp_path):
    with patch.dict(os.environ, {
        "DATABASE_URL": "", "BOT_DB_PATH": str(tmp_path / "category.sqlite3"),
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze",
        "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token",
        "MARKET_AUTOPAPER_ENABLED": "true", "MARKET_PAPER_ENABLED_CATEGORIES": "politics",
        "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
    }, clear=False):
        calls = {"market": 0, "book": 0, "model": 0}
        def forbidden_market(_ticker):
            calls["market"] += 1
            raise AssertionError("disabled category must not fetch market detail")
        def forbidden_book(_ticker):
            calls["book"] += 1
            raise AssertionError("disabled category must not fetch orderbook")
        def forbidden_model(_market, _book_data, _required):
            calls["model"] += 1
            raise AssertionError("disabled category must not call paid analysis provider")
        pipeline = MarketDecisionPipeline(
            lambda: {"status": "complete", "analysis_candidates": [
                {"ticker": "KXSPORTS-TEST", "category": "sports"}
            ]},
            forbidden_market, forbidden_book, forbidden_model,
        )
        result = pipeline.run_once_for_tests()
        assert result["status"] == "complete"
        assert result["paper_entries_created"] == 0
        assert result["analyzed_count"] == 0
        assert result["blocked_count"] == 1
        assert result["decisions"] == []
        assert calls == {"market": 0, "book": 0, "model": 0}



def test_end_to_end_catalog_scan_to_automatic_research_paper_entry(tmp_path):
    """The autopilot pipeline consumes a paginated inventory result and records a paper entry only after gates pass."""
    from market_catalog_scanner import MarketCatalogScanner
    from runtime_controls import set_market_autopilot_enabled

    with patch.dict(os.environ, {
        "DATABASE_URL": "", "BOT_DB_PATH": str(tmp_path / "scan-to-paper.sqlite3"),
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze", "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token",
        "MARKET_AUTOPAPER_ENABLED": "true", "MARKET_PAPER_ENABLED_CATEGORIES": "sports",
        "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
        "MARKET_ANALYSIS_MAX_PER_CYCLE": "10", "MARKET_RESEARCH_MAX_NEW_TRADES_PER_RUN": "5",
    }, clear=False):
        markets = [{
            "ticker": "KXSPORTS-AUTO-TEST", "title": "Will team A win?", "category": "Sports",
            "status": "open", "volume_24h_fp": "1000", "open_interest_fp": "200",
        }]
        scanner = MarketCatalogScanner(lambda **kwargs: {"markets": markets, "cursor": ""})
        scanner._page_delay = 0
        try:
            scanner.start(force=True)
            deadline = __import__("time").monotonic() + 3
            while __import__("time").monotonic() < deadline and scanner.snapshot()["status"] == "running":
                __import__("time").sleep(0.005)
            catalog = scanner.analysis_snapshot()
            assert catalog["status"] == "complete"
            assert catalog["candidate_count"] == 1
            set_market_autopilot_enabled(True)
            pipeline = MarketDecisionPipeline(
                catalog_snapshot=scanner.analysis_snapshot,
                market_fetcher=lambda ticker: {"market": _market(ticker=ticker)},
                orderbook_fetcher=lambda _ticker: _book(),
                model_provider=_model,
            )
            result = pipeline.run_once_for_tests()
            assert result["status"] == "complete"
            assert result["analyzed_count"] == 1
            assert result["paper_entries_created"] == 1
            assert result["live_orders_submitted"] == 0
            conn = db_store.connect_database()
            try:
                row = conn.execute("SELECT market_ticker,status FROM market_research_paper_trades").fetchone()
                assert row[0] == "KXSPORTS-AUTO-TEST"
                assert row[1] == "open"
            finally:
                conn.close()
        finally:
            scanner.reset_for_tests()
            # Restore the runtime switch to OFF after the isolated test.
            try:
                set_market_autopilot_enabled(False)
            except Exception:
                pass
            for suffix in ("", "-wal", "-shm"):
                Path(str(tmp_path / "scan-to-paper.sqlite3") + suffix).unlink(missing_ok=True)



def test_top_ranked_eligible_candidates_open_and_settlement_pnl_is_recorded(tmp_path):
    with patch.dict(os.environ, {
        "DATABASE_URL": "", "BOT_DB_PATH": str(tmp_path / "best-market.sqlite3"),
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze",
        "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token",
        "MARKET_AUTOPAPER_ENABLED": "true", "MARKET_PAPER_ENABLED_CATEGORIES": "sports",
        "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
        "MARKET_ANALYSIS_MAX_PER_CYCLE": "10",
        "MARKET_RESEARCH_MAX_OPEN_POSITIONS": "5",
        "MARKET_RESEARCH_MAX_ENTRIES_PER_CYCLE": "3",
    }, clear=False):
        from runtime_controls import set_market_autopilot_enabled
        set_market_autopilot_enabled(True)

        markets = [
            {"ticker": "KXSPORTS-LIQUID", "title": "High liquidity lower edge", "category": "sports"},
            {"ticker": "KXSPORTS-EDGE", "title": "Higher edge candidate", "category": "sports"},
        ]

        def catalog():
            return {"status": "complete", "analysis_candidates": markets}

        def fetch_market(ticker):
            volume = "1000000" if ticker == "KXSPORTS-LIQUID" else "50"
            return {"market": {
                **_market(ticker=ticker),
                "volume_24h_fp": volume,
            }}

        def fetch_book(ticker):
            depth = 1000 if ticker == "KXSPORTS-LIQUID" else 5
            return {"orderbook": {
                "yes": [[49, depth], [48, depth]],
                "no": [[49, depth], [48, depth]],
            }}

        def model_with_different_odds(market, book, required):
            result = _model(market, book, required)
            result["model_probability_yes"] = 0.75 if market["ticker"] == "KXSPORTS-LIQUID" else 0.95
            return result

        pipeline = MarketDecisionPipeline(catalog, fetch_market, fetch_book, model_with_different_odds)
        try:
            result = pipeline.run_once_for_tests()
            assert result["status"] == "complete"
            assert result["analyzed_count"] == 2
            assert result["paper_entries_created"] == 2
            assert result["live_orders_submitted"] == 0
            ranked = {row["ticker"]: row for row in result["decisions"]}
            assert ranked["KXSPORTS-EDGE"]["paper_selection"]["rank"] == 1
            assert ranked["KXSPORTS-EDGE"]["paper_trade_created"] is True
            assert ranked["KXSPORTS-LIQUID"]["paper_selection"]["rank"] == 2
            assert ranked["KXSPORTS-LIQUID"]["paper_trade_created"] is True

            conn = db_store.connect_database()
            try:
                rows = conn.execute(
                    """SELECT market_ticker,status,entry_cents,estimated_fee_cents,
                              estimated_slippage_cents,expected_net_edge_cents,source_snapshot_json
                       FROM market_research_paper_trades ORDER BY id"""
                ).fetchall()
                assert len(rows) == 2
                by_ticker = {row[0]: row for row in rows}
                best = by_ticker["KXSPORTS-EDGE"]
                assert best[1] == "open"
                assert best[2] == 51.0
                assert best[3] == 2.0
                assert best[4] == 1.0
                assert best[5] == 41.0
                saved = json.loads(best[6])["decision"]
                assert saved["paper_selection"]["rank"] == 1
                assert saved["paper_selection"]["rank_score_is_probability"] is False
                assert saved["paper_trade_created"] is True
                assert saved["paper_selection_status"] == "paper_position_opened"
                assert saved["paper_trade_block_reason"] is None
                assert saved["paper_entry_gate"]["environment_safe"] is True
                assert by_ticker["KXSPORTS-LIQUID"][1] == "open"

                # Only official final statuses/results can settle a research simulation.
                pipeline._market_fetcher = lambda ticker: {
                    "market": {"ticker": ticker, "status": "settled", "result": "YES"}
                }
                pipeline._settle_open_trades(conn)
                settled = conn.execute(
                    """SELECT status,result,pnl_cents,settled_at
                       FROM market_research_paper_trades ORDER BY id"""
                ).fetchall()
                assert len(settled) == 2
                assert all(row[0] == "settled" and row[1] == "YES" for row in settled)
                assert all(row[2] == 46.0 for row in settled)
                assert all(row[3] for row in settled)
                assert conn.execute(
                    "SELECT COUNT(*) FROM market_research_paper_trades WHERE status='open'"
                ).fetchone()[0] == 0
            finally:
                conn.close()
        finally:
            set_market_autopilot_enabled(False)
            for suffix in ("", "-wal", "-shm"):
                Path(str(tmp_path / "best-market.sqlite3") + suffix).unlink(missing_ok=True)

def test_settlement_only_monitor_resolves_open_position_with_autopilot_off(tmp_path):
    with patch.dict(os.environ, {
        "DATABASE_URL": "", "BOT_DB_PATH": str(tmp_path / "settlement-monitor.sqlite3"),
        "MARKET_ANALYSIS_PROVIDER_URL": "", "MARKET_AUTOPAPER_ENABLED": "true",
        "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
    }, clear=False):
        from runtime_controls import set_market_autopilot_enabled
        set_market_autopilot_enabled(False)
        conn = db_store.connect_database()
        db_store.ensure_schema(conn)
        conn.execute(
            """INSERT INTO market_research_paper_trades
               (created_at,market_ticker,category,side,contracts,entry_cents,estimated_fee_cents,
                estimated_slippage_cents,model_probability_yes,model_id,model_version,
                expected_net_edge_cents,status,source_snapshot_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("2026-10-24T08:00:00+00:00", "KXSPORTS-SETTLE-MONITOR", "sports", "YES", 1,
             52.0, 2.0, 1.0, 0.8, "test-model", "test-v1", 20.0, "open", "{}"),
        )
        conn.commit()
        conn.close()

        def final_market(ticker):
            assert ticker == "KXSPORTS-SETTLE-MONITOR"
            # Official final results may be exposed as closed + settlement_result.
            return {"market": {"ticker": ticker, "status": "closed", "settlement_result": "YES"}}

        def no_model(*_args, **_kwargs):
            raise AssertionError("settlement-only monitoring must not invoke the model/evidence provider")

        pipeline = MarketDecisionPipeline(
            lambda: {"status": "complete", "analysis_candidates": []},
            final_market,
            lambda _ticker: {},
            no_model,
        )
        try:
            result = pipeline.settle_open_positions_only()
            assert result == {"checked": 1, "settled": 1, "open_before": 1, "open_after": 0, "errors": 0}
            conn = db_store.connect_database()
            try:
                row = conn.execute(
                    """SELECT status,result,pnl_cents,settled_at
                       FROM market_research_paper_trades WHERE market_ticker='KXSPORTS-SETTLE-MONITOR'"""
                ).fetchone()
                assert row[0] == "settled"
                assert row[1] == "YES"
                assert row[2] == 45.0  # 100¢ payout - 52¢ entry - 2¢ fee - 1¢ slippage
                assert row[3]
            finally:
                conn.close()
        finally:
            set_market_autopilot_enabled(False)
            for suffix in ("", "-wal", "-shm"):
                Path(str(tmp_path / "settlement-monitor.sqlite3") + suffix).unlink(missing_ok=True)


def test_manual_research_batch_can_open_one_paper_position_with_autopilot_off(tmp_path):
    with patch.dict(os.environ, {
        "DATABASE_URL": "", "BOT_DB_PATH": str(tmp_path / "manual-one-shot.sqlite3"),
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze",
        "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token",
        "MARKET_AUTOPAPER_ENABLED": "true", "MARKET_PAPER_ENABLED_CATEGORIES": "sports",
        "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
    }, clear=False):
        from runtime_controls import set_market_autopilot_enabled
        set_market_autopilot_enabled(False)
        pipeline = MarketDecisionPipeline(
            lambda: {"status": "complete", "analysis_candidates": [
                {"ticker": "KXSPORTS-MANUAL", "title": "Manual one-shot test", "category": "sports"}
            ]},
            lambda ticker: {"market": _market(ticker=ticker)},
            lambda _ticker: _book(),
            _model,
        )
        try:
            result = pipeline.run_once_for_tests(paper_entry_once=True)
            assert result["paper_entries_created"] == 1
            assert result["decisions"][0]["paper_trade_created"] is True
            assert result["decisions"][0]["paper_entry_gate"]["manual_one_shot_requested"] is True
            conn = db_store.connect_database()
            try:
                assert conn.execute("SELECT COUNT(*) FROM market_research_paper_trades WHERE status='open'").fetchone()[0] == 1
            finally:
                conn.close()
        finally:
            set_market_autopilot_enabled(False)
            for suffix in ("", "-wal", "-shm"):
                Path(str(tmp_path / "manual-one-shot.sqlite3") + suffix).unlink(missing_ok=True)

def test_pipeline_opens_multiple_top_ranked_research_paper_positions(tmp_path):
    db_path = tmp_path / "multi-position.sqlite3"
    tickers = ["KXSPORTS-MULTI-A", "KXSPORTS-MULTI-B", "KXSPORTS-MULTI-C", "KXSPORTS-MULTI-D"]
    with patch.dict(os.environ, {
        "DATABASE_URL": "", "BOT_DB_PATH": str(db_path),
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze",
        "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token",
        "MARKET_AUTOPAPER_ENABLED": "true", "MARKET_PAPER_ENABLED_CATEGORIES": "sports",
        "MARKET_RESEARCH_MAX_OPEN_POSITIONS": "5", "MARKET_RESEARCH_MAX_ENTRIES_PER_CYCLE": "3",
        "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
    }, clear=False):
        from runtime_controls import set_market_autopilot_enabled
        set_market_autopilot_enabled(True)
        pipeline = MarketDecisionPipeline(
            lambda: {"status": "complete", "analysis_candidates": [
                {"ticker": ticker, "title": f"Will team win? {ticker}", "category": "sports"}
                for ticker in tickers
            ]},
            lambda ticker: {"market": _market(ticker=ticker)},
            lambda _ticker: _book(),
            _model,
        )
        try:
            result = pipeline.run_once_for_tests()
            assert result["status"] == "complete"
            assert result["paper_entries_created"] == 3
            assert result["live_orders_submitted"] == 0
            assert sum(bool(row.get("paper_trade_created")) for row in result["decisions"]) == 3
            opened = [row for row in result["decisions"] if row.get("paper_trade_created")]
            assert all(row["paper_selection"]["selected_for_attempt"] for row in opened)
            conn = db_store.connect_database()
            try:
                assert conn.execute("SELECT COUNT(*) FROM market_research_paper_trades WHERE status='open'").fetchone()[0] == 3
                assert conn.execute("SELECT COUNT(DISTINCT market_ticker) FROM market_research_paper_trades").fetchone()[0] == 3
            finally:
                conn.close()
        finally:
            set_market_autopilot_enabled(False)
            for suffix in ("", "-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)

def test_exploratory_paper_mode_opens_unvalidated_prediction_for_accuracy_tracking(tmp_path):
    db_path = tmp_path / "exploratory-paper.sqlite3"
    with patch.dict(os.environ, {
        "DATABASE_URL": "", "BOT_DB_PATH": str(db_path),
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze",
        "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token",
        "MARKET_AUTOPAPER_ENABLED": "true", "MARKET_EXPLORATORY_PAPER_ENABLED": "true",
        "MARKET_PAPER_ENABLED_CATEGORIES": "sports",
        "MARKET_RESEARCH_MAX_OPEN_POSITIONS": "5", "MARKET_RESEARCH_MAX_ENTRIES_PER_CYCLE": "3",
        "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
    }, clear=False):
        from runtime_controls import set_market_autopilot_enabled
        set_market_autopilot_enabled(True)

        def unvalidated_model(market, _book, _required):
            return {
                "market_ticker": market["ticker"], "model_probability_yes": 0.90,
                "model_id": "exploratory-test-model", "model_version": "exploratory-v1",
                "as_of_utc": _stamp(),
                "validation": {"out_of_sample": False, "sample_count": 0},
                "evidence": [],
            }

        pipeline = MarketDecisionPipeline(
            lambda: {"status": "complete", "analysis_candidates": [
                {"ticker": "KXSPORTS-EXPLORATORY", "title": "Exploratory paper test", "category": "sports"}
            ]},
            lambda ticker: {"market": _market(ticker=ticker)},
            lambda _ticker: _book(),
            unvalidated_model,
        )
        try:
            result = pipeline.run_once_for_tests()
            assert result["status"] == "complete"
            assert result["paper_entries_created"] == 1
            assert result["live_orders_submitted"] == 0
            decision = result["decisions"][0]
            assert decision["status"] == "exploratory_paper_candidate"
            assert decision["experimental_paper_mode"] is True
            assert decision["model_validation_status"] == "UNVALIDATED_EXPLORATORY"
            assert decision["model_probability_yes_pct"] == 90.0
            assert decision["selected_side"] == "YES"
            assert decision["paper_trade_created"] is True
            assert "required_evidence_missing:injury_report,lineup,recent_news,recent_stats" in decision["exploratory_validation_warnings"]

            conn = db_store.connect_database()
            try:
                row = conn.execute(
                    "SELECT status,model_probability_yes,source_snapshot_json FROM market_research_paper_trades"
                ).fetchone()
                assert row[0] == "open"
                assert row[1] == 0.9
                saved = json.loads(row[2])["decision"]
                assert saved["experimental_paper_mode"] is True
                assert saved["model_validation_status"] == "UNVALIDATED_EXPLORATORY"
                assert saved["paper_trade_created"] is True
                assert saved["paper_selection_status"] == "paper_position_opened"
                assert saved["paper_trade_block_reason"] is None
                assert saved["paper_entry_gate"]["environment_safe"] is True
            finally:
                conn.close()
        finally:
            set_market_autopilot_enabled(False)
            for suffix in ("", "-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)


def test_exploratory_paper_mode_still_rejects_closed_or_unsafe_markets(tmp_path):
    db_path = tmp_path / "exploratory-closed.sqlite3"
    with patch.dict(os.environ, {
        "DATABASE_URL": "", "BOT_DB_PATH": str(db_path),
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze",
        "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token",
        "MARKET_AUTOPAPER_ENABLED": "true", "MARKET_EXPLORATORY_PAPER_ENABLED": "true",
        "MARKET_PAPER_ENABLED_CATEGORIES": "sports",
        "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
    }, clear=False):
        from runtime_controls import set_market_autopilot_enabled
        set_market_autopilot_enabled(True)

        def unvalidated_model(market, _book, _required):
            return {
                "market_ticker": market["ticker"], "model_probability_yes": 0.90,
                "model_id": "exploratory-test-model", "model_version": "exploratory-v1",
                "as_of_utc": _stamp(), "validation": {"out_of_sample": False, "sample_count": 0},
                "evidence": [],
            }

        pipeline = MarketDecisionPipeline(
            lambda: {"status": "complete", "analysis_candidates": [
                {"ticker": "KXSPORTS-CLOSED", "title": "Closed-market test", "category": "sports"}
            ]},
            lambda ticker: {"market": _market(ticker=ticker, status="closed")},
            lambda _ticker: _book(),
            unvalidated_model,
        )
        try:
            result = pipeline.run_once_for_tests()
            assert result["paper_entries_created"] == 0
            assert result["live_orders_submitted"] == 0
            assert result["decisions"][0]["eligible"] is False
            conn = db_store.connect_database()
            try:
                assert conn.execute("SELECT COUNT(*) FROM market_research_paper_trades").fetchone()[0] == 0
            finally:
                conn.close()
        finally:
            set_market_autopilot_enabled(False)
            for suffix in ("", "-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)

def test_market_analysis_error_diagnostics_are_allowlisted_and_secret_safe():
    from market_analysis_provider import MarketAnalysisProviderError
    from market_decision_pipeline import _safe_market_analysis_error

    assert _safe_market_analysis_error(MarketAnalysisProviderError("probability_provider_http_429")) == "probability_provider_http_429"
    assert _safe_market_analysis_error(MarketAnalysisProviderError("probability_provider_http_401")) == "probability_provider_http_401"
    assert _safe_market_analysis_error(MarketAnalysisProviderError("probability_provider_network_error:ConnectionError")) == "probability_provider_network_error:ConnectionError"
    assert _safe_market_analysis_error(MarketAnalysisProviderError("Authorization: Bearer do-not-log-this")) == "MarketAnalysisProviderError"
    assert _safe_market_analysis_error(RuntimeError("api_key=do-not-log-this")) == "RuntimeError"

def test_research_pipeline_commits_before_calling_slow_model_provider(tmp_path, monkeypatch):
    import market_decision_pipeline as pipeline_module

    db_path = tmp_path / "short-transaction.sqlite3"
    with patch.dict(os.environ, {
        "DATABASE_URL": "", "BOT_DB_PATH": str(db_path),
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze",
        "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token",
        "MARKET_AUTOPAPER_ENABLED": "true", "MARKET_PAPER_ENABLED_CATEGORIES": "sports",
        "MARKET_EXPLORATORY_PAPER_ENABLED": "false",
        "MARKET_RESEARCH_MAX_OPEN_POSITIONS": "5", "MARKET_RESEARCH_MAX_ENTRIES_PER_CYCLE": "3",
        "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
    }, clear=False):
        from runtime_controls import set_market_autopilot_enabled
        original_connect = pipeline_module.connect_database
        tracked_connections = []

        class CommitTrackingConnection:
            def __init__(self, raw):
                self.raw = raw
                self.commit_count = 0

            def __getattr__(self, name):
                return getattr(self.raw, name)

            def commit(self):
                self.commit_count += 1
                return self.raw.commit()

        def tracked_connect():
            conn = CommitTrackingConnection(original_connect())
            tracked_connections.append(conn)
            return conn

        monkeypatch.setattr(pipeline_module, "connect_database", tracked_connect)
        set_market_autopilot_enabled(True)

        def model_after_commits(market, book, required):
            assert tracked_connections
            # The settlement/outcome checks must be committed before slow provider calls.
            assert tracked_connections[0].commit_count >= 2
            return _model(market, book, required)

        pipeline = MarketDecisionPipeline(
            lambda: {"status": "complete", "analysis_candidates": [
                {"ticker": "KXSPORTS-SHORT-TX", "title": "Short transaction test", "category": "sports"}
            ]},
            lambda ticker: {"market": _market(ticker=ticker)},
            lambda _ticker: _book(),
            model_after_commits,
        )
        try:
            result = pipeline.run_once_for_tests()
            assert result["status"] == "complete"
            assert result["paper_entries_created"] == 1
        finally:
            set_market_autopilot_enabled(False)
            for suffix in ("", "-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)


def test_research_paper_entry_retries_deadlock_without_recalling_model():
    class DeadlockDetected(Exception):
        pass

    class FakeConnection:
        def __init__(self):
            self.commit_attempts = 0
            self.rollbacks = 0

        def commit(self):
            self.commit_attempts += 1
            if self.commit_attempts == 1:
                raise DeadlockDetected("simulated transaction conflict")

        def rollback(self):
            self.rollbacks += 1

    pipeline = MarketDecisionPipeline(lambda: {}, lambda _t: {}, lambda _t: {}, lambda *_: {})
    create_attempts = []

    def create_once_per_attempt(_conn, _result, *, allow_manual_entry_once=False):
        create_attempts.append(allow_manual_entry_once)
        return True, None

    pipeline._create_paper_trade = create_once_per_attempt
    conn = FakeConnection()
    created, reason = pipeline._create_paper_trade_with_retry(conn, {"ticker": "KXSPORTS-RETRY"})
    assert created is True
    assert reason is None
    assert len(create_attempts) == 2
    assert conn.commit_attempts == 2
    assert conn.rollbacks == 1


def test_research_decision_journal_retries_deadlock_from_cached_decisions():
    class DeadlockDetected(Exception):
        pass

    class FakeConnection:
        def __init__(self):
            self.commit_attempts = 0
            self.rollbacks = 0

        def commit(self):
            self.commit_attempts += 1
            if self.commit_attempts == 1:
                raise DeadlockDetected("simulated transaction conflict")

        def rollback(self):
            self.rollbacks += 1

    pipeline = MarketDecisionPipeline(lambda: {}, lambda _t: {}, lambda _t: {}, lambda *_: {})
    writes = []

    def record_without_network(_conn, item):
        writes.append(item["ticker"])

    pipeline._record_decision = record_without_network
    conn = FakeConnection()
    pipeline._record_decisions_with_retry(conn, [{"ticker": "A"}, {"ticker": "B"}])
    assert writes == ["A", "B", "A", "B"]
    assert conn.commit_attempts == 2
    assert conn.rollbacks == 1

