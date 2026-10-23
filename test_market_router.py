from market_router import classify_and_route, scan_market_catalog


def test_only_validated_btc_market_routes_to_btc_paper_strategy():
    result = classify_and_route({"ticker": "KXBTC15M-TEST", "title": "Bitcoin price up in 15 minutes", "category": "Crypto"})
    assert result["route_status"] == "btc_paper_strategy_only"
    assert result["strategy_key"] == "btc_15m"
    assert result["paper_strategy_enabled"] is True
    assert result["trade_submitted"] is False


def test_non_btc_and_unknown_markets_never_route_to_btc_strategy():
    sports = classify_and_route({"ticker": "KXSPORTS-TEST", "title": "Team wins game", "category": "Sports"})
    unknown = classify_and_route({"ticker": "MYSTERY-TEST", "title": "Unknown event"})
    assert sports["route_status"] == "research_only"
    assert sports["strategy_key"] is None
    assert unknown["route_status"] == "blocked_unclassified"
    assert unknown["strategy_key"] is None
    assert not sports["trade_submitted"] and not unknown["trade_submitted"]


def test_catalog_scan_is_bounded_and_marks_all_candidates_non_executing():
    markets = [{"ticker": f"MYSTERY-{i}", "title": "Unclassified market"} for i in range(200)]
    result = scan_market_catalog(markets, max_results=12)
    assert result["candidate_count"] == 200
    assert result["returned_count"] == 12
    assert len(result["markets"]) == 12
    assert result["trade_submitted"] is False
    assert result["route_counts"]["blocked_unclassified"] == 200


def test_catalog_scan_caps_input_work_at_500_records():
    markets = [{"ticker": f"MYSTERY-{i}", "title": "Unknown"} for i in range(1500)]
    result = scan_market_catalog(markets, max_results=100)
    assert result["candidate_count"] == 500
    assert result["returned_count"] == 100


def test_catalog_scan_consumes_only_first_500_items_from_lazy_generator():
    consumed = 0

    def markets():
        nonlocal consumed
        for i in range(100_000):
            consumed += 1
            yield {"ticker": f"UNKNOWN-{i}", "title": "Unknown event"}

    result = scan_market_catalog(markets(), max_results=10)
    assert consumed == 500
    assert result["candidate_count"] == 500
    assert result["returned_count"] == 10
    assert result["trade_submitted"] is False


def test_malformed_public_metadata_is_blocked_without_stringifying_objects():
    result = classify_and_route({
        "ticker": {"spoof": "KXBTC15M-TEST"},
        "title": ["Bitcoin price up in 15 minutes"],
        "category": {"name": "Crypto"},
    })
    assert result["route_status"] == "blocked_unclassified"
    assert result["strategy_key"] is None
    assert result["trade_submitted"] is False


def test_market_router_handles_non_dictionary_input_fail_closed():
    for value in (None, [], "KXBTC15M-TEST", 123):
        result = classify_and_route(value)
        assert result["route_status"] == "blocked_unclassified"
        assert result["trade_submitted"] is False
