"""Persistent post-trade feedback and bounded paper-only adaptation.

Every settled trade is recorded once with entry-time features. A conservative
adaptive policy may tighten the preferred entry target after a minimum cohort
shows sufficiently poor fee-inclusive outcomes. It never loosens the configured
target, changes the hard cap/dollar risk limits, or enables live trading.
One trade is one observation, not proof of cause.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping, Sequence

from market_context import classify_market_regime, utc_calendar_context
from strategy_shadow import build_strategy_comparison_report


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
    if not timeframe_values and isinstance(chart.get("timeframes"), dict):
        timeframe_values = chart["timeframes"]
    momentum = strategies.get("momentum") if isinstance(strategies.get("momentum"), dict) else {}
    if not momentum and isinstance(chart.get("momentum"), dict):
        momentum = chart["momentum"]
    volume = strategies.get("volume_confirmation") if isinstance(strategies.get("volume_confirmation"), dict) else {}
    if not volume and isinstance(chart.get("volume_confirmation"), dict):
        volume = chart["volume_confirmation"]
    structure = strategies.get("market_structure") if isinstance(strategies.get("market_structure"), dict) else {}
    if not structure and isinstance(chart.get("market_structure"), dict):
        structure = chart["market_structure"]
    box = strategies.get("box_theory") if isinstance(strategies.get("box_theory"), dict) else {}
    if not box and isinstance(chart.get("box_theory"), dict):
        box = chart["box_theory"]
    volatility = strategies.get("volatility") if isinstance(strategies.get("volatility"), dict) else {}
    if not volatility and isinstance(chart.get("volatility"), dict):
        volatility = chart["volatility"]
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
        **utc_calendar_context(trade.get("created_at")),
        **classify_market_regime(
            timeframe_values, structure, box,
            chase_warning=volatility.get("chase_warning"),
            three_candle_abs_move_pct=volatility.get("three_candle_abs_move_pct"),
        ),
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



_ADAPTIVE_POLICY_KEY = "btc_hybrid_entry_target"
_ADAPTIVE_MIN_TRADES = 20
_ADAPTIVE_REEVALUATE_EVERY = 10
_ADAPTIVE_TARGET_STEP_CENTS = 1.0
_ADAPTIVE_MAX_TIGHTEN_CENTS = 5.0
_ADAPTIVE_FLOOR_CENTS = 65.0


def _configured_target() -> float:
    try:
        value = float(os.getenv("PAPER_ENTRY_TARGET_MAX_CENTS", "80"))
        return min(99.0, max(1.0, value))
    except (TypeError, ValueError, OverflowError):
        return 80.0


def update_adaptive_entry_policy(
    conn: Any, *, configured_target_cents: float, hard_cap_cents: float, enabled: bool = True,
) -> dict[str, Any]:
    """Update a one-way, bounded entry-target policy from settled hybrid trades.

    It evaluates the last 20 settled hybrid outcomes after the first 20 and
    then once per 10 additional newer hybrid trade IDs. A settled-journal backfill
    of older trades does not trigger repeated adjustments. Only if at least 8 observations fall in the top $0.05
    of the active target band, that subgroup has net loss of at least 8 cents,
    and losses are at least as common as wins, the preferred target tightens by
    one cent. It can tighten at most five cents below the initial configured
    target and never raises the target automatically. Hard cap and dollar limits
    are never altered.
    """
    configured = min(float(hard_cap_cents), max(1.0, min(99.0, float(configured_target_cents))))
    if not enabled:
        return {
            "enabled": False, "status": "disabled", "configured_target_cents": round(configured, 2),
            "effective_target_cents": round(configured, 2), "hard_cap_cents": round(float(hard_cap_cents), 2),
            "adjustment_count": 0,
            "last_sample_count": 0, "last_sample_net_pnl_cents": None,
            "reason": "Automatic target adjustment is disabled; the configured target is used unchanged.",
            "automatic_changes_scope": "preferred entry target only; hard cap and dollar risk limits are unchanged",
        }

    total = int(conn.execute("SELECT COUNT(*) FROM trade_learning WHERE signal_source='hybrid'").fetchone()[0] or 0)
    latest_trade_id = int(conn.execute("SELECT COALESCE(MAX(trade_id),0) FROM trade_learning WHERE signal_source='hybrid'").fetchone()[0] or 0)
    state = conn.execute(
        """SELECT base_target_cents,effective_target_cents,last_evaluated_trade_count,
                  last_evaluated_latest_trade_id,adjustment_count,last_sample_count,
                  last_sample_net_pnl_cents,last_reason
           FROM adaptive_policy_state WHERE policy_key=? LIMIT 1""", (_ADAPTIVE_POLICY_KEY,)
    ).fetchone()
    if state:
        previous_persisted_target = _number(state[1]) or configured
        previous_adjustment_count = int(state[4] or 0)
        original_base = min(float(state[0]), configured)
        effective = min(configured, float(state[1]), original_base)
        last_evaluated_count = int(state[2] or 0)
        last_evaluated_latest_trade_id = int(state[3] or 0)
        adjustments = int(state[4] or 0)
        last_sample_count = int(state[5] or 0)
        last_sample_net = _number(state[6])
        reason = str(state[7] or "No adaptive review has run yet.")
    else:
        previous_persisted_target = configured
        previous_adjustment_count = 0
        original_base = configured
        effective = configured
        last_evaluated_count = 0
        last_evaluated_latest_trade_id = 0
        adjustments = 0
        last_sample_count = 0
        last_sample_net = None
        reason = f"Collecting settled hybrid outcomes; {max(0, _ADAPTIVE_MIN_TRADES-total)} more trade(s) required before the first adaptive review."

    # Never loosen the persisted policy after it has tightened. An explicit
    # operator change to a lower configured target is applied immediately.
    floor = max(_ADAPTIVE_FLOOR_CENTS, original_base - _ADAPTIVE_MAX_TIGHTEN_CENTS)
    effective = max(floor, effective) if effective < original_base else original_base
    if effective > configured:
        effective = configured

    newer_hybrid_trades = int(conn.execute(
        "SELECT COUNT(*) FROM trade_learning WHERE signal_source='hybrid' AND trade_id>?",
        (last_evaluated_latest_trade_id,),
    ).fetchone()[0] or 0)
    should_evaluate = total >= _ADAPTIVE_MIN_TRADES and (
        last_evaluated_latest_trade_id == 0 or newer_hybrid_trades >= _ADAPTIVE_REEVALUATE_EVERY
    )
    status = "collecting_sample" if total < _ADAPTIVE_MIN_TRADES else "holding"
    sample_rows: list[tuple[Any, ...]] = []
    if should_evaluate:
        sample_rows = conn.execute(
            """SELECT entry_cents,pnl_cents FROM trade_learning WHERE signal_source='hybrid'
               ORDER BY settled_at DESC,trade_id DESC LIMIT ?""", (_ADAPTIVE_MIN_TRADES,)
        ).fetchall()
        last_sample_count = len(sample_rows)
        last_sample_net = round(sum(_number(r[1]) or 0.0 for r in sample_rows), 4)
        target_zone = [r for r in sample_rows if _number(r[0]) is not None and
                       effective - 5.0 <= float(r[0]) <= effective]
        target_zone_pnls = [_number(r[1]) or 0.0 for r in target_zone]
        target_zone_losses = sum(1 for value in target_zone_pnls if value < -0.005)
        target_zone_wins = sum(1 for value in target_zone_pnls if value > 0.005)
        zone_net = sum(target_zone_pnls)
        last_evaluated_count = total
        last_evaluated_latest_trade_id = latest_trade_id
        if (len(sample_rows) >= _ADAPTIVE_MIN_TRADES and len(target_zone) >= 8
                and zone_net <= -8.0 and target_zone_losses >= target_zone_wins
                and effective > floor):
            old = effective
            effective = max(floor, effective - _ADAPTIVE_TARGET_STEP_CENTS)
            adjustments += 1
            status = "tightened" if effective < old else "floor_reached"
            reason = (
                f"Automatically tightened the preferred entry target from {old:.0f}¢ to {effective:.0f}¢: "
                f"the last {len(sample_rows)} settled hybrid trades contained {len(target_zone)} trades at "
                f"{old-5:.0f}–{old:.0f}¢ with {zone_net:.1f}¢ net P&L and {target_zone_losses} losses vs {target_zone_wins} wins. "
                "Adjustment is one-way and capped; the hard cap and dollar risk limits are unchanged."
            )
        elif effective <= floor:
            status = "floor_reached"
            reason = f"Adaptive entry target is at its configured conservative floor of {floor:.0f}¢; no further tightening is allowed."
        else:
            status = "holding"
            if len(target_zone) < 8:
                reason = (f"Reviewed {len(sample_rows)} recent settled hybrid trades; only {len(target_zone)} fell in the active target zone, "
                          "so evidence is insufficient for an automatic adjustment. Target unchanged.")
            else:
                reason = (f"Reviewed {len(sample_rows)} recent settled hybrid trades; target-zone net P&L was {zone_net:.1f}¢ "
                          f"with {target_zone_wins} wins and {target_zone_losses} losses. Conservative adjustment criteria not met; target unchanged.")
    elif total < _ADAPTIVE_MIN_TRADES:
        status = "collecting_sample"
        reason = (f"Collected {total}/{_ADAPTIVE_MIN_TRADES} settled hybrid trades for the adaptive review. "
                  "Target remains at the configured value until the minimum sample is reached.")
    else:
        status = "holding"
        reason = (f"No new adaptive review due: {newer_hybrid_trades} of {_ADAPTIVE_REEVALUATE_EVERY} additional newer settled hybrid trades collected. "
                  f"Current preferred entry target remains {effective:.0f}¢.")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if effective < previous_persisted_target - 0.000001:
        automatic_change = adjustments > previous_adjustment_count
        change_type = "automatic_tightening" if automatic_change else "configured_target_lowering"
        history_reason = reason if automatic_change else (
            f"Configured preferred entry target was lowered from {previous_persisted_target:.2f}¢ to {effective:.2f}¢. {reason}"
        )
        details = {
            "automatic": automatic_change,
            "hard_cap_cents": round(float(hard_cap_cents), 2),
            "last_sample_count": last_sample_count,
            "last_sample_net_pnl_cents": last_sample_net,
            "adjustment_count": adjustments,
            "policy_scope": "preferred entry target only; no signal weights, hard cap, dollar budget, daily loss limit, or live-trading permission changed",
        }
        conn.execute(
            """INSERT INTO adaptive_policy_history
               (policy_key,changed_at,event_type,previous_target_cents,new_target_cents,reviewed_trade_count,
                latest_trade_id,sample_count,sample_net_pnl_cents,reason,details_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_ADAPTIVE_POLICY_KEY, now, change_type, previous_persisted_target, effective,
             total, latest_trade_id, last_sample_count, last_sample_net, history_reason,
             json.dumps(details, ensure_ascii=False, separators=(",", ":"), allow_nan=False)),
        )
    conn.execute(
        """INSERT INTO adaptive_policy_state
           (policy_key,base_target_cents,effective_target_cents,last_evaluated_trade_count,
            last_evaluated_latest_trade_id,adjustment_count,last_sample_count,last_sample_net_pnl_cents,last_reason,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(policy_key) DO UPDATE SET
             base_target_cents=excluded.base_target_cents,
             effective_target_cents=excluded.effective_target_cents,
             last_evaluated_trade_count=excluded.last_evaluated_trade_count,
             last_evaluated_latest_trade_id=excluded.last_evaluated_latest_trade_id,
             adjustment_count=excluded.adjustment_count,last_sample_count=excluded.last_sample_count,
             last_sample_net_pnl_cents=excluded.last_sample_net_pnl_cents,
             last_reason=excluded.last_reason,updated_at=excluded.updated_at""",
        (_ADAPTIVE_POLICY_KEY, original_base, effective, last_evaluated_count,
         last_evaluated_latest_trade_id, adjustments, last_sample_count, last_sample_net, reason, now),
    )
    conn.commit()
    persisted = conn.execute(
        "SELECT effective_target_cents FROM adaptive_policy_state WHERE policy_key=?", (_ADAPTIVE_POLICY_KEY,)
    ).fetchone()
    effective = min(configured, float(persisted[0])) if persisted else effective
    return {
        "enabled": True, "status": status, "configured_target_cents": round(configured, 2),
        "initial_target_cents": round(original_base, 2), "effective_target_cents": round(effective, 2),
        "hard_cap_cents": round(float(hard_cap_cents), 2), "adjustment_count": adjustments,
        "settled_hybrid_trades": total, "minimum_sample_required": _ADAPTIVE_MIN_TRADES,
        "reevaluate_every_additional_trades": _ADAPTIVE_REEVALUATE_EVERY,
        "last_sample_count": last_sample_count, "last_sample_net_pnl_cents": last_sample_net,
        "reason": reason,
        "automatic_changes_scope": "preferred entry target only; hard cap, trade dollar budget, daily loss limit, and live-trading permissions are never altered",
        "policy_key": _ADAPTIVE_POLICY_KEY, "updated_at": now,
    }

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


