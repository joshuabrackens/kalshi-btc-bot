from datetime import datetime, timedelta, timezone
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
        result = pipeline.run_once_for_tests()
        assert result["paper_entries_created"] == 0
        assert result["live_orders_submitted"] == 0
        assert result["decisions"][0]["paper_trade_created"] is False


def test_pipeline_category_allowlist_is_required_for_research_paper(tmp_path):
    with patch.dict(os.environ, {
        "DATABASE_URL": "", "BOT_DB_PATH": str(tmp_path / "category.sqlite3"),
        "MARKET_ANALYSIS_PROVIDER_URL": "https://model.example/analyze", "MARKET_ANALYSIS_PROVIDER_API_KEY": "test-provider-token",
        "MARKET_AUTOPAPER_ENABLED": "true", "MARKET_PAPER_ENABLED_CATEGORIES": "politics",
        "TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false",
    }, clear=False):
        pipeline = MarketDecisionPipeline(
            lambda: {"status": "complete", "analysis_candidates": [{"ticker": "KXSPORTS-TEST", "category": "sports"}]},
            lambda _t: {"market": _market()}, lambda _t: _book(), _model,
        )
        result = pipeline.run_once_for_tests()
        assert result["paper_entries_created"] == 0
        assert result["decisions"][0]["eligible"] is False
        assert result["decisions"][0]["status"] == "category_paper_disabled"


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
