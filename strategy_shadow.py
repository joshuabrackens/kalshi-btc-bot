"""Paper-only, same-entry strategy shadow comparison.

The candidate tested here is a hold-to-official-settlement exit policy using the
same market, direction, contract count, entry ask, and entry fee as the current
engine. It deliberately does not submit orders, change actual positions, or
alter entry/exit rules. It is a controlled test of the highest observed
historical exit-path cohort, not a claim that the whole strategy achieved that
win rate.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Callable, Mapping

HOLD_TO_SETTLEMENT_KEY = "hold_to_settlement_v1"
HOLD_TO_SETTLEMENT_LABEL = "Same entry + hold to official settlement"


def _now_utc(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _finite_number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
        return result if isfinite(result) else fallback
    except (TypeError, ValueError, OverflowError):
        return fallback


def _official_outcome(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"yes", "up"}:
        return "UP"
    if text in {"no", "down"}:
        return "DOWN"
    return None


def _comparison_cohort(created_at: Any) -> str:
    """Separate prospective shadow results from history using a fixed start time."""
    raw_start = str(os.getenv("PAPER_STRATEGY_SHADOW_START_AT", "")).strip()
    if not raw_start:
        return "historical_backfill"
    try:
        start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return "prospective" if created.astimezone(timezone.utc) >= start.astimezone(timezone.utc) else "historical_backfill"
    except (ValueError, TypeError, OverflowError):
        return "historical_backfill"


def _settlement_pnl(direction: str, outcome: str, contracts: int,
                     entry_cents: float, fee_cents_per_contract: float) -> float:
    gross = 100.0 * int(contracts) if str(direction).upper() == outcome else 0.0
    return gross - float(entry_cents) * int(contracts) - float(fee_cents_per_contract) * int(contracts)


def ensure_hold_to_settlement_shadow_rows(conn: Any) -> dict[str, int]:
    """Create one idempotent candidate row per actual paper entry, including history."""
    rows = conn.execute(
        """SELECT p.id,p.created_at,p.settled_at,p.market_ticker,p.direction,p.contracts,
                  p.entry_cents,p.fee_cents_per_contract,p.outcome,p.notes,
                  COALESCE(tl.feature_snapshot_json,sl.payload_json,'{}')
           FROM paper_trades p
           LEFT JOIN strategy_shadow_trades existing
             ON existing.baseline_trade_id=p.id AND existing.strategy_key=?
           LEFT JOIN trade_learning tl ON tl.trade_id=p.id
           LEFT JOIN signal_log sl ON sl.id=p.entry_signal_log_id
           WHERE existing.id IS NULL
           ORDER BY p.id""",
        (HOLD_TO_SETTLEMENT_KEY,),
    ).fetchall()

    created = 0
    already_resolved = 0
    for row in rows:
        (trade_id, created_at, actual_settled_at, ticker, direction, contracts,
         entry_cents, fee_cents, actual_outcome, actual_notes, feature_json) = row
        outcome = _official_outcome(actual_outcome)
        is_official = outcome is not None
        qty = max(0, int(contracts or 0))
        entry = _finite_number(entry_cents)
        fee = _finite_number(fee_cents)
        pnl = _settlement_pnl(str(direction or ""), outcome, qty, entry, fee) if is_official else None
        state = "settled" if is_official else "pending"
        note = (
            "Backfilled from official market outcome already recorded on the baseline trade."
            if is_official else
            "Shadow only: same entry and fees, but hold this contract until official market settlement."
        )
        if actual_notes and "official" in str(actual_notes).lower():
            note += " Baseline note confirms official settlement."
        conn.execute(
            """INSERT INTO strategy_shadow_trades
               (baseline_trade_id,strategy_key,created_at,settled_at,market_ticker,direction,
                contracts,entry_cents,fee_cents_per_contract,outcome,pnl_cents,state,
                last_checked_at,last_error,comparison_cohort,feature_snapshot_json,notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(trade_id), HOLD_TO_SETTLEMENT_KEY, str(created_at or _now_utc().isoformat()),
             str(actual_settled_at) if is_official and actual_settled_at else None,
             str(ticker or ""), str(direction or "").upper(), qty, entry, fee,
             outcome, pnl, state,
             str(actual_settled_at) if is_official and actual_settled_at else None,
             None, _comparison_cohort(created_at), str(feature_json or "{}"), note),
        )
        created += 1
        already_resolved += int(is_official)
    if created:
        conn.commit()
    return {"created": created, "already_resolved": already_resolved}


