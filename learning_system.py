"""Transparent, conservative outcome learning for the BTC paper-trading bot.

Learns only from resolved paper trades. It never raises position size or loosens
hard risk limits. A historical-context veto is applied only when two independent
strategy-context dimensions each have enough completed examples and negative ROI.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

ADAPTIVE_DIMENSIONS = (
    "timeframe_alignment",
    "volume_confirmation",
    "box_context",
    "market_structure",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _float_env(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def ensure_learning_schema(conn: sqlite3.Connection) -> None:
    """Create learning tables and migrate columns safely for existing journals."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS trade_learning_profiles (
            feature_key TEXT PRIMARY KEY,
            sample_count INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            breakevens INTEGER NOT NULL DEFAULT 0,
            total_pnl_cents REAL NOT NULL DEFAULT 0,
            total_entry_cost_cents REAL NOT NULL DEFAULT 0,
            last_updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS trade_learning_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL UNIQUE,
            settled_at TEXT NOT NULL,
            direction TEXT NOT NULL,
            outcome TEXT,
            pnl_cents REAL NOT NULL,
            entry_cost_cents REAL NOT NULL,
            features_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )"""
    )
    trade_cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)")}
    for name, definition in (
        ("learning_features_json", "TEXT"),
        ("learning_recorded_at", "TEXT"),
    ):
        if name not in trade_cols:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {definition}")

    # Backfill old settled trades once. If their signal context was not saved at
    # entry, they still contribute to broad direction/source summaries, but not
    # to context-based vetoes. This avoids inventing historical evidence.
    rows = conn.execute(
        """SELECT id FROM paper_trades
           WHERE settled_at IS NOT NULL AND pnl_cents IS NOT NULL
             AND learning_recorded_at IS NULL ORDER BY id"""
    ).fetchall()
    for (trade_id,) in rows:
        record_settled_trade(conn, int(trade_id))


def _normalise_direction(direction: Any) -> str:
    value = str(direction or "").upper()
    if value in {"UP", "BUY_UP", "LONG", "BUY"}:
        return "UP"
    if value in {"DOWN", "BUY_DOWN", "SHORT", "SELL"}:
        return "DOWN"
    return "UNKNOWN"


def _as_direction(value: Any) -> int | None:
    if value in (1, -1):
        return int(value)
    text = str(value or "").lower()
    if text in {"bullish", "up", "upward", "buy", "1"}:
        return 1
    if text in {"bearish", "down", "downward", "sell", "-1"}:
        return -1
    return None


def build_learning_features(direction: Any, source: Any, payload: dict[str, Any] | None) -> dict[str, str]:
    """Extract fixed, human-readable context labels from a saved signal payload."""
    wanted = 1 if _normalise_direction(direction) == "UP" else -1 if _normalise_direction(direction) == "DOWN" else None
    body = payload if isinstance(payload, dict) else {}
    strategies = body.get("strategies") or body.get("strategy_diagnostics") or {}
    if not isinstance(strategies, dict):
        strategies = {}

    trends = (strategies.get("multi_timeframe_trend") or {}).get("timeframes") or {}
    t5 = _as_direction(trends.get("5m")) if isinstance(trends, dict) else None
    t15 = _as_direction(trends.get("15m")) if isinstance(trends, dict) else None
    if wanted is None or t5 is None or t15 is None:
        timeframe_alignment = "unknown"
    elif t5 == wanted and t15 == wanted:
        timeframe_alignment = "aligned"
    elif t5 == -wanted and t15 == -wanted:
        timeframe_alignment = "opposed"
    else:
        timeframe_alignment = "mixed"

    volume = strategies.get("volume_confirmation") or {}
    if not isinstance(volume, dict) or "confirmed" not in volume:
        volume_confirmation = "unknown"
    else:
        volume_confirmation = "confirmed" if bool(volume.get("confirmed")) else "not_confirmed"

    box = strategies.get("box_theory") or {}
    box_dir = _as_direction(box.get("direction")) if isinstance(box, dict) else None
    box_state = str(box.get("state", "unknown")).lower() if isinstance(box, dict) else "unknown"
    if wanted is not None and box_dir == wanted and "breakout" in box_state:
        box_context = "breakout_aligned"
    elif wanted is not None and box_dir == -wanted and "breakout" in box_state:
        box_context = "breakout_opposed"
    elif box_state == "inside_box":
        box_context = "inside_range"
    elif box_state in {"wick_break_or_rejection", "wick_break", "rejection"}:
        box_context = "wick_rejection"
    else:
        box_context = "unknown"

    structure = strategies.get("market_structure") or {}
    struct_dir = _as_direction(structure.get("direction")) if isinstance(structure, dict) else None
    if wanted is None or struct_dir is None or struct_dir == 0:
        market_structure = "mixed_or_unknown"
    elif struct_dir == wanted:
        market_structure = "aligned"
    else:
        market_structure = "opposed"

    source_value = str(source or body.get("source") or "unknown").strip().lower()
    if source_value not in {"hybrid", "internal", "tradingview", "gainzalgo", "manual", "legacy"}:
        source_value = "unknown"

    return {
        "direction": _normalise_direction(direction),
        "signal_source": source_value,
        "timeframe_alignment": timeframe_alignment,
        "volume_confirmation": volume_confirmation,
        "box_context": box_context,
        "market_structure": market_structure,
    }


def _clean_features(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    allowed = {"direction", "signal_source", *ADAPTIVE_DIMENSIONS}
    out: dict[str, str] = {}
    for key, item in value.items():
        key = str(key)
        if key not in allowed or item is None:
            continue
        text = str(item).strip().lower().replace("\n", " ")[:80]
        if text:
            out[key] = text
    return out


def record_settled_trade(conn: sqlite3.Connection, trade_id: int) -> bool:
    """Record one resolved trade into profiles exactly once. Caller commits."""
    row = conn.execute(
        """SELECT id, settled_at, direction, outcome, pnl_cents, contracts,
                  entry_cents, fee_cents_per_contract, learning_features_json,
                  learning_recorded_at
           FROM paper_trades WHERE id=?""",
        (int(trade_id),),
    ).fetchone()
    if not row or not row[1] or row[4] is None:
        return False
    (trade_id, settled_at, direction, outcome, pnl_cents, contracts,
     entry_cents, fee_cents, features_json, learning_recorded_at) = row
    if learning_recorded_at:
        return False
    existing = conn.execute(
        "SELECT 1 FROM trade_learning_events WHERE trade_id=?", (int(trade_id),)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE paper_trades SET learning_recorded_at=? WHERE id=?",
            (utc_now_iso(), int(trade_id)),
        )
        return False

    try:
        features = _clean_features(json.loads(features_json or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        features = {}
    if not features:
        # Legacy or manually entered trade: learn the overall result and side,
        # but do not pretend it has a known strategy context.
        features = {"direction": _normalise_direction(direction), "signal_source": "legacy"}
    else:
        features.setdefault("direction", _normalise_direction(direction))

    contracts_i = max(1, int(contracts or 1))
    entry_cost = max(0.0, (float(entry_cents or 0) + float(fee_cents or 0)) * contracts_i)
    pnl = float(pnl_cents)
    now = utc_now_iso()
    cursor = conn.execute(
        """INSERT OR IGNORE INTO trade_learning_events
           (trade_id, settled_at, direction, outcome, pnl_cents, entry_cost_cents, features_json, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (int(trade_id), str(settled_at), _normalise_direction(direction), str(outcome or ""),
         pnl, entry_cost, json.dumps(features, sort_keys=True), now),
    )
    if cursor.rowcount == 0:
        conn.execute("UPDATE paper_trades SET learning_recorded_at=? WHERE id=?", (now, int(trade_id)))
        return False

    for feature, value in features.items():
        key = f"{feature}={value}"
        win = int(pnl > 0)
        loss = int(pnl < 0)
        breakeven = int(pnl == 0)
        conn.execute(
            """INSERT INTO trade_learning_profiles
               (feature_key, sample_count, wins, losses, breakevens, total_pnl_cents,
                total_entry_cost_cents, last_updated_at)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(feature_key) DO UPDATE SET
                   sample_count=sample_count+1,
                   wins=wins+excluded.wins,
                   losses=losses+excluded.losses,
                   breakevens=breakevens+excluded.breakevens,
                   total_pnl_cents=total_pnl_cents+excluded.total_pnl_cents,
                   total_entry_cost_cents=total_entry_cost_cents+excluded.total_entry_cost_cents,
                   last_updated_at=excluded.last_updated_at""",
            (key, win, loss, breakeven, pnl, entry_cost, now),
        )
    conn.execute(
        "UPDATE paper_trades SET learning_recorded_at=? WHERE id=?",
        (now, int(trade_id)),
    )
    return True


