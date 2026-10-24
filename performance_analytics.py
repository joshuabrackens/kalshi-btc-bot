"""Pure retrospective metrics for paper trading; no market calls and no order code."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping, Sequence


def _num(value: Any) -> float | None:
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _pnl_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    settled = [r for r in rows if r.get("settled_at") is not None and _num(r.get("pnl_cents")) is not None]
    pnls = [_num(r.get("pnl_cents")) or 0.0 for r in settled]
    wins = sum(p > 0 for p in pnls)
    losses = sum(p < 0 for p in pnls)
    flats = len(pnls) - wins - losses
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    def _sort_key(row: Mapping[str, Any]):
        return str(row.get("settled_at") or "")
    for row in sorted(settled, key=_sort_key):
        equity += _num(row.get("pnl_cents")) or 0.0
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    settled_count = len(settled)
    win_rate = wins / settled_count if settled_count else None
    # Wilson interval communicates how uncertain a modest observed win rate is.
    # It is descriptive, not a forecast and not used to loosen/tighten trade rules.
    if settled_count and win_rate is not None:
        z = 1.959963984540054
        denominator = 1 + z * z / settled_count
        center = (win_rate + z * z / (2 * settled_count)) / denominator
        margin = (z * ((win_rate * (1 - win_rate) / settled_count + z * z / (4 * settled_count * settled_count)) ** 0.5)) / denominator
        ci_low = max(0.0, (center - margin) * 100)
        ci_high = min(100.0, (center + margin) * 100)
    else:
        ci_low = ci_high = None
    return {
        "total_trades": len(rows), "settled_trades": settled_count,
        "open_positions": max(0, len(rows) - settled_count),
        "wins": wins, "losses": losses, "break_even": flats,
        "win_rate_percent": round(wins * 100 / settled_count, 2) if settled_count else None,
        "win_rate_95_ci_low_percent": round(ci_low, 2) if ci_low is not None else None,
        "win_rate_95_ci_high_percent": round(ci_high, 2) if ci_high is not None else None,
        "net_pnl_cents": round(sum(pnls), 4), "net_pnl_dollars": round(sum(pnls) / 100, 4),
        "average_net_pnl_cents_per_settled_trade": round(sum(pnls) / len(pnls), 4) if pnls else None,
        "gross_profit_cents": round(gross_profit, 4), "gross_loss_cents": round(gross_loss, 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (None if not pnls else "no_recorded_losses"),
        "max_drawdown_cents": round(max_drawdown, 4),
        "largest_win_cents": round(max(pnls), 4) if pnls else None,
        "largest_loss_cents": round(min(pnls), 4) if pnls else None,
        "sample_note": "Descriptive paper results only; small samples are noisy and do not establish a tradable edge." if len(settled) < 50 else "More data available, but performance still needs out-of-sample validation.",
    }


def _group_by(rows: Sequence[Mapping[str, Any]], key_func) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        key = key_func(row)
        if key is not None:
            groups[str(key)].append(row)
    result = []
    for key, group in sorted(groups.items()):
        summary = _pnl_summary(group)
        result.append({"group": key, **summary})
    return result


def _price_bucket(value: Any) -> str | None:
    price = _num(value)
    if price is None or not 0 < price < 100:
        return None
    if price < 50: return "1–49¢"
    if price < 65: return "50–64¢"
    if price < 80: return "65–79¢"
    if price <= 92: return "80–92¢"
    return "93–99¢"


def _extract_chart_snapshot(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        import json
        try: payload = json.loads(payload)
        except (TypeError, ValueError): return {}
    if not isinstance(payload, dict): return {}
    trend = payload.get("chart_trend_at_decision")
    if isinstance(trend, dict) and trend:
        return trend
    # Backward compatibility for possible previous payload layouts.
    trend = payload.get("btc_trend_monitor")
    return trend if isinstance(trend, dict) else {}


def build_performance_analytics(
    trades: Sequence[Mapping[str, Any]],
    linked_signal_payloads: Mapping[int, Any] | None = None,
    decision_events: Sequence[Mapping[str, Any]] = (),
    counterfactual_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Aggregate actual journal P&L, decision cohorts and counterfactuals transparently."""
    linked = linked_signal_payloads or {}
    settled_desc = sorted(
        [t for t in trades if t.get("settled_at") is not None and _num(t.get("pnl_cents")) is not None],
        key=lambda t: str(t.get("settled_at") or ""), reverse=True,
    )
    recent20 = list(reversed(settled_desc[:20]))
    recent50 = list(reversed(settled_desc[:50]))

    annotated: list[dict[str, Any]] = []
    for original in trades:
        row = dict(original)
        signal_id = _num(row.get("entry_signal_log_id"))
        payload = linked.get(int(signal_id)) if signal_id is not None else None
        snap = _extract_chart_snapshot(payload)
        bias = str(snap.get("candidate_bias") or "").strip().upper()
        direction = str(row.get("direction") or "").strip().upper()
        if bias in {"UP", "DOWN"} and direction in {"UP", "DOWN"}:
            alignment = "aligned" if bias == direction else "opposed"
        elif bias == "MIXED":
            alignment = "mixed"
        else:
            alignment = "unknown"
        score = _num(snap.get("evidence_score"))
        row["_trend_alignment"] = alignment
        row["_chart_trend_bias"] = bias or None
        row["_chart_evidence_score"] = score
        annotated.append(row)

    price_perf = _group_by(annotated, lambda r: _price_bucket(r.get("entry_cents")))
    alignment_perf = _group_by(annotated, lambda r: r.get("_trend_alignment"))
    def score_bucket(r):
        score = _num(r.get("_chart_evidence_score"))
        if score is None: return None
        if score < 40: return "0–39"
        if score < 60: return "40–59"
        if score < 75: return "60–74"
        return "75–100"
    score_perf = _group_by(annotated, score_bucket)

    shadow_rows = [r for r in annotated if r.get("trend_stop_shadow_triggered_at") and r.get("settled_at") is not None]
    hyp = [_num(r.get("trend_stop_shadow_pnl_cents")) for r in shadow_rows]
    hyp = [x for x in hyp if x is not None]
    actual = [(_num(r.get("pnl_cents")) or 0.0) for r in shadow_rows]
    shadow_comparison = {
        "triggered_and_settled_count": len(shadow_rows),
        "rows_with_hypothetical_pnl": len(hyp),
        "hypothetical_exit_net_pnl_cents_sum": round(sum(hyp), 4),
        "actual_journal_net_pnl_cents_sum_for_same_trades": round(sum(actual), 4),
        "hypothetical_minus_actual_cents_sum": round(sum(hyp) - sum(actual), 4) if len(hyp) == len(actual) else None,
        "note": "Shadow exits are simulated observations; differences are descriptive, not proof a different exit would fill at that price.",
    }

    cf_valid = [dict(r) for r in counterfactual_rows if r.get("counterfactual_settled_at") and _num(r.get("counterfactual_pnl_cents")) is not None]
    cf_summary = _pnl_summary([
        {"settled_at": r.get("counterfactual_settled_at"), "pnl_cents": r.get("counterfactual_pnl_cents"), "created_at": r.get("created_at")}
        for r in cf_valid
    ])
    cf_by_status = _group_by([
        {"settled_at": r.get("counterfactual_settled_at"), "pnl_cents": r.get("counterfactual_pnl_cents"),
         "decision_status": r.get("decision_status"), "entry_cents": r.get("entry_ask_cents")}
        for r in cf_valid
    ], lambda r: r.get("decision_status") or "unknown")

    eval_events = [e for e in decision_events if e.get("event_type") == "signal_evaluation"]
    status_counts: dict[str, int] = defaultdict(int)
    for event in eval_events:
        status_counts[str(event.get("decision") or "unknown")] += 1
    alert_status_counts: dict[str, int] = defaultdict(int)
    for event in decision_events:
        if event.get("event_type") == "alert_status":
            alert_status_counts[str(event.get("decision") or "unknown")] += 1

    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "summary": _pnl_summary(annotated),
        "rolling": {"last_20_settled": _pnl_summary(recent20), "last_50_settled": _pnl_summary(recent50)},
        "by_entry_price_bucket": price_perf,
        "by_chart_trend_alignment": alignment_perf,
        "by_chart_evidence_score": score_perf,
        "trend_stop_shadow_comparison": shadow_comparison,
        "rejected_signal_counterfactuals": {
            "summary": cf_summary,
            "by_decision_status": cf_by_status,
            "recent": cf_valid[:25],
            "note": "Hypothetical one-contract hold-to-settlement results include the observed entry ask and entry fee; they do not include stop/exit behavior and are not actual trades.",
        },
        "decision_event_counts": {"signal_evaluations_by_status": dict(status_counts), "alert_transitions_by_status": dict(alert_status_counts)},
        "recent_decisions": [dict(e) for e in list(decision_events)[:50]],
        "important": "Actual paper P&L, entry-price cohorts, chart alignment, evidence score, and hypothetical rejected-signal outcomes are separate measures. Scores are not win probabilities. Do not use small samples alone to tune or enable live trading.",
        "win_rate_interpretation": "The 95% Wilson interval describes sampling uncertainty in the observed win rate. Compare average net P&L, profit factor, fees and drawdown alongside win rate; higher win rate alone does not establish higher profitability.",
    }