def process_hold_to_settlement_shadow(
    conn: Any,
    get_market: Callable[[str], Mapping[str, Any]],
    *,
    max_market_checks: int = 1,
    retry_seconds: int = 60,
    now: datetime | None = None,
) -> dict[str, int]:
    """Resolve a small, rate-limited batch of stopped historical/current shadows."""
    backfill = ensure_hold_to_settlement_shadow_rows(conn)
    current = _now_utc(now)
    cutoff = (current - timedelta(seconds=max(1, int(retry_seconds)))).isoformat()
    limit = max(0, min(10, int(max_market_checks)))
    if limit == 0:
        return {"created": backfill["created"], "already_resolved": backfill["already_resolved"],
                "checked": 0, "newly_settled": 0, "pending": 0, "errors": 0}

    pending = conn.execute(
        """SELECT sh.id,sh.market_ticker,sh.direction,sh.contracts,sh.entry_cents,
                  sh.fee_cents_per_contract
           FROM strategy_shadow_trades sh
           JOIN paper_trades p ON p.id=sh.baseline_trade_id
           WHERE sh.strategy_key=? AND sh.state='pending'
             AND p.settled_at IS NOT NULL
             AND (sh.last_checked_at IS NULL OR sh.last_checked_at<=?)
           ORDER BY CASE WHEN sh.last_checked_at IS NULL THEN 0 ELSE 1 END,
                    sh.baseline_trade_id DESC
           LIMIT ?""",
        (HOLD_TO_SETTLEMENT_KEY, cutoff, limit),
    ).fetchall()

    checked = settled = errors = 0
    for shadow_id, ticker, direction, contracts, entry, fee in pending:
        checked += 1
        checked_at = current.isoformat()
        try:
            payload = get_market(str(ticker))
            market = payload.get("market", payload) if isinstance(payload, Mapping) else {}
            if not isinstance(market, Mapping):
                market = {}
            status = str(market.get("status") or "").strip().lower()
            outcome = _official_outcome(market.get("result"))
            if status in {"settled", "finalized"} and outcome:
                pnl = _settlement_pnl(str(direction), outcome, int(contracts), _finite_number(entry), _finite_number(fee))
                conn.execute(
                    """UPDATE strategy_shadow_trades
                       SET state='settled',settled_at=?,outcome=?,pnl_cents=?,
                           last_checked_at=?,last_error=NULL,
                           notes=COALESCE(notes,'') || ?
                       WHERE id=? AND state='pending'""",
                    (checked_at, outcome, pnl, checked_at,
                     f" Official result verified by read-only market lookup ({status}).",
                     int(shadow_id)),
                )
                settled += 1
            else:
                detail = None
                if status in {"settled", "finalized"} and not outcome:
                    detail = "Market status is final but the official result is missing or unrecognized."
                conn.execute(
                    """UPDATE strategy_shadow_trades
                       SET last_checked_at=?,last_error=?
                       WHERE id=? AND state='pending'""",
                    (checked_at, detail, int(shadow_id)),
                )
        except Exception as exc:
            errors += 1
            conn.execute(
                """UPDATE strategy_shadow_trades
                   SET last_checked_at=?,last_error=?
                   WHERE id=? AND state='pending'""",
                (checked_at, f"{type(exc).__name__}: {str(exc)[:240]}", int(shadow_id)),
            )
    if checked:
        conn.commit()
    pending_count = conn.execute(
        """SELECT COUNT(*) FROM strategy_shadow_trades
           WHERE strategy_key=? AND state='pending'""",
        (HOLD_TO_SETTLEMENT_KEY,),
    ).fetchone()[0]
    return {
        "created": backfill["created"],
        "already_resolved": backfill["already_resolved"],
        "checked": checked,
        "newly_settled": settled,
        "pending": int(pending_count or 0),
        "errors": errors,
    }


def _metrics(pnls: list[float]) -> dict[str, Any]:
    wins = sum(1 for pnl in pnls if pnl > 0)
    losses = sum(1 for pnl in pnls if pnl < 0)
    flat = len(pnls) - wins - losses
    total = sum(pnls)
    return {
        "settled_trades": len(pnls),
        "positive_net_count": wins,
        "negative_net_count": losses,
        "breakeven_count": flat,
        "positive_net_rate_percent": round(100.0 * wins / len(pnls), 2) if pnls else None,
        "net_pnl_cents": round(total, 2),
        "average_net_pnl_cents": round(total / len(pnls), 2) if pnls else None,
        "sample_quality": (
            "insufficient sample" if len(pnls) < 20 else
            "descriptive; needs chronological out-of-sample validation"
        ),
    }