def evaluate_learned_adjustment(
    conn: sqlite3.Connection,
    features: dict[str, str],
    *,
    min_samples: int | None = None,
    bad_roi_percent: float | None = None,
) -> dict[str, Any]:
    """Conservatively veto repeat contexts only after two independently poor factors.

    This is deliberately a bounded, explainable adjustment: it can block a
    historically poor context, but it never increases risk or loosens safeguards.
    """
    min_samples = min_samples or _int_env("PAPER_LEARNING_MIN_SAMPLES", 8, 5, 100)
    bad_roi_percent = bad_roi_percent if bad_roi_percent is not None else _float_env(
        "PAPER_LEARNING_BAD_ROI_PERCENT", -8.0, -50.0, -1.0
    )
    cleaned = _clean_features(features)
    observed: list[dict[str, Any]] = []
    underperforming: list[dict[str, Any]] = []
    positive: list[dict[str, Any]] = []

    for dimension in ADAPTIVE_DIMENSIONS:
        value = cleaned.get(dimension)
        if not value or value == "unknown" or value == "mixed_or_unknown":
            continue
        key = f"{dimension}={value}"
        row = conn.execute(
            """SELECT sample_count, wins, losses, total_pnl_cents, total_entry_cost_cents
               FROM trade_learning_profiles WHERE feature_key=?""",
            (key,),
        ).fetchone()
        count, wins, losses, total_pnl, total_cost = row if row else (0, 0, 0, 0.0, 0.0)
        roi = (100.0 * float(total_pnl) / float(total_cost)) if total_cost else None
        info = {
            "dimension": dimension,
            "value": value,
            "feature_key": key,
            "sample_count": int(count),
            "wins": int(wins),
            "losses": int(losses),
            "total_pnl_cents": round(float(total_pnl), 2),
            "roi_percent": round(float(roi), 2) if roi is not None else None,
            "status": "building_sample",
        }
        if int(count) >= min_samples and roi is not None:
            if roi <= bad_roi_percent and float(total_pnl) < 0:
                info["status"] = "historically_underperforming"
                underperforming.append(info)
            elif roi >= abs(bad_roi_percent) and float(total_pnl) > 0:
                info["status"] = "historically_positive"
                positive.append(info)
            else:
                info["status"] = "no_strong_adjustment"
        observed.append(info)

    # Learning updates are always recorded, but entry adjustments can be toggled.
    # Require at least two different strategy dimensions to avoid overreacting to
    # a single noisy feature. Direction/indicator source never count as veto factors.
    enabled = os.getenv("PAPER_LEARNING_ENABLED", "true").lower() == "true"
    vetoed = enabled and len({item["dimension"] for item in underperforming}) >= 2
    reasons: list[str] = []
    cautions: list[str] = []
    if vetoed:
        joined = "; ".join(
            f"{item['dimension'].replace('_', ' ')}={item['value']} has {item['sample_count']} resolved examples and {item['roi_percent']:.1f}% net ROI"
            for item in underperforming
        )
        reasons.append(
            f"Learning safeguard veto: two or more independent context factors have repeatedly underperformed "
            f"(minimum {min_samples} resolved trades per factor; cutoff {bad_roi_percent:.1f}% ROI). {joined}."
        )
    elif underperforming:
        item = underperforming[0]
        cautions.append(
            f"Historical caution only: {item['dimension'].replace('_', ' ')}={item['value']} has "
            f"{item['sample_count']} resolved examples and {item['roi_percent']:.1f}% net ROI; "
            "one weak factor alone does not reject the signal."
        )

    return {
        "enabled": enabled,
        "vetoed": vetoed,
        "minimum_samples_per_factor": int(min_samples),
        "underperforming_roi_cutoff_percent": float(bad_roi_percent),
        "observed_contexts": observed,
        "underperforming_factors": underperforming,
        "positive_factors": positive,
        "reasons": reasons,
        "cautions": cautions,
        "adjustment_applied": bool(vetoed),
        "policy": "Records every settled paper-trade outcome. When enabled, it only vetoes if two independent context factors each have enough negative-ROI history. It never increases position size or loosens risk limits.",
    }


