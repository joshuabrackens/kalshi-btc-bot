"""Offline correctness tests for the Phase 3–6 descriptive analytics."""
from performance_analytics import build_performance_analytics


def test_actual_pnl_is_separate_from_counterfactual_results():
    trades = [
        {"id": 1, "created_at": "2026-10-23T00:00:00+00:00", "settled_at": "2026-10-23T00:15:00+00:00",
         "entry_cents": 70, "direction": "UP", "pnl_cents": 28, "entry_signal_log_id": 10},
        {"id": 2, "created_at": "2026-10-23T00:15:00+00:00", "settled_at": "2026-10-23T00:30:00+00:00",
         "entry_cents": 82, "direction": "DOWN", "pnl_cents": -84, "entry_signal_log_id": 11},
        {"id": 3, "created_at": "2026-10-23T00:30:00+00:00", "settled_at": None,
         "entry_cents": 79, "direction": "UP", "pnl_cents": None, "entry_signal_log_id": None},
    ]
    linked = {
        10: {"chart_trend_at_decision": {"candidate_bias": "UP", "evidence_score": 82}},
        11: {"chart_trend_at_decision": {"candidate_bias": "UP", "evidence_score": 71}},
    }
    cf = [{"id": 4, "created_at": "2026-10-23T00:30:00+00:00", "counterfactual_settled_at": "2026-10-23T00:45:00+00:00",
           "counterfactual_pnl_cents": 48, "decision_status": "vetoed", "market_ticker": "KXBTC15M-CF",
           "proposed_direction": "UP", "entry_ask_cents": 50, "trend_bias": "DOWN", "trend_evidence_score": 82}]
    output = build_performance_analytics(trades, linked, [], cf)
    assert output["summary"]["settled_trades"] == 2
    assert output["summary"]["open_positions"] == 1
    assert output["summary"]["net_pnl_cents"] == -56
    assert output["summary"]["wins"] == 1 and output["summary"]["losses"] == 1
    assert output["trend_stop_shadow_comparison"]["triggered_and_settled_count"] == 0
    assert output["rejected_signal_counterfactuals"]["summary"]["net_pnl_cents"] == 48
    assert output["rejected_signal_counterfactuals"]["summary"]["net_pnl_cents"] != output["summary"]["net_pnl_cents"]
    assert output["by_chart_trend_alignment"]


def test_price_buckets_and_score_groups_are_descriptive_not_probabilities():
    trades = [
        {"id": 1, "settled_at": "2026-10-23T00:01:00+00:00", "entry_cents": 49, "direction": "UP", "pnl_cents": 10, "entry_signal_log_id": 1},
        {"id": 2, "settled_at": "2026-10-23T00:02:00+00:00", "entry_cents": 80, "direction": "DOWN", "pnl_cents": -12, "entry_signal_log_id": 2},
    ]
    linked = {
        1: {"chart_trend_at_decision": {"candidate_bias": "UP", "evidence_score": 38}},
        2: {"chart_trend_at_decision": {"candidate_bias": "UP", "evidence_score": 81}},
    }
    output = build_performance_analytics(trades, linked)
    assert {r["group"] for r in output["by_entry_price_bucket"]} == {"1–49¢", "80–92¢"}
    assert {r["group"] for r in output["by_chart_evidence_score"]} == {"0–39", "75–100"}
    assert "Scores are not win probabilities" in output["important"]


def test_win_rate_includes_wilson_interval_and_profitability_warning_for_small_samples():
    from performance_analytics import _pnl_summary
    rows = [
        {"settled_at": f"2026-10-{(i % 28) + 1:02d}T00:00:00Z", "pnl_cents": 20 if i < 53 else -25}
        for i in range(95)
    ]
    summary = _pnl_summary(rows)
    assert summary["win_rate_percent"] == round(53 / 95 * 100, 2)
    assert summary["win_rate_95_ci_low_percent"] < summary["win_rate_percent"]
    assert summary["win_rate_95_ci_high_percent"] > summary["win_rate_percent"]
    assert summary["win_rate_95_ci_low_percent"] > 35
    assert summary["win_rate_95_ci_high_percent"] < 80
