"""Phase 7 readiness scaffold must never route unvalidated markets to BTC strategy."""
from market_category_registry import category_profiles, classify_market


def test_only_validated_btc_15m_profile_is_paper_enabled():
    profiles = category_profiles()
    enabled = [profile["key"] for profile in profiles if profile["paper_strategy_enabled"]]
    assert enabled == ["btc_15m"]
    assert all(profile["status"] == "research_only" for profile in profiles if profile["key"] != "btc_15m")


def test_btc_classification_requires_expected_ticker_and_btc_title():
    good = classify_market("KXBTC15M-TEST", "BTC price up next 15 minutes")
    bad_ticker = classify_market("KXSPORTS-TEST", "BTC price up next 15 minutes")
    bad_title = classify_market("KXBTC15M-TEST", "some unrelated question")
    assert good["category"] == "btc_15m" and good["paper_strategy_enabled"] is True
    assert bad_ticker["category"] == "unclassified" and bad_ticker["paper_strategy_enabled"] is False
    assert bad_title["category"] == "unclassified" and bad_title["paper_strategy_enabled"] is False


def test_explicit_non_btc_categories_are_research_only():
    for category in ("sports", "politics", "economics", "weather", "crypto", "finance", "entertainment"):
        classified = classify_market("KXEXAMPLE-TEST", "Example event", category)
        assert classified["category"] != "btc_15m"
        assert classified["paper_strategy_enabled"] is False
        assert classified["status"] in {"research_only", "blocked_unclassified"}


def test_unknown_category_does_not_get_a_fallback_strategy():
    classified = classify_market("KXUNKNOWN-TEST", "Unrecognized event", "space_missions")
    assert classified["category"] == "unclassified"
    assert classified["paper_strategy_enabled"] is False


def test_category_readiness_api_reports_non_btc_as_research_only():
    import main

    response = main.category_readiness()
    assert response["mode"] == "SIMULATION_ONLY"
    assert response["current_active_strategy"] == "btc_15m"
    profiles = {profile["key"]: profile for profile in response["profiles"]}
    assert profiles["btc_15m"]["paper_strategy_enabled"] is True
    assert all(not profile["paper_strategy_enabled"] for key, profile in profiles.items() if key != "btc_15m")
    assert "cannot be routed" in response["safety_note"]


def test_registry_rejects_non_text_identifiers_instead_of_coercing_them():
    from market_category_registry import classify_market

    result = classify_market(["KXBTC15M-TEST"], {"title": "Bitcoin price up"}, {"category": "Crypto"})
    assert result["status"] == "blocked_unclassified"
    assert result["paper_strategy_enabled"] is False
