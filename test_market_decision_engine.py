from datetime import datetime, timedelta, timezone

from market_decision_engine import assess_market_opportunity, validate_model_evidence


def _stamp(minutes_ago=1):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _evidence(kind):
    return {
        "type": kind,
        "source": f"Official {kind.replace('_', ' ')} source",
        "url": f"https://source.example/{kind}",
        "published_at": _stamp(5),
        "retrieved_at": _stamp(1),
        "relevance_score": 0.95,
        "reliability_score": 0.98,
    }


def _valid_sports_model(**overrides):
    model = {
        "market_ticker": "KXSPORTS-TEST",
        "model_probability_yes": 0.75,
        "model_id": "sports-outcomes",
        "model_version": "v1.2.0",
        "as_of_utc": _stamp(1),
        "validation": {"out_of_sample": True, "sample_count": 250, "brier_score": 0.17},
        "evidence": [_evidence(k) for k in ("injury_report", "lineup", "recent_news", "recent_stats")],
        "rationale": "Fixture with complete source provenance.",
    }
    model.update(overrides)
    return model


def _market(**overrides):
    m = {
        "ticker": "KXSPORTS-TEST", "title": "Will team A win?", "category": "sports",
        "status": "active", "volume_24h_fp": "1000", "open_interest_fp": "800",
    }
    m.update(overrides)
    return m


def _orderbook(yes_bid=49, no_bid=49, yes_qty=15, no_qty=15):
    return {"orderbook": {"yes": [[yes_bid, yes_qty], [yes_bid - 1, 10]],
                           "no": [[no_bid, no_qty], [no_bid - 1, 10]]}}


def test_fresh_calibrated_model_with_required_evidence_and_liquid_book_can_be_paper_candidate():
    result = assess_market_opportunity(_market(), _orderbook(), _valid_sports_model())
    assert result["eligible"] is True
    assert result["status"] == "paper_candidate"
    assert result["selected_side"] == "YES"
    assert result["yes_net_edge_cents"] == 21
    assert result["trade_submitted"] is False
    assert result["model"]["validation_sample_count"] == 250
    assert set(result["model"]["evidence_types"]) >= {"injury_report", "lineup", "recent_news", "recent_stats"}


def test_absent_model_does_not_treat_market_implied_odds_as_edge():
    result = assess_market_opportunity(_market(), _orderbook(), None)
    assert result["eligible"] is False
    assert "probability_provider_unavailable" in result["reasons"]
    assert "net_edge_not_calculable" in result["reasons"]
    assert result["selected_side"] is None


def test_stale_or_missing_injury_evidence_blocks_sports_market():
    model = _valid_sports_model()
    model["evidence"] = [e for e in model["evidence"] if e["type"] != "injury_report"]
    result = assess_market_opportunity(_market(), _orderbook(), model)
    assert result["eligible"] is False
    assert any(r.startswith("required_evidence_missing:injury_report") for r in result["reasons"])


def test_unvalidated_or_poorly_calibrated_model_blocks_even_with_positive_raw_edge():
    model = _valid_sports_model(validation={"out_of_sample": False, "sample_count": 99, "brier_score": 0.28})
    result = assess_market_opportunity(_market(), _orderbook(), model)
    assert result["eligible"] is False
    assert "model_not_validated_out_of_sample" in result["reasons"]
    assert "insufficient_out_of_sample_validation" in result["reasons"]
    assert "brier_score_unavailable_or_too_high" in result["reasons"]


def test_wide_spread_and_thin_depth_block_market_even_when_model_has_edge():
    result = assess_market_opportunity(
        _market(), _orderbook(yes_bid=48, no_bid=42, yes_qty=1, no_qty=2), _valid_sports_model()
    )
    assert result["eligible"] is False
    assert "yes_spread_too_wide_or_unknown" in result["reasons"]
    assert "no_spread_too_wide_or_unknown" in result["reasons"]
    assert "insufficient_yes_ask_depth" in result["reasons"]
    assert "insufficient_no_ask_depth" in result["reasons"]


def test_unknown_closed_market_and_bad_prices_fail_closed():
    for market in (
        _market(status="closed"), _market(status=""),
        _market(volume_24h_fp="not-a-number"),
    ):
        result = assess_market_opportunity(market, _orderbook(), _valid_sports_model())
        assert result["eligible"] is False
        assert result["trade_submitted"] is False


def test_evidence_url_and_retrieval_freshness_are_required():
    model = _valid_sports_model()
    model["evidence"][0]["url"] = "javascript:alert(1)"
    model["evidence"][1]["retrieved_at"] = _stamp(90)
    ok, reasons, summary = validate_model_evidence(model, "sports")
    assert ok is False
    assert any(r.startswith("required_evidence_missing:") for r in reasons)
    assert "injury_report" in reasons[-1]
    assert "lineup" in reasons[-1]
    assert summary["evidence_count"] == 2


def test_extreme_model_timestamp_and_probability_rejected():
    model = _valid_sports_model(model_probability_yes=1.0, as_of_utc="2999-01-01T00:00:00Z")
    ok, reasons, _ = validate_model_evidence(model, "sports")
    assert ok is False
    assert "invalid_model_probability" in reasons
    assert "model_timestamp_in_future" in reasons


def test_unknown_category_never_becomes_eligible_even_with_strong_model():
    result = assess_market_opportunity(
        _market(ticker="UNKNOWN-MARKET", title="Unrecognized event", category="unknown"),
        _orderbook(), _valid_sports_model(),
    )
    assert result["eligible"] is False
    assert "category_not_supported" in result["reasons"]
    assert result["trade_submitted"] is False


def test_model_must_be_bound_to_exact_market_ticker():
    model = _valid_sports_model(market_ticker="KXSPORTS-OTHER")
    result = assess_market_opportunity(_market(), _orderbook(), model)
    assert result["eligible"] is False
    assert "model_ticker_mismatch" in result["reasons"]
    assert result["trade_submitted"] is False


def test_cost_and_liquidity_configuration_cannot_disable_minimum_gates():
    result = assess_market_opportunity(
        _market(), _orderbook(), _valid_sports_model(),
        min_volume_24h=0, min_depth_contracts=0, max_spread_cents=100,
        min_net_edge_cents=0, estimated_fee_cents=0, slippage_cents=0,
        max_entry_cents=100, min_validation_samples=1, max_brier_score=1.0,
    )
    assert result["estimated_fee_cents"] == 2
    assert result["estimated_slippage_cents"] == 1
    assert result["entry_ask_cents"] <= 90 if result["eligible"] else True
    assert result["expected_net_edge_cents"] is None or result["expected_net_edge_cents"] >= 5