def _strategy_family(entry_source: Any, notes: Any) -> str:
    source = str(entry_source or "").strip().lower()
    note = str(notes or "").strip().lower()
    if note.startswith("auto paper entry"):
        return "legacy_auto_paper"
    if source == "hybrid" or note.startswith("tradingview-primary hybrid"):
        return "tradingview_hybrid"
    if source in {"internal", "tradingview"}:
        return f"{source}_only"
    return "unclassified_legacy"


def _exit_path(outcome: Any, notes: Any) -> str:
    outcome_text = str(outcome or "").strip().lower()
    note = str(notes or "").strip().lower()
    if "take-profit" in note or "take profit" in note or "take_profit" in outcome_text:
        return "take_profit_or_profit_protection"
    if "trend stop" in note or "chart-stop" in note or "trend-stop" in note:
        return "trend_or_chart_stop"
    if "paper stop triggered" in note:
        return "fixed_stop"
    if outcome_text in {"up", "down"} or "official kalshi result" in note or "final result" in note:
        return "held_to_official_settlement"
    return "other_exit_or_unknown"


def build_strategy_comparison_report(conn: Any, *, target_positive_net_rate_percent: float = 90.0) -> dict[str, Any]:
    """Report full-family performance and paired same-entry settlement-only shadows."""
    trade_rows = conn.execute(
        """SELECT p.id,p.created_at,p.settled_at,p.market_ticker,p.direction,p.contracts,
                  p.entry_cents,p.pnl_cents,p.outcome,p.notes,p.entry_signal_source,
                  sh.state,sh.settled_at,sh.pnl_cents,sh.last_checked_at,sh.last_error
           FROM paper_trades p
           LEFT JOIN strategy_shadow_trades sh
             ON sh.baseline_trade_id=p.id AND sh.strategy_key=?
           ORDER BY p.id""",
        (HOLD_TO_SETTLEMENT_KEY,),
    ).fetchall()

    families: dict[str, list[tuple[float, str, str]]] = {}
    exit_paths: dict[str, list[tuple[float, str, str]]] = {}
    paired_actual: list[float] = []
    paired_shadow: list[float] = []
    unresolved_count = settled_count = 0
    last_checked_at = None
    last_error_count = 0
    current_family_count = legacy_family_count = 0

    for row in trade_rows:
        (trade_id, created_at, actual_settled_at, ticker, direction, contracts,
         entry, actual_pnl, outcome, notes, source,
         shadow_state, shadow_settled_at, shadow_pnl, checked_at, last_error) = row
        last_checked_at = max(str(last_checked_at or ""), str(checked_at or "")) or None
        last_error_count += int(bool(last_error))
        if shadow_state == "settled" and shadow_pnl is not None:
            settled_count += 1
        elif shadow_state == "pending":
            unresolved_count += 1
        if actual_settled_at is None or actual_pnl is None:
            continue
        family = _strategy_family(source, notes)
        exit_path = _exit_path(outcome, notes)
        families.setdefault(family, []).append((_finite_number(actual_pnl), str(created_at or ""), str(ticker or "")))
        exit_paths.setdefault(exit_path, []).append((_finite_number(actual_pnl), str(created_at or ""), str(ticker or "")))
        if shadow_state == "settled" and shadow_pnl is not None:
            paired_actual.append(_finite_number(actual_pnl))
            paired_shadow.append(_finite_number(shadow_pnl))
        if family == "tradingview_hybrid":
            current_family_count += 1
        elif family == "legacy_auto_paper":
            legacy_family_count += 1

    family_results = []
    for key, entries in families.items():
        report = _metrics([item[0] for item in entries])
        family_results.append({
            "strategy_family": key,
            **report,
            "first_trade_at": min(item[1] for item in entries),
            "last_trade_at": max(item[1] for item in entries),
        })
    family_results.sort(
        key=lambda item: (
            item["positive_net_rate_percent"] if item["positive_net_rate_percent"] is not None else -1,
            item["net_pnl_cents"],
        ),
        reverse=True,
    )

    exit_results = []
    for key, entries in exit_paths.items():
        exit_results.append({"exit_path": key, **_metrics([item[0] for item in entries])})
    exit_results.sort(
        key=lambda item: (
            item["positive_net_rate_percent"] if item["positive_net_rate_percent"] is not None else -1,
            item["net_pnl_cents"],
        ),
        reverse=True,
    )

    actual_metrics = _metrics(paired_actual)
    shadow_metrics = _metrics(paired_shadow)
    net_delta = (
        round(shadow_metrics["net_pnl_cents"] - actual_metrics["net_pnl_cents"], 2)
        if paired_actual else None
    )
    rate_delta = (
        round(shadow_metrics["positive_net_rate_percent"] - actual_metrics["positive_net_rate_percent"], 2)
        if paired_actual else None
    )
    rows_total = len(trade_rows)
    shadow_available = conn.execute(
        """SELECT COUNT(*) FROM strategy_shadow_trades WHERE strategy_key=?""",
        (HOLD_TO_SETTLEMENT_KEY,),
    ).fetchone()[0]
    pending_ready = conn.execute(
        """SELECT COUNT(*) FROM strategy_shadow_trades sh
           JOIN paper_trades p ON p.id=sh.baseline_trade_id
           WHERE sh.strategy_key=? AND sh.state='pending' AND p.settled_at IS NOT NULL""",
        (HOLD_TO_SETTLEMENT_KEY,),
    ).fetchone()[0]

    highest_exit = next((item for item in exit_results if item["exit_path"] == "held_to_official_settlement"), None)
    historical_candidate_note = (
        f"The highest observed exit-path cohort was hold-to-official-settlement: "
        f"{highest_exit['positive_net_count']}/{highest_exit['settled_trades']} positive-net "
        f"({highest_exit['positive_net_rate_percent']:.2f}%) with {highest_exit['net_pnl_cents']:.2f} cents net P&L."
        if highest_exit and highest_exit["settled_trades"] else
        "No completed official-settlement exit cohort is available yet."
    )
    return {
        "mode": "SIMULATION_ONLY",
        "target_positive_net_rate_percent": float(target_positive_net_rate_percent),
        "candidate": {
            "strategy_key": HOLD_TO_SETTLEMENT_KEY,
            "label": HOLD_TO_SETTLEMENT_LABEL,
            "comparison_type": "same-entry exit-policy shadow",
            "description": (
                "The candidate duplicates each accepted baseline paper entry, uses the same side, contract count, ask and entry fee, "
                "and remains open until Kalshi publishes the official market result. It does not submit orders or modify baseline trades."
            ),
            "configured": str(os.getenv("PAPER_STRATEGY_SHADOW_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"},
        },
        "historical_strategy_families": family_results,
        "historical_exit_path_cohorts": exit_results,
        "paired_comparison": {
            "paired_resolved_trades": len(paired_actual),
            "current_engine": actual_metrics,
            "hold_to_settlement_candidate": shadow_metrics,
            "positive_net_rate_difference_percentage_points": rate_delta,
            "net_pnl_difference_cents": net_delta,
            "target_met_by_candidate": bool(
                shadow_metrics["positive_net_rate_percent"] is not None
                and shadow_metrics["settled_trades"] >= 20
                and shadow_metrics["positive_net_rate_percent"] >= float(target_positive_net_rate_percent)
                and shadow_metrics["net_pnl_cents"] > 0
            ),
            "interpretation": (
                "This is a paired, same-entry comparison. The candidate is not a separately proven entry strategy; it tests whether early exits help or hurt. "
                "A high historical hold-to-settlement rate is subject to selection bias because stopped trades have to be included using their later official outcomes."
            ),
        },
        "shadow_journal": {
            "baseline_trades_seen": rows_total,
            "shadow_rows_created": int(shadow_available or 0),
            "shadow_results_resolved": settled_count,
            "shadow_rows_pending": unresolved_count,
            "stopped_baseline_trades_waiting_for_official_resolution": int(pending_ready or 0),
            "last_market_check_at": last_checked_at,
            "last_market_check_error_count": last_error_count,
            "comparison_scope": "Only rows whose official market result has been retrieved are included in the paired comparison.",
        },
        "historical_candidate_note": historical_candidate_note,
        "full_strategy_note": (
            "Historical strategy-family rates include every settled trade in the family, including losing stop exits. "
            "Exit-path rates are conditional diagnostics and must not be treated as full-strategy win rates."
        ),
        "automatic_strategy_changes_enabled": False,
    }
