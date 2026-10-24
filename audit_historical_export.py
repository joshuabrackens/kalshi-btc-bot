"""Audit an admin-only BTC bot historical replay ZIP export.

This audits the actual recorded trade/decision history. It never fetches current
prices to pretend to reproduce old information and never invents missing outcomes.
Use the exported CSVs from /api/diagnostics/replay-export.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _float(v: Any) -> float | None:
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _date(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        if d.tzinfo is None:
            return None
        return d.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def wilson(wins: int, n: int, z: float = 1.959963984540054) -> list[float] | None:
    if n <= 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) / n) + (z * z / (4 * n * n))) / denom
    return [round(max(0.0, center - half) * 100, 2), round(min(1.0, center + half) * 100, 2)]


def _csv_rows(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    try:
        raw = archive.read(name).decode("utf-8-sig")
    except KeyError:
        return []
    return list(csv.DictReader(io.StringIO(raw, newline="")))


def _trade_metrics(rows: list[dict[str, str]], pnl_key: str = "pnl_cents") -> dict[str, Any]:
    settled: list[dict[str, Any]] = []
    rejected = 0
    for row in rows:
        opened = _date(row.get("created_at"))
        settled_at = _date(row.get("settled_at"))
        pnl = _float(row.get(pnl_key))
        if not row.get("settled_at") or pnl is None:
            continue
        if opened and settled_at and settled_at <= opened:
            rejected += 1
            continue
        settled.append({**row, "_pnl": pnl, "_settled_at": settled_at})
    wins = sum(r["_pnl"] > 0 for r in settled)
    losses = sum(r["_pnl"] < 0 for r in settled)
    flats = len(settled) - wins - losses
    total = sum(r["_pnl"] for r in settled)
    winning_sum = sum(r["_pnl"] for r in settled if r["_pnl"] > 0)
    losing_abs = -sum(r["_pnl"] for r in settled if r["_pnl"] < 0)
    equity, peak, max_drawdown = 0.0, 0.0, 0.0
    for row in sorted(settled, key=lambda r: r["_settled_at"] or datetime.min.replace(tzinfo=timezone.utc)):
        equity += row["_pnl"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {"settled_trades": len(settled), "unsettled_or_missing_pnl_rows": len(rows) - len(settled),
            "invalid_time_order_rows_rejected": rejected, "wins": wins, "losses": losses, "flat": flats,
            "win_rate_percent": round(wins * 100 / len(settled), 2) if settled else None,
            "win_rate_95_wilson_percent": wilson(wins, len(settled)),
            "net_pnl_cents": round(total, 4), "average_pnl_cents": round(total / len(settled), 4) if settled else None,
            "average_win_cents": round(winning_sum / wins, 4) if wins else None,
            "average_loss_cents": round(losing_abs / losses, 4) if losses else None,
            "profit_factor": round(winning_sum / losing_abs, 4) if losing_abs else (None if not wins else "infinite_no_losses"),
            "max_closed_trade_drawdown_cents": round(max_drawdown, 4)}


def _cohort(rows: list[dict[str, str]], key_fn) -> dict[str, Any]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if not row.get("settled_at") or _float(row.get("pnl_cents")) is None:
            continue
        groups[str(key_fn(row))].append(row)
    output: dict[str, Any] = {}
    for key, group in sorted(groups.items()):
        metrics = _trade_metrics(group)
        output[key] = {k: metrics[k] for k in ("settled_trades", "wins", "losses", "win_rate_percent", "win_rate_95_wilson_percent", "net_pnl_cents", "average_pnl_cents", "profit_factor")}
    return output


def audit_export(zip_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path, "r") as z:
        names = set(z.namelist())
        paper = _csv_rows(z, "paper_trades.csv")
        learning = _csv_rows(z, "trade_learning.csv")
        signals = _csv_rows(z, "signal_decisions.csv")
        decisions = _csv_rows(z, "market_research_decisions.csv")
        outcomes = _csv_rows(z, "market_research_prediction_outcomes.csv")
        market_trades = _csv_rows(z, "market_research_paper_trades.csv")
    metrics = _trade_metrics(paper)
    # Outcome labels are recognized only when explicit; stop-loss labels are exits,
    # not market-resolved truth, and are not counted as directional correctness.
    directional = []
    for row in paper:
        side = str(row.get("direction") or "").strip().upper().replace("BUY ", "")
        result = str(row.get("outcome") or "").strip().upper()
        side_norm = {"UP": "UP", "DOWN": "DOWN", "YES": "YES", "NO": "NO"}.get(side)
        out_norm = {"UP": "UP", "DOWN": "DOWN", "YES": "YES", "NO": "NO"}.get(result)
        if side_norm and out_norm and row.get("settled_at"):
            directional.append(side_norm == out_norm)
    research_settled = [r for r in market_trades if str(r.get("status", "")).lower() == "settled" and str(r.get("result", "")).upper() in {"YES", "NO"}]
    research_wins = sum(str(r.get("side", "")).upper() == str(r.get("result", "")).upper() for r in research_settled)
    research_pnl = [_float(r.get("pnl_cents")) for r in research_settled]
    research_pnl = [x for x in research_pnl if x is not None]
    calibration_groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in outcomes:
        p, y = _float(row.get("model_probability_yes")), _float(row.get("actual_outcome_yes"))
        if p is None or y not in (0.0, 1.0) or not 0 < p < 1 or not (_date(row.get("settled_at")) and _date(row.get("evaluated_at")) and _date(row["settled_at"]) > _date(row["evaluated_at"])):
            continue
        calibration_groups[(row.get("category", "unknown"), row.get("model_id", "unknown"), row.get("model_version", "unknown"))].append(row)
    calibration = {}
    for key, group in sorted(calibration_groups.items()):
        ps = [_float(r["model_probability_yes"]) for r in group]
        ys = [int(float(r["actual_outcome_yes"])) for r in group]
        wins = sum((p >= 0.5) == bool(y) for p, y in zip(ps, ys))
        calibration["|".join(key)] = {"n": len(group), "accuracy_percent": round(wins * 100 / len(group), 2),
            "brier_score": round(sum((p-y)**2 for p, y in zip(ps, ys))/len(group), 6),
            "yes_outcomes": sum(ys), "no_outcomes": len(ys)-sum(ys)}
    min_rows = 10
    result = {
        "source_zip": zip_path.name,
        "source_files_found": sorted(names),
        "paper_trade_metrics": metrics,
        "paper_directional_correctness": {"comparable_settled_rows": len(directional), "correct": sum(directional),
            "accuracy_percent": round(100*sum(directional)/len(directional), 2) if directional else None,
            "note": "Only explicit UP/DOWN or YES/NO outcomes are counted; exit labels such as TREND STOP LOSS are not market truth."},
        "paper_trade_cohorts": {
            "signal_source": _cohort(paper, lambda r: r.get("entry_signal_source") or "unknown"),
            "timeframe": _cohort(paper, lambda r: r.get("timeframe") or "unknown"),
            "entry_score_bucket": _cohort(paper, lambda r: ("missing" if _float(r.get("entry_signal_score")) is None else ("<40" if _float(r.get("entry_signal_score")) < 40 else "40-59" if _float(r.get("entry_signal_score")) < 60 else "60-79" if _float(r.get("entry_signal_score")) < 80 else "80+"))),
            "entry_price_bucket": _cohort(paper, lambda r: ("missing" if _float(r.get("entry_cents")) is None else ("1-49" if _float(r.get("entry_cents")) < 50 else "50-64" if _float(r.get("entry_cents")) < 65 else "65-79" if _float(r.get("entry_cents")) < 80 else "80-92"))),
        },
        "counterfactual_decisions": {"rows": len(signals), "settled_counterfactuals": sum(bool(r.get("counterfactual_settled_at")) for r in signals),
            "note": "Counterfactual rows are reported separately from actual executed-trade P&L; do not combine them into actual win rate."},
        "market_research_paper_trades": {"settled": len(research_settled), "correct_side": research_wins,
            "side_accuracy_percent": round(research_wins * 100 / len(research_settled), 2) if research_settled else None,
            "net_pnl_cents": round(sum(research_pnl), 4), "pending_or_nonbinary": len(market_trades)-len(research_settled)},
        "model_calibration": calibration,
        "notes": [
            "This is an audit of decisions actually recorded in the exported historical journal, not a full counterfactual simulation of all markets.",
            "A true historical strategy replay needs the exact data available at each decision time, including contemporaneous quotes, rules, news snapshots, model version, and the final resolution.",
            "Win rate is not profit expectancy. Use net P&L, fee/slippage estimates, drawdown, and confidence intervals together.",
            f"Cohort conclusions should be treated as exploratory below {min_rows} settled trades per group; do not make causal claims from these correlations."
        ]
    }
    return result


def write_markdown(report: dict[str, Any], path: Path) -> None:
    m = report["paper_trade_metrics"]
    lines = ["# BTC Bot Historical Replay Audit", "", f"Source: `{report['source_zip']}`", "",
             "## Actual BTC paper-trade journal", "",
             f"- Settled trades: {m['settled_trades']}", f"- Wins / losses / flat: {m['wins']} / {m['losses']} / {m['flat']}",
             f"- Win rate: {m['win_rate_percent']}%", f"- 95% Wilson interval: {m['win_rate_95_wilson_percent']}",
             f"- Net P&L: {m['net_pnl_cents']} cents", f"- Average P&L per settled trade: {m['average_pnl_cents']} cents",
             f"- Profit factor: {m['profit_factor']}", f"- Closed-trade drawdown: {m['max_closed_trade_drawdown_cents']} cents", "",
             "## Entry-price cohorts", "", "| Entry-price bucket | N | Win rate | Net P&L (cents) |", "|---|---:|---:|---:|"]
    for bucket, v in report["paper_trade_cohorts"]["entry_price_bucket"].items():
        lines.append(f"| {bucket} | {v['settled_trades']} | {v['win_rate_percent']}% | {v['net_pnl_cents']} |")
    lines.extend(["", "## Model calibration", "", "| Category / model / version | N | Accuracy | Brier |", "|---|---:|---:|---:|"])
    for key, v in report["model_calibration"].items():
        lines.append(f"| {key} | {v['n']} | {v['accuracy_percent']}% | {v['brier_score']} |")
    lines.extend(["", "## Limitations", *[f"- {n}" for n in report["notes"]], ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit BTC bot historical replay export ZIP")
    parser.add_argument("export_zip", type=Path)
    parser.add_argument("--json", type=Path, default=Path("historical_replay_audit.json"))
    parser.add_argument("--markdown", type=Path, default=Path("historical_replay_audit.md"))
    args = parser.parse_args()
    result = audit_export(args.export_zip)
    args.json.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    write_markdown(result, args.markdown)
    print(json.dumps({"report_json": str(args.json), "report_markdown": str(args.markdown),
                      "settled_trades": result["paper_trade_metrics"]["settled_trades"],
                      "win_rate_percent": result["paper_trade_metrics"]["win_rate_percent"]}, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