def _walk_forward_context_validation(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate whether descriptive context differences persist in a later holdout.

    Rows are expected newest-first. The newer 30% are held out from context
    discovery; the older 70% are the historical training slice. This is a simple
    sanity check on observed cohort stability, not a backtest of trading rules.
    """
    total = len(rows)
    holdout_count = max(1, int(total * 0.30 + 0.999999)) if total else 0
    training_rows = list(rows[holdout_count:])
    holdout_rows = list(rows[:holdout_count])
    base = {
        "method": "chronological 70/30 split; older settlements discover cohorts, newer settlements are held out",
        "historical_trade_count": total,
        "training_trade_count": len(training_rows),
        "holdout_trade_count": len(holdout_rows),
        "minimum_training_examples_per_cohort": 8,
        "minimum_holdout_examples_per_cohort": 5,
        "tested_cohorts": 0,
        "consistent_cohorts": 0,
        "cohorts": [],
        "weight_change_authorized": False,
    }
    if total < 30:
        return {
            **base,
            "status": "insufficient_sample",
            "note": "At least 30 settled trades are required even to run this basic holdout check; no signal weights are changed.",
        }

    dimensions = {
        "day_type_utc": "Weekday vs weekend (UTC)",
        "utc_time_bucket": "Time of day (UTC)",
        "trend_regime": "Trend/range regime",
        "volatility_regime": "Volatility warning state",
    }
    results: list[dict[str, Any]] = []
    for key, label in dimensions.items():
        training_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        holdout_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in training_rows:
            segment = str(row.get(key) or "unknown")
            if segment not in {"unknown", "None", ""}:
                training_groups[segment].append(row)
        for row in holdout_rows:
            segment = str(row.get(key) or "unknown")
            if segment not in {"unknown", "None", ""}:
                holdout_groups[segment].append(row)
        for segment in sorted(set(training_groups) & set(holdout_groups)):
            train_group = training_groups[segment]
            test_group = holdout_groups[segment]
            if len(train_group) < 8 or len(test_group) < 5:
                continue
            train_values = [(_number(x.get("pnl_cents")) or 0.0) for x in train_group]
            test_values = [(_number(x.get("pnl_cents")) or 0.0) for x in test_group]
            train_avg = sum(train_values) / len(train_values)
            test_avg = sum(test_values) / len(test_values)
            consistent = (train_avg > 0 and test_avg > 0) or (train_avg < 0 and test_avg < 0)
            if abs(train_avg) <= 0.005 or abs(test_avg) <= 0.005:
                consistent = False
                status = "not_reproduced_or_flat"
            elif consistent:
                status = "directionally_consistent"
            else:
                status = "not_reproduced"
            results.append({
                "feature": label,
                "segment": segment,
                "training_trades": len(train_group),
                "holdout_trades": len(test_group),
                "training_average_net_pnl_cents": round(train_avg, 4),
                "holdout_average_net_pnl_cents": round(test_avg, 4),
                "status": status,
            })
    consistent_count = sum(1 for item in results if item["status"] == "directionally_consistent")
    if results:
        status = "review_only"
        note = "Eligible context cohorts were compared against a later holdout. Consistency is a research lead, not permission to change weights."
    else:
        status = "insufficient_cohort_sample"
        note = "The total sample passed the minimum, but no shared context cohort met both per-split sample thresholds; keep collecting data."
    return {
        **base,
        "status": status,
        "tested_cohorts": len(results),
        "consistent_cohorts": consistent_count,
        "consistency_rate_percent": round(100 * consistent_count / len(results), 2) if results else None,
        "cohorts": results[:40],
        "note": note,
    }


def _build_rejection_feedback_report(conn: Any, *, review_min_samples: int = 20,
                                    target_positive_net_rate_percent: float = 90.0) -> dict[str, Any]:
    """Summarize settled counterfactuals for rejected signals; never changes entry rules."""
    rows = conn.execute(
        """SELECT id,created_at,market_ticker,proposed_direction,decision_status,entry_ask_cents,
                  counterfactual_pnl_cents,counterfactual_outcome,counterfactual_settled_at,payload_json
           FROM signal_log
           WHERE counterfactual_settled_at IS NOT NULL
             AND proposed_direction IN ('UP','DOWN')
             AND decision_status IN ('vetoed','skipped','wait_for_better_price','wait','blocked','ignored')
             AND counterfactual_pnl_cents IS NOT NULL
           ORDER BY counterfactual_settled_at DESC,id DESC"""
    ).fetchall()

    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        (signal_id, created_at, ticker, direction, status, entry_ask, pnl_value,
         outcome, settled_at, payload_json) = row
        pnl = _number(pnl_value)
        if pnl is None:
            continue
        payload = _payload(payload_json)
        fusion = payload.get("fusion") if isinstance(payload.get("fusion"), dict) else {}
        reasons = payload.get("reasons") if isinstance(payload.get("reasons"), list) else []
        fusion_reasons = fusion.get("reasons") if isinstance(fusion.get("reasons"), list) else []
        reason_text = str(payload.get("final_decision_reason") or "; ".join(
            str(item) for item in (fusion_reasons or reasons)[:8]
        ))
        lowered = reason_text.lower()
        normalized_status = str(status or "unknown").strip().lower()
        if "risk cooldown" in lowered or "circuit breaker" in lowered:
            category = "risk_cooldown"
        elif normalized_status == "vetoed" or "major conflict" in lowered or "vetoed" in lowered:
            category = "strategy_conflict_veto"
        elif normalized_status == "wait_for_better_price" or "preferred target" in lowered or "entry cap" in lowered:
            category = "entry_price_filter"
        elif "spread" in lowered:
            category = "spread_filter"
        elif "stale" in lowered or "freshness" in lowered or "too old" in lowered:
            category = "alert_freshness_filter"
        elif "trend monitor" in lowered or "trend snapshot" in lowered:
            category = "trend_data_filter"
        elif "orderbook" in lowered or "order-book" in lowered or "quote" in lowered or "market data" in lowered:
            category = "market_data_filter"
        else:
            category = "other_" + normalized_status.replace(" ", "_")

        item = groups.setdefault(category, {
            "rejection_class": category,
            "sample_count": 0,
            "positive_net_count": 0,
            "negative_net_count": 0,
            "breakeven_count": 0,
            "net_pnl_cents": 0.0,
            "examples": [],
        })
        item["sample_count"] += 1
        if pnl > 0:
            item["positive_net_count"] += 1
        elif pnl < 0:
            item["negative_net_count"] += 1
        else:
            item["breakeven_count"] += 1
        item["net_pnl_cents"] += pnl
        if len(item["examples"]) < 5:
            item["examples"].append({
                "signal_log_id": int(signal_id),
                "created_at": created_at,
                "settled_at": settled_at,
                "market_ticker": ticker,
                "proposed_direction": direction,
                "decision_status": status,
                "entry_ask_cents": _number(entry_ask),
                "counterfactual_outcome": outcome,
                "counterfactual_net_pnl_cents": round(pnl, 2),
                "reason": reason_text[:400],
            })

    result_groups = []
    for item in groups.values():
        n = item["sample_count"]
        positive_rate = 100.0 * item["positive_net_count"] / n if n else 0.0
        net_pnl = item["net_pnl_cents"]
        enough_data = n >= max(1, int(review_min_samples))
        qualifies = enough_data and positive_rate >= float(target_positive_net_rate_percent) and net_pnl > 0
        result_groups.append({
            **item,
            "net_pnl_cents": round(net_pnl, 2),
            "average_net_pnl_cents": round(net_pnl / n, 2) if n else None,
            "positive_net_rate_percent": round(positive_rate, 2) if n else None,
            "meets_review_gate": bool(qualifies),
            "automatic_rule_change": False,
            "review_status": (
                "candidate_for_out_of_sample_review" if qualifies else
                "insufficient_sample" if not enough_data else
                "does_not_meet_target_or_net_pnl_gate"
            ),
        })
    result_groups.sort(key=lambda item: (-item["sample_count"], item["rejection_class"]))
    return {
        "target_positive_net_rate_percent": float(target_positive_net_rate_percent),
        "review_min_samples": max(1, int(review_min_samples)),
        "settled_rejected_signal_count": sum(item["sample_count"] for item in result_groups),
        "groups": result_groups,
        "automatic_strategy_changes_enabled": False,
        "interpretation": (
            "These are hypothetical one-contract hold-to-settlement counterfactuals using the recorded entry ask and entry fee; "
            "they exclude exit/stop behavior and are not actual trades or a guarantee of future results. A group can become a review "
            "candidate only after the minimum sample, at least the requested positive-net rate, and positive aggregate P&L; it still "
            "requires out-of-sample validation and never overrides freshness, quote, price-cap, loss-budget, or cooldown safeguards."
        ),
    }


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
    calendar_context_sources = {"stored_entry_snapshot": 0, "derived_from_trade_timestamp": 0, "unknown": 0}
    for raw in raw_rows:
        item = dict(zip(keys, raw))
        features = _parse_json_dict(item.pop("feature_snapshot_json", "{}"))
        review = _parse_json_dict(item.pop("review_json", "{}"))

        # Older journal rows can predate calendar tagging. Their recorded entry
        # timestamp is still a factual source for UTC weekday/weekend/time buckets.
        # Derive only calendar labels; never infer missing trend/volatility features.
        calendar_keys = ("day_type_utc", "weekday_utc", "utc_time_bucket", "utc_hour")
        saved_calendar_present = any(
            features.get(key) not in (None, "", "unknown") for key in calendar_keys
        )
        derived_calendar = utc_calendar_context(item.get("created_at"))
        derived_calendar_used = False
        for key in calendar_keys:
            if features.get(key) in (None, "", "unknown"):
                value = derived_calendar.get(key)
                if value not in (None, "", "unknown"):
                    features[key] = value
                    derived_calendar_used = True
        if saved_calendar_present:
            source = "stored_entry_snapshot"
        elif derived_calendar_used:
            source = "derived_from_trade_timestamp"
        else:
            source = "unknown"
        features["calendar_context_source"] = source
        calendar_context_sources[source] += 1

        item["features"] = features
        item["review"] = review
        # Flatten calendar features after deterministic timestamp backfill.
        # Trend/range and volatility remain unknown when historical evidence is absent.
        for context_key in ("day_type_utc", "weekday_utc", "utc_time_bucket", "trend_regime", "volatility_regime"):
            item[context_key] = features.get(context_key, "unknown")
        item["calendar_context_source"] = source
        rows.append(item)
    summary = _summary(rows)

    policy_history_raw = conn.execute(
        """SELECT id,changed_at,event_type,previous_target_cents,new_target_cents,reviewed_trade_count,
                  latest_trade_id,sample_count,sample_net_pnl_cents,reason,details_json
           FROM adaptive_policy_history WHERE policy_key=? ORDER BY id DESC LIMIT 10""",
        (_ADAPTIVE_POLICY_KEY,),
    ).fetchall()
    policy_history_keys = ["id", "changed_at", "event_type", "previous_target_cents", "new_target_cents",
                           "reviewed_trade_count", "latest_trade_id", "sample_count", "sample_net_pnl_cents", "reason", "details_json"]
    policy_change_history: list[dict[str, Any]] = []
    for raw in policy_history_raw:
        event = dict(zip(policy_history_keys, raw))
        event["details"] = _parse_json_dict(event.pop("details_json", "{}"))
        policy_change_history.append(event)

    cohort_defs = {
        "trend_alignment": ("trend_alignment", "Chart trend alignment"),
        "entry_price_bucket": ("entry_price_bucket", "Entry price"),
        "evidence_bucket": ("evidence_bucket", "Trend evidence score"),
        "volume_confirmation": ("volume_confirmation", "Volume confirmation"),
        "momentum_alignment": ("momentum_alignment", "Momentum alignment"),
        "day_type_utc": ("day_type_utc", "Weekday vs weekend (UTC)"),
        "weekday_utc": ("weekday_utc", "Weekday (UTC)"),
        "utc_time_bucket": ("utc_time_bucket", "Time of day (UTC)"),
        "trend_regime": ("trend_regime", "Trend/range regime"),
        "volatility_regime": ("volatility_regime", "Volatility warning state"),
    }
    cohorts: dict[str, list[dict[str, Any]]] = {}
    flat_cohorts: list[dict[str, Any]] = []
    for output_key, (field, label) in cohort_defs.items():
        values = _cohort_rows(rows, field, label)
        cohorts[output_key] = values
        flat_cohorts.extend(values)
    adaptive_row = conn.execute(
        """SELECT base_target_cents,effective_target_cents,last_evaluated_trade_count,adjustment_count,
                  last_sample_count,last_sample_net_pnl_cents,last_reason,updated_at
           FROM adaptive_policy_state WHERE policy_key=? LIMIT 1""", (_ADAPTIVE_POLICY_KEY,)
    ).fetchone()
    adaptive_enabled = os.getenv("PAPER_ADAPTIVE_ENTRY_TARGET_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
    configured = min(_configured_target(), _number(os.getenv("PAPER_MAX_ENTRY_CENTS", "92")) or 92.0)
    if adaptive_row:
        adaptive_policy = {
            "enabled": adaptive_enabled,
            "status": ("disabled" if not adaptive_enabled else
                       "floor_reached" if "floor of" in str(adaptive_row[6] or "").lower() else
                       "tightened" if str(adaptive_row[6] or "").startswith("Automatically tightened") else
                       "holding" if int(adaptive_row[2] or 0) else "collecting_sample"),
            "initial_target_cents": _number(adaptive_row[0]),
            "configured_target_cents": configured,
            "effective_target_cents": configured if not adaptive_enabled else min(configured, _number(adaptive_row[1]) or configured),
            "adjustment_count": int(adaptive_row[3] or 0),
            "last_sample_count": int(adaptive_row[4] or 0),
            "last_sample_net_pnl_cents": _number(adaptive_row[5]),
            "reason": ("Automatic target adjustment is disabled; configured target is active." if not adaptive_enabled else str(adaptive_row[6] or "Waiting for data.")),
            "updated_at": adaptive_row[7],
            "automatic_changes_scope": "preferred entry target only; hard cap, trade dollar budget, daily loss limit, and live-trading permissions are never altered",
        }
    else:
        adaptive_policy = {
            "enabled": adaptive_enabled, "status": "collecting_sample" if adaptive_enabled else "disabled",
            "initial_target_cents": configured, "configured_target_cents": configured,
            "effective_target_cents": configured, "adjustment_count": 0, "last_sample_count": 0,
            "last_sample_net_pnl_cents": None,
            "reason": ("No adaptive state exists yet. Worker initializes it and collects at least 20 settled hybrid trades before considering a change." if adaptive_enabled else "Automatic target adjustment is disabled."),
            "automatic_changes_scope": "preferred entry target only; hard cap, trade dollar budget, daily loss limit, and live-trading permissions are never altered",
        }
    session_cohorts = cohorts.get("day_type_utc", [])
    weekend_cohort = next((item for item in session_cohorts if item.get("segment") == "weekend"), None)
    weekday_cohort = next((item for item in session_cohorts if item.get("segment") == "weekday"), None)
    session_review_flags: list[str] = []
    if weekend_cohort:
        weekend_n = int(weekend_cohort.get("settled_trades") or 0)
        weekend_rate = _number(weekend_cohort.get("win_rate_percent"))
        weekend_net = _number(weekend_cohort.get("net_pnl_cents"))
        if weekend_n >= 10 and weekend_rate is not None and weekend_net is not None and weekend_rate < 90 and weekend_net < 0:
            session_review_flags.append(
                f"Weekend cohort needs review: {weekend_rate:.2f}% positive-net across {weekend_n} settled trades "
                f"with net P&L {weekend_net:.2f} cents. This is an alert for investigation, not proof the calendar caused losses."
            )
    if weekday_cohort:
        weekday_n = int(weekday_cohort.get("settled_trades") or 0)
        weekday_rate = _number(weekday_cohort.get("win_rate_percent"))
        if weekday_n >= 10 and weekday_rate is not None and weekday_rate < 90:
            session_review_flags.append(
                f"Weekday cohort is below the 90% target at {weekday_rate:.2f}% across {weekday_n} settled trades; "
                "do not attribute the overall shortfall to weekends alone."
            )
    if not session_review_flags:
        session_review_flags.append(
            "No calendar cohort currently meets the sample and loss criteria for a specific alert; continue tracking by UTC day and session."
        )

    return {
        "mode": "SIMULATION_ONLY",
        "system": "post_trade_feedback_v2",
        "calendar_context_coverage": {
            **calendar_context_sources,
            "total_journal_rows_reviewed": len(rows),
            "note": "Missing legacy weekday/weekend/time labels are derived from the saved trade-entry timestamp in UTC; original database rows are not mutated. Missing chart-regime and volatility features remain unknown.",
        },
        "session_context_diagnostics": {
            "target_positive_net_rate_percent": 90.0,
            "weekday": weekday_cohort,
            "weekend": weekend_cohort,
            "review_flags": session_review_flags,
            "automatic_strategy_changes_enabled": False,
            "note": "Calendar differences are diagnostic. They do not establish causation or authorize a blanket weekend ban; any strategy change must pass chronological out-of-sample testing and maintain positive net P&L.",
        },
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "automatic_strategy_changes_enabled": adaptive_enabled,
        "signal_weight_adjustment_enabled": False,
        "phase1_context_policy": (
            "Weekday/weekend, UTC time-of-day, trend/range regime, and existing volatility-warning cohorts are descriptive only. "
            "Signal weights are not automatically changed; any future change requires enough settled observations and out-of-sample validation."
        ),
        "walk_forward_validation": _walk_forward_context_validation(rows),
        "adaptive_policy": adaptive_policy,
        "policy_change_history": policy_change_history,
        "rejection_feedback": _build_rejection_feedback_report(conn),
        "policy": "Every settled paper trade is stored once with its entry-time feature snapshot and net outcome. The bounded paper-only adaptation can only tighten the preferred entry target after a minimum sample and repeated negative fee-inclusive outcomes in the high-price cohort. It never loosens the target or changes the hard entry cap, trade budget, daily loss limit, or live-trading permissions. Every target reduction is retained in an append-only history for review and manual reversal if needed.",
        "summary": summary,
        "cohorts": cohorts,
        "cohort_rows": flat_cohorts,
        "research_notes": _research_notes(cohorts),
        "latest_review": rows[0] if rows else None,
        "recent_reviews": rows[:10],
        "strategy_comparison": build_strategy_comparison_report(conn),
        "important": (
            "A result is classified by recorded net P&L; this is separate from the official market direction and from exit reason labels. "
            "Entry features are observations, not proven causes. Adaptation requires at least 20 settled hybrid trades, a sufficient high-price cohort, and negative net outcomes; reviews occur no more than once per 10 additional settled hybrid trades. Hypothetical rejected-signal and shadow-exit outcomes remain separate."
        ),
    }
