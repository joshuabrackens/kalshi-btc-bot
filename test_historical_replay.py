from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

from historical_replay import replay


def _row(i: int, *, split="holdout"):
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    decision = cutoff + timedelta(days=1, minutes=i)
    outcome_yes = i % 2 == 0
    prob = 0.9 if outcome_yes else 0.1
    return {"ticker": f"KXTEST-{i}", "category": "sports", "evaluated_at": decision.isoformat(),
            "settled_at": (decision + timedelta(hours=3)).isoformat(), "model_id": "openai-responses-websearch",
            "model_version": "gpt-5.1:evidence-probability-v1", "model_probability_yes": str(prob),
            "outcome_yes": str(outcome_yes).lower(), "split": split,
            "selected_side": "YES" if outcome_yes else "NO", "entry_price_cents": "50", "contracts": "1",
            "fees_cents": "1", "slippage_cents": "1"}


def _write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_historical_replay_builds_registry_only_from_good_chronological_holdout(tmp_path):
    source = tmp_path / "history.csv"
    _write_csv(source, [_row(i) for i in range(100)])
    report = replay(source, train_cutoff_utc=datetime(2025, 12, 31, tzinfo=timezone.utc), min_samples=100, max_brier=0.22)
    registry = report["calibration_registry"]["models"]["openai-responses-websearch:gpt-5.1:evidence-probability-v1:sports"]
    assert report["input_rows_accepted"] == 100
    assert report["summary"]["historical_trade_rows"] == 100
    assert report["summary"]["win_rate_percent"] == 100.0
    assert registry["out_of_sample"] is True
    assert registry["sample_count"] == 100
    assert registry["brier_score"] < 0.02
    assert len(registry["dataset_sha256"]) == 64


def test_historical_replay_does_not_validate_when_cutoff_is_after_decisions(tmp_path):
    source = tmp_path / "history.csv"
    _write_csv(source, [_row(i) for i in range(100)])
    report = replay(source, train_cutoff_utc=datetime(2026, 1, 3, tzinfo=timezone.utc), min_samples=100, max_brier=0.22)
    assert report["calibration_registry"]["models"] == {}


def test_historical_replay_rejects_lookahead_or_malformed_outcomes(tmp_path):
    source = tmp_path / "history.csv"
    rows = [_row(i) for i in range(2)]
    rows[0]["settled_at"] = rows[0]["evaluated_at"]
    rows[1]["outcome_yes"] = "unknown"
    _write_csv(source, rows)
    report = replay(source, train_cutoff_utc=datetime(2025, 12, 31, tzinfo=timezone.utc))
    assert report["input_rows_accepted"] == 0
    assert report["input_rows_rejected"] == 2
