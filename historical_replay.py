"""No-lookahead replay/validation of archived market decisions.

Input CSV must contain a real historical decision snapshot plus its final resolved
outcome. This script does not invent missing history, reconstruct news that was not
archived, or use the market's final result as an input feature.

Required columns:
  ticker,category,evaluated_at,settled_at,model_id,model_version,
  model_probability_yes,outcome_yes,split
Optional trade columns:
  selected_side,entry_price_cents,contracts,fees_cents,slippage_cents
`split` must be `holdout` for rows used to generate a calibration registry. Supply a
`--train-cutoff-utc`; only decisions evaluated after that cutoff qualify for the
holdout metrics. The registry remains ineligible unless each category/model/version
has at least 100 valid holdout observations and Brier score <= configured maximum.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_COLUMNS = {
    "ticker", "category", "evaluated_at", "settled_at", "model_id", "model_version",
    "model_probability_yes", "outcome_yes", "split",
}


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None  # Reject ambiguous local timestamps.
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _num(value: Any) -> float | None:
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _truth(value: Any) -> bool | None:
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "y", "up"}:
        return True
    if value in {"0", "false", "no", "n", "down"}:
        return False
    return None


def wilson_interval(wins: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt((p * (1 - p) / total) + (z * z / (4 * total * total))) / denom
    return max(0.0, center - half), min(1.0, center + half)


def load_records(csv_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    raw_bytes = csv_path.read_bytes()
    dataset_digest = hashlib.sha256(raw_bytes).hexdigest()
    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = set(reader.fieldnames or [])
        missing = sorted(REQUIRED_COLUMNS - headers)
        if missing:
            raise ValueError("CSV is missing required columns: " + ", ".join(missing))
        valid: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for line_no, raw in enumerate(reader, start=2):
            ticker = str(raw.get("ticker") or "").strip()
            category = str(raw.get("category") or "").strip().lower()
            model_id = str(raw.get("model_id") or "").strip()
            model_version = str(raw.get("model_version") or "").strip()
            evaluated_at = _date(raw.get("evaluated_at"))
            settled_at = _date(raw.get("settled_at"))
            probability = _num(raw.get("model_probability_yes"))
            outcome = _truth(raw.get("outcome_yes"))
            if not ticker or not category or not model_id or not model_version:
                rejected.append({"line": line_no, "reason": "missing identity fields", "ticker": ticker})
                continue
            if evaluated_at is None or settled_at is None or settled_at <= evaluated_at:
                rejected.append({"line": line_no, "reason": "invalid timestamps or settlement not after decision", "ticker": ticker})
                continue
            if probability is None or not 0 < probability < 1 or outcome is None:
                rejected.append({"line": line_no, "reason": "invalid probability or unresolved outcome", "ticker": ticker})
                continue
            row = {
                "line": line_no, "ticker": ticker, "category": category, "model_id": model_id,
                "model_version": model_version, "evaluated_at": evaluated_at, "settled_at": settled_at,
                "probability": probability, "outcome_yes": outcome,
                "split": str(raw.get("split") or "").strip().lower(),
                "side": str(raw.get("selected_side") or "").strip().upper(),
                "entry_price_cents": _num(raw.get("entry_price_cents")),
                "contracts": _num(raw.get("contracts")) or 1.0,
                "fees_cents": _num(raw.get("fees_cents")) or 0.0,
                "slippage_cents": _num(raw.get("slippage_cents")) or 0.0,
            }
            valid.append(row)
    return valid, rejected, dataset_digest


def replay(csv_path: Path, *, train_cutoff_utc: datetime | None, min_samples: int = 100,
           max_brier: float = 0.22) -> dict[str, Any]:
    records, rejected, digest = load_records(csv_path)
    holdout_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    stats_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    trade_rows: list[dict[str, Any]] = []
    for row in records:
        stats_groups[row["category"]].append(row)
        if row["split"] == "holdout" and train_cutoff_utc is not None and row["evaluated_at"] > train_cutoff_utc:
            holdout_groups[(row["model_id"], row["model_version"], row["category"])].append(row)
        if row["side"] in {"YES", "NO"}:
            price, contracts = row["entry_price_cents"], row["contracts"]
            if price is None or not 0 < price < 100 or not contracts.is_integer() or not 1 <= contracts <= 100:
                continue
            won = (row["side"] == "YES" and row["outcome_yes"]) or (row["side"] == "NO" and not row["outcome_yes"])
            gross = contracts * ((100.0 - price) if won else -price)
            net = gross - row["fees_cents"] - row["slippage_cents"]
            trade_rows.append({"ticker": row["ticker"], "category": row["category"], "side": row["side"],
                               "won": won, "net_pnl_cents": round(net, 4), "evaluated_at": row["evaluated_at"].isoformat(),
                               "settled_at": row["settled_at"].isoformat()})

    def calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
        brier = sum((r["probability"] - float(r["outcome_yes"])) ** 2 for r in rows) / len(rows) if rows else None
        yes_count = sum(int(r["outcome_yes"]) for r in rows)
        no_count = len(rows) - yes_count
        eligible = len(rows) >= min_samples and brier is not None and brier <= max_brier and yes_count >= 10 and no_count >= 10
        return {"sample_count": len(rows), "brier_score": round(brier, 6) if brier is not None else None,
                "yes_outcomes": yes_count, "no_outcomes": no_count,
                "out_of_sample": bool(eligible), "evaluation_type": "chronological_holdout" if eligible else "not_validated"}

    registry_models: dict[str, Any] = {}
    for (model_id, model_version, category), rows in sorted(holdout_groups.items()):
        values = calibration(rows)
        key = f"{model_id}:{model_version}:{category}"
        registry_models[key] = {"model_id": model_id, "model_version": model_version, "category": category,
                                **values, "dataset_sha256": digest,
                                "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    category_report: dict[str, Any] = {}
    for category, rows in sorted(stats_groups.items()):
        wins = sum(1 for row in trade_rows if row["category"] == category and row["won"])
        trades = [row for row in trade_rows if row["category"] == category]
        total_won = sum(float(row["net_pnl_cents"]) for row in trades if row["net_pnl_cents"] > 0)
        total_lost = -sum(float(row["net_pnl_cents"]) for row in trades if row["net_pnl_cents"] < 0)
        lo, hi = wilson_interval(wins, len(trades))
        category_report[category] = {"forecast_rows": len(rows), "brier_score": round(sum((r["probability"] - float(r["outcome_yes"])) ** 2 for r in rows) / len(rows), 6),
                                     "trade_rows": len(trades), "trade_wins": wins, "trade_losses": len(trades) - wins,
                                     "trade_win_rate_percent": round(100 * wins / len(trades), 2) if trades else None,
                                     "win_rate_95_wilson_percent": [round(100*lo, 2), round(100*hi, 2)] if lo is not None and hi is not None else None,
                                     "net_pnl_cents": round(sum(float(row["net_pnl_cents"]) for row in trades), 4),
                                     "gross_winning_cents": round(total_won, 4), "gross_losing_cents": round(total_lost, 4)}
    all_trades = trade_rows
    total_wins = sum(row["won"] for row in all_trades)
    lo, hi = wilson_interval(total_wins, len(all_trades))
    return {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_csv": csv_path.name, "dataset_sha256": digest,
        "input_rows_accepted": len(records), "input_rows_rejected": len(rejected), "rejected_examples": rejected[:100],
        "holdout_cutoff_utc": train_cutoff_utc.isoformat() if train_cutoff_utc else None,
        "min_samples": min_samples, "max_brier_score": max_brier,
        "summary": {"historical_trade_rows": len(all_trades), "wins": total_wins, "losses": len(all_trades)-total_wins,
                    "win_rate_percent": round(100 * total_wins / len(all_trades), 2) if all_trades else None,
                    "win_rate_95_wilson_percent": [round(100*lo,2),round(100*hi,2)] if lo is not None and hi is not None else None,
                    "net_pnl_cents": round(sum(float(row["net_pnl_cents"]) for row in all_trades), 4)},
        "by_category": category_report,
        "calibration_registry": {"schema_version": 1, "models": registry_models,
                                 "note": "Only independently reviewed, chronological holdout reports should be used to enable model-based paper entries."},
        "historical_trades": trade_rows[:5000],
        "limitations": ["This replays archived predictions/actions; it does not reconstruct unrecorded historical news or quote states.",
                        "Paper/live execution behavior is not inferred from outcome correctness alone.",
                        "No validation registry qualifies without a supplied cutoff, 100+ holdout rows, both outcomes, and Brier <= threshold."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="CSV of archived decisions and later market outcomes")
    parser.add_argument("--train-cutoff-utc", default="", help="ISO-8601 cutoff; only holdout decisions evaluated after this time qualify for validation")
    parser.add_argument("--min-samples", type=int, default=100)
    parser.add_argument("--max-brier", type=float, default=0.22)
    parser.add_argument("--report", type=Path, default=Path("historical_replay_report.json"))
    parser.add_argument("--registry", type=Path, default=Path("market_model_validation.json"))
    args = parser.parse_args()
    cutoff = _date(args.train_cutoff_utc) if args.train_cutoff_utc else None
    if args.train_cutoff_utc and cutoff is None:
        parser.error("--train-cutoff-utc must be timezone-aware ISO-8601.")
    if not 30 <= args.min_samples <= 10000 or not 0.01 <= args.max_brier <= 0.5:
        parser.error("min samples must be 30-10000 and max Brier 0.01-0.5.")
    report = replay(args.csv, train_cutoff_utc=cutoff, min_samples=args.min_samples, max_brier=args.max_brier)
    args.report.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    # Do not overwrite a working registry with an empty or non-qualifying registry.
    qualifying = {key: val for key, val in report["calibration_registry"]["models"].items() if val.get("out_of_sample") is True}
    if qualifying:
        existing = {"schema_version": 1, "models": {}}
        if args.registry.exists():
            try:
                loaded = json.loads(args.registry.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("models"), dict):
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                pass
        existing.setdefault("models", {}).update(qualifying)
        existing["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        args.registry.write_text(json.dumps(existing, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.report), "registry_updated": bool(qualifying), "qualifying_models": len(qualifying),
                      "summary": report["summary"], "accepted_rows": report["input_rows_accepted"], "rejected_rows": report["input_rows_rejected"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
