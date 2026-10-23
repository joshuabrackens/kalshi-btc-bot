"""Persistent post-trade feedback for the BTC paper bot.

This module records an immutable snapshot of each settled paper trade and its
entry-time features, then summarizes outcomes by observable cohorts. It never
changes entry thresholds, enables trading, or edits strategy rules automatically.
One trade is one observation, not proof of cause.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping, Sequence


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return value if isinstance(value, dict) else {}


def _side(value: Any) -> str | None:
    text = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if text in {"UP", "BUY_UP", "BULL", "BULLISH", "LONG", "BUY", "UPWARD"}:
        return "UP"
    if text in {"DOWN", "BUY_DOWN", "BEAR", "BEARISH", "SHORT", "SELL", "DOWNWARD"}:
        return "DOWN"
    return None


def _signed_side(value: Any) -> str | None:
    number = _number(value)
    if number is None:
        return _side(value)
    if number > 0:
        return "UP"
    if number < 0:
        return "DOWN"
    return "MIXED"


def _price_bucket(value: Any) -> str:
    price = _number(value)
    if price is None or not 0 < price < 100:
        return "unknown"
    if price < 50:
        return "1–49¢"
    if price < 65:
        return "50–64¢"
    if price < 80:
        return "65–79¢"
    if price <= 92:
        return "80–92¢"
    return "93–99¢"


def _score_bucket(value: Any) -> str:
    score = _number(value)
    if score is None:
        return "unknown"
    if score < 40:
        return "0–39"
    if score < 60:
        return "40–59"
    if score < 75:
        return "60–74"
    return "75–100"


def _align(side: str | None, bias: str | None) -> str:
    if side not in {"UP", "DOWN"}:
        return "unknown"
    bias_side = _side(bias)
    if bias_side in {"UP", "DOWN"}:
        return "aligned" if side == bias_side else "opposed"
    if str(bias or "").upper() == "MIXED":
        return "mixed"
    return "unknown"


def _first_number(*values: Any) -> float | None:
    for value in values:
        parsed = _number(value)
        if parsed is not None:
            return parsed
    return None


def _build_feature_snapshot(trade: Mapping[str, Any], signal: Mapping[str, Any] | None) -> dict[str, Any]:
    signal = signal or {}
    payload = _payload(signal.get("payload_json"))
    strategies = payload.get("strategies") if isinstance(payload.get("strategies"), dict) else {}
    chart = payload.get("chart_trend_at_decision")
    if not isinstance(chart, dict):
        chart = payload.get("btc_trend_monitor") if isinstance(payload.get("btc_trend_monitor"), dict) else {}
    fusion = payload.get("fusion") if isinstance(payload.get("fusion"), dict) else {}
    if not chart and isinstance(fusion.get("chart_trend_at_decision"), dict):
        chart = fusion["chart_trend_at_decision"]
    trends = strategies.get("multi_timeframe_trend") if isinstance(strategies.get("multi_timeframe_trend"), dict) else {}
    timeframe_values = trends.get("timeframes") if isinstance(trends.get("timeframes"), dict) else {}
    momentum = strategies.get("momentum") if isinstance(strategies.get("momentum"), dict) else {}
    volume = strategies.get("volume_confirmation") if isinstance(strategies.get("volume_confirmation"), dict) else {}
    structure = strategies.get("market_structure") if isinstance(strategies.get("market_structure"), dict) else {}
    box = strategies.get("box_theory") if isinstance(strategies.get("box_theory"), dict) else {}
    volatility = strategies.get("volatility") if isinstance(strategies.get("volatility"), dict) else {}
    ext = payload.get("external_signal") if isinstance(payload.get("external_signal"), dict) else {}

    direction = _side(trade.get("direction")) or str(trade.get("direction") or "UNKNOWN").upper()
    bias = _side(chart.get("candidate_bias") or chart.get("trend_bias") or signal.get("trend_bias"))
    evidence = _first_number(chart.get("evidence_score"), signal.get("trend_evidence_score"))
    score = _first_number(trade.get("entry_signal_score"), signal.get("score"))
    momentum_direction = _signed_side(momentum.get("direction"))
    if momentum_direction == "MIXED":
        momentum_alignment = "mixed"
    elif momentum_direction in {"UP", "DOWN"} and direction in {"UP", "DOWN"}:
        momentum_alignment = "aligned" if momentum_direction == direction else "opposed"
    else:
        momentum_alignment = "unknown"

    btc_price = _first_number(trade.get("entry_btc_price"), signal.get("btc_price"))
    strike = _number(signal.get("strike_price"))
    dist_from_strike = abs(btc_price - strike) if btc_price is not None and strike is not None else None
    recent_reasons = payload.get("reasons") if isinstance(payload.get("reasons"), list) else []
    reasons = [str(x)[:240] for x in recent_reasons[:6] if isinstance(x, (str, int, float))]
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    warnings = [str(x)[:240] for x in warnings[:6] if isinstance(x, (str, int, float))]

    snapshot = {
        "direction": direction,
        "signal_source": str(trade.get("entry_signal_source") or "unknown"),
        "entry_cents": _number(trade.get("entry_cents")),
        "entry_price_bucket": _price_bucket(trade.get("entry_cents")),
        "entry_fee_cents_per_contract": _number(trade.get("fee_cents_per_contract")),
        "entry_signal_score": score,
        "evidence_score_bucket": _score_bucket(evidence),
        "internal_score": _number(signal.get("score")),
        "chart_trend_bias": bias or (str(chart.get("candidate_bias") or "").upper() or None),
        "trend_evidence_score": evidence,
        "trend_snapshot_age_seconds": _first_number(chart.get("age_seconds"), signal.get("trend_snapshot_age_seconds")),
        "trend_alignment": _align(direction, bias),
        "timeframes": {str(k): str(v) for k, v in timeframe_values.items() if k in {"1m", "5m", "15m"}},
        "momentum_direction": momentum_direction,
        "momentum_alignment": momentum_alignment,
        "rsi_1m": _number(momentum.get("rsi_1m")),
        "volume_confirmation": ("yes" if volume.get("confirmed") is True else "no" if volume.get("confirmed") is False else "unknown"),
        "market_structure_direction": _signed_side(structure.get("direction")),
        "market_structure_label": str(structure.get("label") or "unknown"),
        "box_state": str(box.get("state") or "unknown"),
        "box_direction": _signed_side(box.get("direction")),
        "chase_warning": bool(volatility.get("chase_warning")) if isinstance(volatility.get("chase_warning"), bool) else None,
        "three_candle_abs_move_pct": _number(volatility.get("three_candle_abs_move_pct")),
        "spread_cents": _number(signal.get("spread_cents")),
        "seconds_remaining_at_signal": _number(signal.get("seconds_remaining")),
        "btc_price_at_entry": btc_price,
        "strike_price": strike,
        "absolute_distance_from_strike": dist_from_strike,
        "alert_age_seconds": _number(ext.get("age_seconds")),
        "decision_status_at_entry": str(signal.get("decision_status") or "unknown"),
        "entry_reasons": reasons,
        "entry_warnings": warnings,
    }
    return snapshot


def _build_review(trade: Mapping[str, Any], features: Mapping[str, Any], result: str) -> dict[str, Any]:
    pnl = _number(trade.get("pnl_cents")) or 0.0
    alignment = str(features.get("trend_alignment") or "unknown")
    momentum_alignment = str(features.get("momentum_alignment") or "unknown")
    volume = str(features.get("volume_confirmation") or "unknown")
    chase = features.get("chase_warning")
    helpful: list[str] = []
    review: list[str] = []
    if result == "profit":
        helpful.append("The recorded simulated trade finished with positive net P&L after the stored fees.")
        if alignment == "aligned":
            helpful.append("Entry-time chart bias aligned with the trade direction; test this pattern across a larger sample.")
        if momentum_alignment == "aligned":
            helpful.append("Entry-time momentum aligned with the trade direction; this is an observation, not causal proof.")
        if volume == "yes":
            helpful.append("Volume confirmation was present at entry; compare with losing trades before drawing conclusions.")
        if not any("aligned" in item.lower() for item in helpful[1:]):
            review.append("Do not infer a winning rule from the outcome alone; compare its entry features with other settled trades.")
    elif result == "loss":
        review.append("The recorded simulated trade finished with negative net P&L after the stored fees.")
        if alignment == "opposed":
            review.append("Chart bias opposed the trade direction at entry; measure this cohort before changing the gate.")
        if momentum_alignment == "opposed":
            review.append("Momentum opposed the trade direction at entry; check whether this pattern repeats.")
        if chase is True:
            review.append("A chase warning was recorded at entry; evaluate its loss rate against trades without that warning.")
        if volume == "no":
            review.append("Volume confirmation was absent at entry; compare this cohort with volume-confirmed trades.")
        if len(review) == 1:
            review.append("No single contributing factor can be assigned from this outcome; review the saved entry-time context.")
    else:
        review.append("The recorded net P&L was approximately break-even; retain it in cohorts without labeling it a win or loss.")
    if str(trade.get("outcome") or "").upper() in {"UP", "DOWN"}:
        settled_side = str(trade.get("outcome")).upper()
        direction = str(features.get("direction") or "").upper()
        direction_hit = direction == settled_side
    else:
        direction_hit = None
    return {
        "result": result,
        "net_pnl_cents": round(pnl, 4),
        "positive_observations": helpful,
        "review_flags": review,
        "directional_settlement_hit": direction_hit,
        "interpretation_note": (
            "This per-trade review is descriptive feedback, not proof any factor caused the outcome. "
            "The learning loop records every result and updates aggregate cohorts; it never changes strategy rules from one trade."
        ),
    }


def sync_settled_trade_learning(conn: Any, *, batch_size: int = 250) -> int:
    """Persist immutable feature/outcome snapshots for settled paper trades not yet reviewed."""
    batch_size = max(1, min(1000, int(batch_size)))
    rows = conn.execute(
        """SELECT p.id,p.created_at,p.settled_at,p.market_ticker,p.direction,p.contracts,p.entry_cents,
                  p.fee_cents_per_contract,p.outcome,p.pnl_cents,p.entry_signal_source,p.entry_signal_log_id,
                  p.entry_signal_score,p.entry_btc_price,
                  s.created_at,s.direction,s.score,s.btc_price,s.strike_price,s.seconds_remaining,
                  s.entry_ask_cents,s.spread_cents,s.trend_bias,s.trend_evidence_score,
                  s.trend_snapshot_age_seconds,s.payload_json
           FROM paper_trades p
           LEFT JOIN signal_log s ON s.id=COALESCE(
               p.entry_signal_log_id,
               (SELECT s2.id FROM signal_log s2
                WHERE p.external_signal_id IS NOT NULL
                  AND s2.external_signal_id=p.external_signal_id
                ORDER BY CASE WHEN s2.decision_status='paper_opened' THEN 0 ELSE 1 END, s2.id DESC
                LIMIT 1)
           )
           WHERE p.settled_at IS NOT NULL AND p.pnl_cents IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM trade_learning l WHERE l.trade_id=p.id)
           ORDER BY p.id DESC LIMIT ?""",
        (batch_size,),
    ).fetchall()
    added = 0
    for row in rows:
        trade = {
            "id": row[0], "created_at": row[1], "settled_at": row[2], "market_ticker": row[3],
            "direction": row[4], "contracts": row[5], "entry_cents": row[6],
            "fee_cents_per_contract": row[7], "outcome": row[8], "pnl_cents": row[9],
            "entry_signal_source": row[10], "entry_signal_log_id": row[11],
            "entry_signal_score": row[12], "entry_btc_price": row[13],
        }
        signal = {
            "created_at": row[14], "direction": row[15], "score": row[16], "btc_price": row[17],
            "strike_price": row[18], "seconds_remaining": row[19], "entry_ask_cents": row[20],
            "spread_cents": row[21], "trend_bias": row[22], "trend_evidence_score": row[23],
            "trend_snapshot_age_seconds": row[24], "payload_json": row[25],
        } if row[14] is not None else None
        pnl = _number(trade["pnl_cents"])
        if pnl is None:
            continue
        result = "profit" if pnl > 0.005 else "loss" if pnl < -0.005 else "breakeven"
        features = _build_feature_snapshot(trade, signal)
        review = _build_review(trade, features, result)
        cursor = conn.execute(
            """INSERT INTO trade_learning
               (trade_id,created_at,settled_at,market_ticker,direction,contracts,entry_cents,
                fee_cents_per_contract,trade_outcome,pnl_result,pnl_cents,entry_signal_log_id,signal_source,
                entry_score,trend_bias,trend_evidence_score,trend_snapshot_age_seconds,trend_alignment,
                entry_price_bucket,evidence_bucket,volume_confirmation,momentum_alignment,chase_warning,
                feature_snapshot_json,review_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(trade_id) DO NOTHING""",
            (int(trade["id"]), str(trade["created_at"] or ""), str(trade["settled_at"]),
             trade["market_ticker"], features["direction"], int(trade["contracts"] or 0),
             _number(trade["entry_cents"]), _number(trade["fee_cents_per_contract"]),
             str(trade["outcome"] or "unknown"), result, pnl, trade["entry_signal_log_id"],
             features["signal_source"], int(features["entry_signal_score"]) if _number(features["entry_signal_score"]) is not None else None,
             features["chart_trend_bias"], int(features["trend_evidence_score"]) if _number(features["trend_evidence_score"]) is not None else None,
             _number(features["trend_snapshot_age_seconds"]), features["trend_alignment"],
             features["entry_price_bucket"], features["evidence_score_bucket"], features["volume_confirmation"],
             features["momentum_alignment"], (1 if features["chase_warning"] is True else 0 if features["chase_warning"] is False else None),
             json.dumps(features, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
             json.dumps(review, ensure_ascii=False, separators=(",", ":"), allow_nan=False)),
        )
        if getattr(cursor, "rowcount", 0) > 0:
            added += 1
    conn.commit()
    return added


def _parse_json_dict(value: Any) -> dict[str, Any]:
    return _payload(value)


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pnl_values = [_number(row.get("pnl_cents")) or 0.0 for row in rows]
    wins = sum(1 for p in pnl_values if p > 0.005)
    losses = sum(1 for p in pnl_values if p < -0.005)
    flats = len(pnl_values) - wins - losses
    gross_wins = sum(p for p in pnl_values if p > 0)
    gross_losses = abs(sum(p for p in pnl_values if p < 0))
    return {
        "trades_reviewed": len(rows), "profitable": wins, "unprofitable": losses, "breakeven": flats,
        "win_rate_percent": round(wins * 100 / len(rows), 2) if rows else None,
        "net_pnl_cents": round(sum(pnl_values), 4), "net_pnl_dollars": round(sum(pnl_values) / 100, 4),
        "average_net_pnl_cents": round(sum(pnl_values) / len(rows), 4) if rows else None,
        "gross_profit_cents": round(gross_wins, 4), "gross_loss_cents": round(gross_losses, 4),
        "profit_factor": round(gross_wins / gross_losses, 4) if gross_losses > 0 else ("no_recorded_losses" if rows else None),
        "sample_note": "Small and potentially changing samples are noisy; use this as descriptive feedback, not a forecast." if len(rows) < 50 else "Larger sample available, but any rule changes still require out-of-sample validation.",
    }


def _cohort_rows(rows: Sequence[Mapping[str, Any]], key: str, label: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        value = str(row.get(key) or "unknown")
        if value not in {"unknown", "None", ""}:
            groups[value].append(row)
    output = []
    for segment, group in sorted(groups.items()):
        stats = _summary(group)
        output.append({
            "feature": label, "segment": segment, "settled_trades": stats["trades_reviewed"],
            "wins": stats["profitable"], "losses": stats["unprofitable"], "breakeven": stats["breakeven"],
            "win_rate_percent": stats["win_rate_percent"], "net_pnl_cents": stats["net_pnl_cents"],
            "net_pnl_dollars": stats["net_pnl_dollars"],
            "average_net_pnl_cents": stats["average_net_pnl_cents"],
            "sample_note": "Exploratory only; small cohort" if len(group) < 20 else "Descriptive cohort; validate out of sample",
        })
    return output


def _research_notes(cohorts: Mapping[str, Sequence[Mapping[str, Any]]], *, min_group: int = 10) -> list[str]:
    """Report only large cohort differences; never silently change trading rules."""
    notes: list[str] = []
    for key, items in cohorts.items():
        eligible = [x for x in items if int(x.get("settled_trades") or 0) >= min_group and _number(x.get("win_rate_percent")) is not None]
        if len(eligible) < 2:
            continue
        high = max(eligible, key=lambda x: float(x.get("win_rate_percent") or 0))
        low = min(eligible, key=lambda x: float(x.get("win_rate_percent") or 0))
        difference = float(high["win_rate_percent"]) - float(low["win_rate_percent"])
        if difference >= 15:
            notes.append(
                f"Review {high['feature']} cohorts: '{high['segment']}' has a {float(high['win_rate_percent']):.1f}% observed win rate "
                f"({high['settled_trades']} trades) versus '{low['segment']}' at {float(low['win_rate_percent']):.1f}% "
                f"({low['settled_trades']} trades). This is a research lead, not evidence of causation or an automatic rule change."
            )
    if not notes:
        notes.append("No cohort difference currently meets the conservative review threshold. Keep collecting outcomes; do not tune rules from a single trade.")
    return notes[:8]


def build_learning_report(conn: Any, *, limit: int = 10000) -> dict[str, Any]:
    """Read the persistent per-trade feedback journal and summarize candidate patterns."""
    limit = max(1, min(10000, int(limit)))
    raw_rows = conn.execute(
        """SELECT trade_id,created_at,settled_at,market_ticker,direction,contracts,entry_cents,
                  fee_cents_per_contract,trade_outcome,pnl_result,pnl_cents,entry_signal_log_id,signal_source,
                  entry_score,trend_bias,trend_evidence_score,trend_snapshot_age_seconds,trend_alignment,
                  entry_price_bucket,evidence_bucket,volume_confirmation,momentum_alignment,chase_warning,
                  feature_snapshot_json,review_json
           FROM trade_learning ORDER BY settled_at DESC, trade_id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    keys = ["trade_id", "created_at", "settled_at", "market_ticker", "direction", "contracts", "entry_cents",
            "fee_cents_per_contract", "trade_outcome", "pnl_result", "pnl_cents", "entry_signal_log_id", "signal_source",
            "entry_score", "trend_bias", "trend_evidence_score", "trend_snapshot_age_seconds", "trend_alignment",
            "entry_price_bucket", "evidence_bucket", "volume_confirmation", "momentum_alignment", "chase_warning",
            "feature_snapshot_json", "review_json"]
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        item = dict(zip(keys, raw))
        features = _parse_json_dict(item.pop("feature_snapshot_json", "{}"))
        review = _parse_json_dict(item.pop("review_json", "{}"))
        item["features"] = features
        item["review"] = review
        rows.append(item)
    summary = _summary(rows)

    cohort_defs = {
        "trend_alignment": ("trend_alignment", "Chart trend alignment"),
        "entry_price_bucket": ("entry_price_bucket", "Entry price"),
        "evidence_bucket": ("evidence_bucket", "Trend evidence score"),
        "volume_confirmation": ("volume_confirmation", "Volume confirmation"),
        "momentum_alignment": ("momentum_alignment", "Momentum alignment"),
    }
    cohorts: dict[str, list[dict[str, Any]]] = {}
    flat_cohorts: list[dict[str, Any]] = []
    for output_key, (field, label) in cohort_defs.items():
        values = _cohort_rows(rows, field, label)
        cohorts[output_key] = values
        flat_cohorts.extend(values)
    # Do not adapt parameters automatically; research notes are deliberately gated by sample size.
    return {
        "mode": "SIMULATION_ONLY",
        "system": "post_trade_feedback_v1",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "automatic_strategy_changes_enabled": False,
        "policy": "Every settled paper trade is stored once with its entry-time feature snapshot and net outcome. Cohort insights are descriptive and are never applied automatically to entry thresholds or trade permissions.",
        "summary": summary,
        "cohorts": cohorts,
        "cohort_rows": flat_cohorts,
        "research_notes": _research_notes(cohorts),
        "latest_review": rows[0] if rows else None,
        "recent_reviews": rows[:10],
        "important": (
            "A result is classified by recorded net P&L; this is separate from the official market direction and from exit reason labels. "
            "Entry features are observations, not proven causes. No threshold changes happen after a single trade. Hypothetical rejected-signal and shadow-exit outcomes remain separate."
        ),
    }