def learning_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compact dashboard-ready learning status and recent outcome records."""
    min_samples = _int_env("PAPER_LEARNING_MIN_SAMPLES", 8, 5, 100)
    bad_roi = _float_env("PAPER_LEARNING_BAD_ROI_PERCENT", -8.0, -50.0, -1.0)
    events = int(conn.execute("SELECT COUNT(*) FROM trade_learning_events").fetchone()[0] or 0)
    profile_rows = conn.execute(
        """SELECT feature_key, sample_count, wins, losses, total_pnl_cents,
                  total_entry_cost_cents, last_updated_at
           FROM trade_learning_profiles ORDER BY sample_count DESC, feature_key"""
    ).fetchall()
    profiles = []
    for key, count, wins, losses, pnl, cost, updated in profile_rows:
        feature, _, value = str(key).partition("=")
        roi = 100.0 * float(pnl or 0) / float(cost) if cost else None
        roi_v = round(roi, 2) if roi is not None else None
        if feature in ADAPTIVE_DIMENSIONS and int(count) >= min_samples and roi is not None and roi <= bad_roi and float(pnl or 0) < 0:
            status = "historically_underperforming"
        elif feature in ADAPTIVE_DIMENSIONS and int(count) >= min_samples and roi is not None and roi >= abs(bad_roi) and float(pnl or 0) > 0:
            status = "historically_positive"
        else:
            status = "building_sample" if int(count) < min_samples else "no_strong_adjustment"
        profiles.append({
            "feature": feature,
            "value": value,
            "feature_key": key,
            "sample_count": int(count),
            "wins": int(wins),
            "losses": int(losses),
            "total_pnl_dollars": round(float(pnl or 0) / 100, 2),
            "roi_percent": roi_v,
            "last_updated_at": updated,
            "status": status,
        })
    context_profiles = [p for p in profiles if p["feature"] in ADAPTIVE_DIMENSIONS]
    mature = [p for p in context_profiles if p["sample_count"] >= min_samples]
    underperforming = [p for p in context_profiles if p["status"] == "historically_underperforming"]
    positive = [p for p in context_profiles if p["status"] == "historically_positive"]
    last_update_row = conn.execute("SELECT MAX(recorded_at) FROM trade_learning_events").fetchone()
    recent_rows = conn.execute(
        """SELECT trade_id, settled_at, direction, outcome, pnl_cents, features_json
           FROM trade_learning_events ORDER BY id DESC LIMIT 5"""
    ).fetchall()
    recent = []
    for trade_id, settled_at, direction, outcome, pnl, features_json in recent_rows:
        try:
            features = json.loads(features_json or "{}")
        except (TypeError, ValueError):
            features = {}
        recent.append({
            "trade_id": int(trade_id), "settled_at": settled_at,
            "direction": direction, "outcome": outcome,
            "pnl_dollars": round(float(pnl or 0) / 100, 2),
            "features": _clean_features(features),
        })
    return {
        "enabled": os.getenv("PAPER_LEARNING_ENABLED", "true").lower() == "true",
        "resolved_trades_learned": events,
        "context_patterns_tracked": len(context_profiles),
        "patterns_with_minimum_samples": len(mature),
        "historically_underperforming_patterns": len(underperforming),
        "historically_positive_patterns": len(positive),
        "minimum_samples_per_factor": min_samples,
        "underperforming_roi_cutoff_percent": bad_roi,
        "last_update_at": last_update_row[0] if last_update_row else None,
        "underperforming_patterns": underperforming[:8],
        "positive_patterns": positive[:8],
        "recent_trades": recent,
        "policy": "Each settled trade is recorded once. Context adjustments begin only after enough examples; two independent negative-ROI factors can veto a future setup. Learning never raises size or relaxes risk limits.",
    }
