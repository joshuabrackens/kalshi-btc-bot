"""Fail-closed tests for the paper-only consecutive net-loss circuit breaker."""
import unittest
from datetime import datetime, timezone

import paper_worker as worker


class LossStreakCircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)

    def test_four_recent_losses_pause_new_entries(self):
        rows = [
            ("2026-10-24T05:50:00+00:00", -6),
            ("2026-10-24T05:40:00+00:00", -17),
            ("2026-10-24T05:30:00+00:00", -25),
            ("2026-10-24T05:20:00+00:00", -36),
        ]
        status = worker._loss_streak_cooldown_status(rows, limit=4, cooldown_minutes=60, now=self.now)
        self.assertIsNotNone(status)
        self.assertTrue(status["blocked"])
        self.assertEqual(status["consecutive_losses"], 4)
        self.assertEqual(status["seconds_remaining"], 50 * 60)

    def test_a_positive_net_trade_resets_the_streak(self):
        rows = [
            ("2026-10-24T05:50:00+00:00", -6),
            ("2026-10-24T05:40:00+00:00", -17),
            ("2026-10-24T05:30:00+00:00", 2),
            ("2026-10-24T05:20:00+00:00", -36),
        ]
        self.assertIsNone(worker._loss_streak_cooldown_status(rows, limit=4, cooldown_minutes=60, now=self.now))

    def test_cooldown_expiry_allows_new_entries_even_if_streak_remains(self):
        rows = [(f"2026-10-24T04:{m:02d}:00+00:00", -1) for m in (50, 40, 30, 20)]
        self.assertIsNone(worker._loss_streak_cooldown_status(rows, limit=4, cooldown_minutes=60, now=self.now))

    def test_disabled_guard_does_not_pause(self):
        rows = [("2026-10-24T05:50:00+00:00", -1)] * 4
        self.assertIsNone(worker._loss_streak_cooldown_status(rows, limit=0, cooldown_minutes=60, now=self.now))
        self.assertIsNone(worker._loss_streak_cooldown_status(rows, limit=4, cooldown_minutes=0, now=self.now))


if __name__ == "__main__":
    unittest.main()
