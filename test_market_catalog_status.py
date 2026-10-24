from market_catalog_scanner import MarketCatalogScanner, _safe_market_row


def test_catalog_keeps_initialized_contracts_for_inventory_but_not_research_pool():
    scanner = MarketCatalogScanner(lambda **_kwargs: {"markets": [], "cursor": ""})
    try:
        active = _safe_market_row({
            "ticker": "KX-OPEN", "title": "Will team A win?", "category": "Sports",
            "status": "active", "volume_24h_fp": "1000", "open_interest_fp": "50",
        })
        initialized = _safe_market_row({
            "ticker": "KX-INIT", "title": "Will team B win?", "category": "Sports",
            "status": "initialized", "volume_24h_fp": "10000", "open_interest_fp": "500",
        })
        scanner._rank_analysis_candidate(initialized)
        scanner._rank_analysis_candidate(active)
        pool = scanner.analysis_snapshot()["analysis_candidates"]
        assert [row["ticker"] for row in pool] == ["KX-OPEN"]
    finally:
        scanner.reset_for_tests()
