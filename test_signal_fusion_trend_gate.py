"""Tests for binding the continuously refreshed BTC trend snapshot into hybrid decisions."""
from datetime import datetime, timedelta, timezone
import unittest

from signal_fusion import evaluate_fusion

NOW = datetime(2026, 10, 23, 4, 0, 0, tzinfo=timezone.utc)


def base_kwargs(**overrides):
    kwargs = {
        "direction": "DOWN",
        "alert_created_at": (NOW - timedelta(seconds=20)).isoformat(),
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "internal_payload": {"strategies": {}},
        "market_ticker": "KXBTC15M-26OCT230400-00",
        "seconds_remaining": 600,
        "up_ask_cents": 70,
        "down_ask_cents": 70,
        "spread_cents": 2,
        "fee_cents_per_contract": 2,
        "max_entry_cents": 80,
        "max_spread_cents": 5,
        "max_alert_age_seconds": 300,
        "minimum_opposing_points": 4,
        "now": NOW,
    }
    kwargs.update(overrides)
    return kwargs


def snapshot(**overrides):
    data = {
        "status": "ok",
        "monitor_only": True,
        "updated_at": (NOW - timedelta(seconds=10)).isoformat(),
        "candidate_bias": "UP",
        "evidence_score": 82,
        "evidence_points": {"up": 9, "down": 0, "net": 9, "max_directional_points": 11},
        "timeframes": {"1m": "bullish", "5m": "bullish", "15m": "mixed"},
    }
    data.update(overrides)
    return data


class TrendGateTests(unittest.TestCase):
    def test_strong_opposing_fresh_trend_vetoes_hybrid_alert(self):
        result = evaluate_fusion(**base_kwargs(trend_snapshot=snapshot(), require_trend_snapshot=True))
        self.assertFalse(result.approved)
        self.assertEqual(result.direction, "WAIT")
        self.assertTrue(result.vetoed_by_internal)
        self.assertTrue(any("BTC trend monitor veto" in reason for reason in result.reasons))

    def test_missing_required_trend_snapshot_fails_closed(self):
        result = evaluate_fusion(**base_kwargs(require_trend_snapshot=True))
        self.assertFalse(result.approved)
        self.assertTrue(any("snapshot is missing" in reason for reason in result.reasons))

    def test_stale_trend_snapshot_fails_closed(self):
        old = snapshot(updated_at=(NOW - timedelta(seconds=75)).isoformat())
        result = evaluate_fusion(**base_kwargs(trend_snapshot=old, require_trend_snapshot=True,
                                               max_trend_snapshot_age_seconds=45))
        self.assertFalse(result.approved)
        self.assertTrue(any("stale/invalid" in reason for reason in result.reasons))

    def test_healthy_aligned_trend_allows_to_continue_normal_checks(self):
        aligned = snapshot(candidate_bias="DOWN", evidence_score=73,
                           evidence_points={"up": 1, "down": 8, "net": -7},
                           timeframes={"1m": "bearish", "5m": "bearish", "15m": "mixed"})
        result = evaluate_fusion(**base_kwargs(trend_snapshot=aligned, require_trend_snapshot=True))
        self.assertTrue(result.approved)
        self.assertTrue(any("aligns with DOWN" in reason for reason in result.reasons))

    def test_weak_opposing_alignment_does_not_veto_by_itself(self):
        weak = snapshot(evidence_score=55, evidence_points={"up": 6, "down": 2, "net": 4},
                        timeframes={"1m": "bullish", "5m": "mixed", "15m": "mixed"})
        result = evaluate_fusion(**base_kwargs(trend_snapshot=weak, require_trend_snapshot=True))
        self.assertTrue(result.approved)
        self.assertTrue(any("below veto threshold" in reason for reason in result.reasons))

    def test_one_opposing_timeframe_is_not_enough_for_trend_veto(self):
        not_confirmed = snapshot(evidence_score=82, evidence_points={"up": 9, "down": 0, "net": 9},
                                 timeframes={"1m": "bullish", "5m": "mixed", "15m": "mixed"})
        result = evaluate_fusion(**base_kwargs(trend_snapshot=not_confirmed, require_trend_snapshot=True))
        self.assertTrue(result.approved)


    def test_price_above_preferred_target_is_allowed_when_hard_cap_is_92(self):
        aligned = snapshot(candidate_bias="DOWN", evidence_score=73,
                           evidence_points={"up": 1, "down": 8, "net": -7},
                           timeframes={"1m": "bearish", "5m": "bearish", "15m": "mixed"})
        result = evaluate_fusion(**base_kwargs(down_ask_cents=90, max_entry_cents=92,
                                               trend_snapshot=aligned, require_trend_snapshot=True))
        self.assertTrue(result.approved)
        self.assertEqual(result.direction, "BUY_DOWN")
        self.assertEqual(result.entry_ask_cents, 90)

    def test_missing_monitor_health_fails_closed(self):
        unhealthy = snapshot(status="degraded")
        result = evaluate_fusion(**base_kwargs(trend_snapshot=unhealthy, require_trend_snapshot=True))
        self.assertFalse(result.approved)
        self.assertTrue(any("not healthy" in reason for reason in result.reasons))


if __name__ == "__main__":
    unittest.main()
