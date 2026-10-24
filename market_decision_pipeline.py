"""Bounded market research pipeline that only creates segregated PAPER simulations.

The catalog scanner inventories open markets. This module evaluates a rotating,
liquidity-ranked subset using fresh order books and an optional validated model+news
provider. It can only create a separate research paper journal, never real orders.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from math import log1p
from typing import Any, Callable, Mapping

from db_store import connect_database, ensure_schema
from market_analysis_provider import analyze_market, provider_configured, MarketAnalysisProviderError
from market_decision_engine import assess_market_opportunity, _orderbook_summary, _REQUIRED_EVIDENCE
from market_category_registry import market_display_group

log = logging.getLogger("market_decision_pipeline")

# Research candidates are ranked across markets, but only one paper position may be open at a time.
# Environment overrides may reduce other risk budgets, but cannot raise this single-position cap.
MAX_OPEN_RESEARCH_POSITIONS = 1
MAX_RESEARCH_ENTRIES_PER_CYCLE = 1


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(low, min(high, value))


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        value = float(value)
        return value if value == value and abs(value) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default


def _paper_candidate_rank(result: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    """Score only already-eligible opportunities; this is a ranking score, not a win probability."""
    side = str(result.get("selected_side") or "").upper()
    yes_probability = _num(result.get("model_probability_yes_pct"), -1)
    selected_probability = (
        yes_probability if side == "YES" else
        100.0 - yes_probability if side == "NO" and yes_probability >= 0 else 0.0
    )
    edge = max(0.0, _num(result.get("expected_net_edge_cents"), 0.0))
    depth_raw = result.get("yes_ask_depth_contracts") if side == "YES" else result.get("no_ask_depth_contracts")
    spread_raw = result.get("yes_spread_cents") if side == "YES" else result.get("no_spread_cents")
    depth = max(0.0, _num(depth_raw, 0.0))
    volume = max(0.0, _num(result.get("volume_24h"), 0.0))
    spread = max(0.0, _num(spread_raw, 5.0))
    entry = max(0.0, _num(result.get("entry_ask_cents"), 100.0))

    # Fixed scales keep ranking comparable from batch to batch.
    edge_score = min(edge / 50.0, 1.0)
    depth_score = log1p(min(depth, 1000.0)) / log1p(1000.0)
    volume_score = log1p(min(volume, 1_000_000.0)) / log1p(1_000_000.0)
    odds_score = max(0.0, min(selected_probability / 100.0, 1.0))
    spread_score = max(0.0, 1.0 - min(spread, 5.0) / 5.0)
    components = {
        "net_edge": round(edge_score * 60.0, 3),
        "selected_side_ask_depth": round(depth_score * 18.0, 3),
        "volume_24h": round(volume_score * 12.0, 3),
        "model_estimated_selected_side_probability": round(odds_score * 7.0, 3),
        "spread_efficiency": round(spread_score * 3.0, 3),
    }
    score = round(sum(components.values()), 3)
    return score, {
        "rank_score": score,
        "rank_score_is_probability": False,
        "score_components": components,
        "expected_net_edge_cents": round(edge, 3),
        "selected_side_probability_percent": round(selected_probability, 3),
        "selected_side_ask_depth_contracts": round(depth, 3),
        "volume_24h": round(volume, 3),
        "selected_side_spread_cents": round(spread, 3),
        "entry_ask_cents": round(entry, 3),
        "method": "60% net edge, 18% selected-side depth, 12% 24h volume, 7% model-estimated selected-side probability, 3% spread efficiency",
    }


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_market(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    market = payload.get("market")
    return dict(market) if isinstance(market, dict) else dict(payload)


def _exploratory_paper_candidate(result: dict[str, Any], market: Mapping[str, Any],
                                  model: Any, *, enabled: bool) -> dict[str, Any]:
    """Create a separately labelled paper candidate when only calibration/evidence is missing."""
    if not enabled or result.get("eligible") or not isinstance(model, Mapping):
        return result
    reasons = [str(x) for x in result.get("reasons", [])]
    allowed = {"model_not_validated_out_of_sample", "insufficient_out_of_sample_validation",
               "brier_score_unavailable_or_too_high", "net_edge_not_calculable"}
    if not reasons or any(x not in allowed and not x.startswith("required_evidence_missing:") for x in reasons):
        return result
    ticker = str(market.get("ticker") or market.get("market_ticker") or "")
    p = _num(model.get("model_probability_yes"), -1)
    if not ticker or model.get("market_ticker") != ticker or not 0 < p < 1:
        return result
    model_id, version = model.get("model_id"), model.get("model_version")
    if not isinstance(model_id, str) or not model_id.strip() or not isinstance(version, str) or not version.strip():
        return result
    try:
        as_of = datetime.fromisoformat(str(model.get("as_of_utc")).replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            return result
        age = (datetime.now(timezone.utc) - as_of.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return result
    if age < -120 or age > 1800 or str(market.get("status") or "").lower() not in {"open", "active"}:
        return result
    yes_ask, no_ask = _num(result.get("yes_ask_cents"), -1), _num(result.get("no_ask_cents"), -1)
    if not (0 < yes_ask <= 100 and 0 < no_ask <= 100):
        return result
    fee = max(2.0, _num(result.get("estimated_fee_cents"), 2))
    slip = max(1.0, _num(result.get("estimated_slippage_cents"), 1))
    yes_edge, no_edge = p * 100 - yes_ask - fee - slip, (1 - p) * 100 - no_ask - fee - slip
    side, entry, edge = max((("YES", yes_ask, yes_edge), ("NO", no_ask, no_edge)), key=lambda x: x[2])
    min_edge = max(5.0, _num(os.getenv("MARKET_MIN_NET_EDGE_CENTS", "5"), 5))
    max_entry = min(90.0, _num(os.getenv("MARKET_MAX_ENTRY_CENTS", "90"), 90))
    if edge < min_edge or entry > max_entry:
        return result
    result.update({
        "status": "exploratory_paper_candidate", "eligible": True, "selected_side": side,
        "model_probability_yes_pct": round(p * 100, 3), "yes_net_edge_cents": round(yes_edge, 3),
        "no_net_edge_cents": round(no_edge, 3), "expected_net_edge_cents": round(edge, 3),
        "entry_ask_cents": round(entry, 3), "experimental_paper_mode": True,
        "model_validation_status": "UNVALIDATED_EXPLORATORY",
        "exploratory_validation_warnings": reasons, "warnings": [
            "Unvalidated probability: paper accuracy measurement only.",
            "Do not treat exploratory results as validated predictive performance.",
        ], "reasons": [],
    })
    if isinstance(result.get("model"), dict):
        result["model"]["validation_status"] = "UNVALIDATED_EXPLORATORY"
    return result


class MarketDecisionPipeline:
    def __init__(
        self,
        catalog_snapshot: Callable[[], dict[str, Any]],
        market_fetcher: Callable[[str], dict[str, Any]],
        orderbook_fetcher: Callable[[str], dict[str, Any]],
        model_provider: Callable[[Mapping[str, Any], Mapping[str, Any], list[str]], dict[str, Any]] = analyze_market,
    ) -> None:
        self._catalog_snapshot = catalog_snapshot
        self._market_fetcher = market_fetcher
        self._orderbook_fetcher = orderbook_fetcher
        self._model_provider = model_provider
        self._lock = threading.RLock()
        self._job_lock = threading.Lock()
        self._settlement_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._scheduler_started = False
        self._manual_entry_once_pending = False
        self._cursor = 0
        self._state: dict[str, Any] = self._empty()

    @staticmethod
    def _empty() -> dict[str, Any]:
        from runtime_controls import market_autopilot_enabled
        return {
            "status": "idle", "updated_at": _stamp(), "last_run_at": None,
            "provider_configured": provider_configured(), "autopaper_enabled": _bool_env("MARKET_AUTOPAPER_ENABLED", False) and market_autopilot_enabled(),
            "candidate_pool_size": 0, "analyzed_count": 0, "paper_entries_created": 0,
            "blocked_count": 0, "decisions": [], "last_error": None,
            "mode": "PAPER_RESEARCH_ONLY", "live_orders_submitted": 0,
            "exploratory_paper_enabled": _bool_env("MARKET_EXPLORATORY_PAPER_ENABLED", False),
            "note": "Validated entries require calibrated evidence; explicitly enabled exploratory entries are labeled unvalidated and used only to measure paper accuracy. No live order API exists.",
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._state)
            result["decisions"] = [dict(row) for row in self._state.get("decisions", [])]
            result["provider_configured"] = provider_configured()
            from runtime_controls import market_autopilot_enabled
            result["autopaper_enabled"] = _bool_env("MARKET_AUTOPAPER_ENABLED", False) and market_autopilot_enabled()
            result["exploratory_paper_enabled"] = _bool_env("MARKET_EXPLORATORY_PAPER_ENABLED", False)
            return result

    def start(self, force: bool = True, *, paper_entry_once: bool = False) -> dict[str, Any]:
        with self._job_lock:
            with self._lock:
                if self._state.get("status") == "running":
                    if paper_entry_once:
                        self._manual_entry_once_pending = True
                    return self.snapshot()
                if not force and not provider_configured():
                    self._state.update(status="provider_not_configured", updated_at=_stamp(), provider_configured=False,
                                       last_error="Configure MARKET_ANALYSIS_PROVIDER_URL and MARKET_ANALYSIS_PROVIDER_API_KEY to provide calibrated probabilities and source-attributed current evidence.")
                    return self.snapshot()
                self._state.update(status="running", updated_at=_stamp(), last_error=None)
            self._thread = threading.Thread(
                target=self._run, args=(paper_entry_once,),
                name="market-research-decision-pipeline", daemon=True,
            )
            self._thread.start()
            return self.snapshot()

    def _run(self, paper_entry_once: bool = False) -> None:
        with self._lock:
            self._state.update(status="running", updated_at=_stamp(), last_run_at=_stamp(), last_error=None,
                               provider_configured=provider_configured(), paper_entries_created=0,
                               analyzed_count=0, blocked_count=0, decisions=[])
        try:
            catalog = self._catalog_snapshot()
            catalog_status = catalog.get("status") if isinstance(catalog, dict) else "unavailable"
            if catalog_status not in {"complete", "partial"}:
                raise RuntimeError("market_catalog_not_ready")
            raw_candidates = catalog.get("analysis_candidates") if isinstance(catalog.get("analysis_candidates"), list) else catalog.get("markets", [])
            raw_candidates = [row for row in raw_candidates if isinstance(row, dict) and isinstance(row.get("ticker"), str) and row.get("ticker")]
            raw_pool_size = len(raw_candidates)
            enabled_categories = {x.strip().lower() for x in os.getenv("MARKET_PAPER_ENABLED_CATEGORIES", "sports,politics,economics,weather,crypto_event,financial_markets,entertainment,companies,world,science_technology").split(",") if x.strip()}
            # Don't spend model tokens on unknown categories, BTC markets that belong
            # to the separate BTC engine, or research categories not explicitly allowed.
            candidates = [row for row in raw_candidates
                          if str(row.get("category") or "unclassified").lower() in _REQUIRED_EVIDENCE
                          and row.get("route_status") in (None, "research_only")]
            pool_size = len(candidates)
            max_per_cycle = _int_env("MARKET_ANALYSIS_MAX_PER_CYCLE", 10, 1, 50)
            if not candidates:
                with self._lock:
                    self._state.update(status="complete", candidate_pool_size=raw_pool_size, updated_at=_stamp(),
                                       analyzed_count=0, blocked_count=raw_pool_size, decisions=[],
                                       last_error="No recognized, allowlisted research-category markets are available in the bounded candidate pool.")
                return
            start = self._cursor % len(candidates)
            selected = [candidates[(start + i) % len(candidates)] for i in range(min(max_per_cycle, len(candidates)))]
            self._cursor = (start + len(selected)) % max(1, len(candidates))
            decisions: list[dict[str, Any]] = []
            eligible_for_entry: list[dict[str, Any]] = []
            entered = 0
            blocked = 0
            # If there is no current calibrated probability/news provider, do not
            # fetch order books or fabricate an edge. Surface the blocker visibly.
            if not provider_configured():
                # Keep already-open research paper simulations settling even if the
                # optional model/news provider is temporarily unconfigured.
                try:
                    conn = connect_database()
                    try:
                        ensure_schema(conn)
                        self._settle_open_trades(conn)
                        self._collect_resolved_predictions(conn)
                        conn.commit()
                    finally:
                        conn.close()
                except Exception as exc:
                    log.debug("Settlement-only pass skipped (%s).", type(exc).__name__)
                for row in selected:
                    decisions.append({"ticker": row.get("ticker"), "title": row.get("title"),
                                      "category": row.get("category"), "status": "research_only",
                                      "eligible": False, "trade_submitted": False,
                                      "reasons": ["probability_provider_not_configured"],
                                      "note": "No calibrated probability model and current evidence provider is configured."})
                    blocked += 1
                with self._lock:
                    self._state.update(status="provider_not_configured", updated_at=_stamp(),
                                       candidate_pool_size=pool_size, analyzed_count=0, blocked_count=blocked,
                                       decisions=decisions, last_error="Configure MARKET_ANALYSIS_PROVIDER_URL plus MARKET_ANALYSIS_PROVIDER_API_KEY, then validate the model/evidence response before opportunities can be considered.")
                return

            conn = connect_database()
            try:
                ensure_schema(conn)
                with self._settlement_lock:
                    self._settle_open_trades(conn)
                self._collect_resolved_predictions(conn)
                for row in selected:
                    ticker = row["ticker"]
                    try:
                        market = _normalize_market(self._market_fetcher(ticker))
                        if not market:
                            raise ValueError("market_detail_unavailable")
                        # The scanner's parent-event category is the source of truth.
                        # Detail-market endpoints may omit category or return a stale field.
                        market["ticker"] = ticker
                        category = str(row.get("category") or "unclassified").strip().lower()
                        market["category"] = category
                        if not (isinstance(market.get("title"), str) and market.get("title").strip()):
                            market["title"] = str(row.get("title") or row.get("event_title") or "")[:800]
                        orderbook_payload = self._orderbook_fetcher(ticker)
                        orderbook_summary = _orderbook_summary(orderbook_payload, market)
                        required = sorted(_REQUIRED_EVIDENCE.get(category, set()))
                        model = self._model_provider(market, orderbook_summary, required)
                        result = assess_market_opportunity(
                            market, orderbook_payload, model,
                            min_volume_24h=_num(os.getenv("MARKET_MIN_VOLUME_24H", "50"), 50),
                            min_depth_contracts=_num(os.getenv("MARKET_MIN_DEPTH_CONTRACTS", "5"), 5),
                            max_spread_cents=_num(os.getenv("MARKET_MAX_SPREAD_CENTS", "5"), 5),
                            min_net_edge_cents=_num(os.getenv("MARKET_MIN_NET_EDGE_CENTS", "5"), 5),
                            estimated_fee_cents=_num(os.getenv("MARKET_ESTIMATED_FEE_CENTS", "2"), 2),
                            slippage_cents=_num(os.getenv("MARKET_ESTIMATED_SLIPPAGE_CENTS", "1"), 1),
                            max_entry_cents=_num(os.getenv("MARKET_MAX_ENTRY_CENTS", "90"), 90),
                            min_validation_samples=_int_env("MARKET_MODEL_MIN_VALIDATION_SAMPLES", 100, 30, 10000),
                            max_brier_score=_num(os.getenv("MARKET_MODEL_MAX_BRIER_SCORE", "0.22"), 0.22),
                        )
                        result = _exploratory_paper_candidate(
                            result, market, model,
                            enabled=_bool_env("MARKET_EXPLORATORY_PAPER_ENABLED", False),
                        )
                        result["display_group"] = market_display_group(
                            category, ticker, market.get("title"), market.get("event_title"),
                            market.get("event_ticker"), market.get("event_sub_title"),
                        )
                        result["provider_configured"] = True
                        result["trade_submitted"] = False
                        if category not in enabled_categories:
                            result["eligible"] = False
                            result["status"] = "category_paper_disabled"
                            result.setdefault("reasons", []).append("category_not_allowlisted_for_paper")
                        result["paper_trade_created"] = False
                        result["paper_selection_status"] = "not_eligible"
                        if result.get("eligible") and category in enabled_categories:
                            eligible_for_entry.append(result)
                        decisions.append(result)
                        blocked += int(not result.get("eligible"))
                    except Exception as exc:
                        # Continue to the next market; don't expose provider text/tokens.
                        log.warning("Market analysis skipped ticker=%s err=%s", ticker[:120], type(exc).__name__)
                        blocked += 1
                        decisions.append({"ticker": ticker, "title": str(row.get("title") or "")[:300],
                                          "category": row.get("category"), "status": "analysis_error",
                                          "eligible": False, "trade_submitted": False,
                                          "reasons": ["market_analysis_unavailable", type(exc).__name__]})
                # Rank all fully eligible results from this analyzed batch before opening anything.
                # This prevents the first market returned by the scanner from winning merely by order.
                eligible_for_entry = [
                    item for item in eligible_for_entry
                    if isinstance(item.get("ticker"), str)
                    and isinstance(item.get("model"), Mapping)
                    and item.get("selected_side") in {"YES", "NO"}
                ]
                already_traded: list[dict[str, Any]] = []
                ranked_candidates: list[tuple[dict[str, Any], float, dict[str, Any]]] = []
                for item in eligible_for_entry:
                    model = item.get("model") if isinstance(item.get("model"), Mapping) else {}
                    model_id = str(model.get("model_id") or "")[:100]
                    model_version = str(model.get("model_version") or "")[:100]
                    prior = conn.execute(
                        """SELECT id FROM market_research_paper_trades
                           WHERE market_ticker=? AND model_id=? AND model_version=? LIMIT 1""",
                        (item.get("ticker"), model_id, model_version),
                    ).fetchone() if model_id and model_version else None
                    if prior:
                        item["paper_selection_status"] = "already_traded_for_model_version"
                        item["paper_trade_block_reason"] = "duplicate_market_model_paper_trade"
                        item.setdefault("reasons", []).append("already_paper_traded_for_this_model_version")
                        already_traded.append(item)
                        continue
                    score, details = _paper_candidate_rank(item)
                    ranked_candidates.append((item, score, details))
                ranked_candidates.sort(
                    key=lambda packed: (
                        packed[1],
                        _num(packed[0].get("expected_net_edge_cents"), 0.0),
                        _num(packed[2].get("selected_side_ask_depth_contracts"), 0.0),
                        _num(packed[2].get("selected_side_probability_percent"), 0.0),
                        _num(packed[2].get("volume_24h"), 0.0),
                        -_num(packed[2].get("selected_side_spread_cents"), 5.0),
                        -_num(packed[2].get("entry_ask_cents"), 100.0),
                    ),
                    reverse=True,
                )
                for index, (item, score, details) in enumerate(ranked_candidates, start=1):
                    item["paper_selection"] = {
                        **details, "rank": index, "eligible_candidate_count": len(ranked_candidates),
                        "is_best_candidate": index == 1,
                    }
                    item["paper_selection_status"] = (
                        "best_eligible_candidate" if index == 1 else "eligible_not_selected_as_best"
                    )
                    if index > 1:
                        item.setdefault("reasons", []).append("another_eligible_candidate_ranked_higher_this_batch")
                for item in already_traded:
                    item["paper_selection"] = {
                        "rank": None, "rank_score": None, "rank_score_is_probability": False,
                        "eligible_candidate_count": len(ranked_candidates), "is_best_candidate": False,
                    }

                from runtime_controls import market_autopilot_enabled
                if self._manual_entry_once_pending:
                    # If the user clicks "Run research batch" while an automatic
                    # batch is already in progress, let that in-flight batch honour
                    # the single manual paper-entry request once it finishes ranking.
                    paper_entry_once = True
                    self._manual_entry_once_pending = False
                autopilot_on = bool(market_autopilot_enabled(conn))
                paper_environment_safe = (
                    _bool_env("MARKET_AUTOPAPER_ENABLED", False)
                    and os.getenv("TRADING_MODE", "paper").strip().lower() == "paper"
                    and not _bool_env("LIVE_TRADING_ENABLED", False)
                )
                entry_authorized = paper_environment_safe and (autopilot_on or paper_entry_once)
                open_count = int(conn.execute(
                    "SELECT COUNT(*) FROM market_research_paper_trades WHERE status='open'"
                ).fetchone()[0] or 0)
                max_open_positions = min(
                    MAX_OPEN_RESEARCH_POSITIONS,
                    _int_env("MARKET_RESEARCH_MAX_OPEN_POSITIONS", 1, 1, 20),
                )
                max_entries_per_cycle = min(
                    MAX_RESEARCH_ENTRIES_PER_CYCLE,
                    _int_env("MARKET_RESEARCH_MAX_ENTRIES_PER_CYCLE", 1, 1, 10),
                )
                if paper_entry_once and not autopilot_on:
                    max_entries_per_cycle = 1

                if ranked_candidates:
                    for candidate, _score, _details in ranked_candidates:
                        candidate["paper_trade_created"] = False
                        candidate["paper_entry_gate"] = {
                            "authorized_for_this_batch": entry_authorized,
                            "autopilot_enabled": autopilot_on,
                            "manual_one_shot_requested": bool(paper_entry_once),
                            "environment_safe": paper_environment_safe,
                            "open_position_count_before_entry": open_count,
                            "max_open_positions": max_open_positions,
                            "max_entries_per_cycle": max_entries_per_cycle,
                        }
                        if not paper_environment_safe:
                            reason = "paper_entry_environment_gate_off_or_unsafe_mode"
                            candidate["paper_selection_status"] = "best_eligible_candidate_not_opened"
                        elif not (autopilot_on or paper_entry_once):
                            reason = "master_autopilot_off; candidate ranked for research only"
                            candidate["paper_selection_status"] = "best_candidate_ranked_autopilot_off"
                        elif open_count >= max_open_positions:
                            reason = "max_open_research_paper_trades_reached"
                            candidate["paper_selection_status"] = "eligible_candidate_waiting_for_open_position"
                        elif entered >= max_entries_per_cycle:
                            reason = "max_entries_per_research_cycle_reached"
                            candidate["paper_selection_status"] = "eligible_candidate_not_selected_this_cycle"
                        else:
                            candidate.setdefault("paper_selection", {})["selected_for_attempt"] = True
                            created, risk_reason = self._create_paper_trade(
                                conn, candidate,
                                allow_manual_entry_once=bool(paper_entry_once and not autopilot_on),
                            )
                            if created:
                                entered += 1
                                open_count += 1
                                candidate["paper_trade_created"] = True
                                candidate["paper_selection_status"] = "paper_position_opened"
                                candidate["paper_trade_block_reason"] = None
                                reason = None
                            else:
                                reason = risk_reason or "paper_position_not_created"
                                candidate["paper_selection_status"] = "best_candidate_blocked_by_risk_or_duplicate"
                        if reason:
                            candidate["paper_trade_block_reason"] = reason
                            if reason not in candidate.setdefault("reasons", []):
                                candidate["reasons"].append(reason)
                    for index, (candidate, _score, _details) in enumerate(ranked_candidates, start=1):
                        candidate.setdefault("paper_selection", {}).update({
                            "rank": index,
                            "eligible_candidate_count": len(ranked_candidates),
                            "is_best_candidate": index == 1,
                            "selected_for_attempt": bool(candidate.get("paper_selection", {}).get("selected_for_attempt", False)),
                        })
                for item in decisions:
                    self._record_decision(conn, item)
                conn.commit()
            finally:
                conn.close()
            with self._lock:
                self._state.update(status="complete", updated_at=_stamp(), candidate_pool_size=pool_size,
                                   analyzed_count=len(decisions), blocked_count=blocked,
                                   paper_entries_created=entered, decisions=decisions, last_error=None,
                                   live_orders_submitted=0,
                                   note="Only isolated research paper trades may be created. Live orders are not supported by this pipeline.")
        except Exception as exc:
            log.warning("Market decision pipeline failed (%s).", type(exc).__name__)
            with self._lock:
                self._state.update(status="failed", updated_at=_stamp(), last_error="Market research pipeline could not complete; no live order was attempted.", live_orders_submitted=0)

    def _record_decision(self, conn: Any, result: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(result), ensure_ascii=False, allow_nan=False, default=str)[:100_000]
        model = result.get("model") if isinstance(result.get("model"), Mapping) else {}
        raw_probability = _num(model.get("model_probability_yes"), float("nan"))
        probability = raw_probability if 0.0 < raw_probability < 1.0 else None
        model_id = str(model.get("model_id") or "")[:100] or None
        model_version = str(model.get("model_version") or "")[:100] or None
        conn.execute(
            """INSERT INTO market_research_decisions
               (evaluated_at,market_ticker,category,decision,selected_side,model_probability_yes,expected_net_edge_cents,model_id,model_version,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (_stamp(), result.get("ticker"), result.get("category"), result.get("status"), result.get("selected_side"),
             probability, result.get("expected_net_edge_cents"), model_id, model_version, payload),
        )

    def _collect_resolved_predictions(self, conn: Any, *, max_markets: int | None = None) -> dict[str, int]:
        """Label earlier predictions once a market resolves, for real calibration.

        Only predictions at least MARKET_OUTCOME_MIN_AGE_MINUTES old are eligible.
        Market-detail checks are rate-limited in a separate table. No label is
        inferred from a simulated trade's P&L or from current prices.
        """
        max_markets = max_markets or _int_env("MARKET_OUTCOME_COLLECT_MAX_PER_RUN", 20, 1, 100)
        min_age = _int_env("MARKET_OUTCOME_MIN_AGE_MINUTES", 60, 10, 10080)
        retry_after = _int_env("MARKET_OUTCOME_RETRY_MINUTES", 60, 10, 1440)
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=min_age)).isoformat(timespec="seconds")
        retry_cutoff = (now - timedelta(minutes=retry_after)).isoformat(timespec="seconds")
        rows = conn.execute(
            """SELECT d.market_ticker,d.category,d.model_id,d.model_version,
                      d.model_probability_yes,d.evaluated_at
               FROM market_research_decisions d
               LEFT JOIN market_research_prediction_outcomes o
                 ON o.market_ticker=d.market_ticker AND o.model_id=d.model_id AND o.model_version=d.model_version
               LEFT JOIN market_research_prediction_checks c
                 ON c.market_ticker=d.market_ticker AND c.model_id=d.model_id AND c.model_version=d.model_version
               WHERE d.model_probability_yes IS NOT NULL AND d.model_id IS NOT NULL AND d.model_version IS NOT NULL
                 AND d.evaluated_at < ? AND o.market_ticker IS NULL
                 AND (c.last_checked_at IS NULL OR
                      (c.last_checked_at < ? AND c.check_status NOT IN ('resolved_before_prediction','invalid_probability')))
                 AND d.id=(SELECT MAX(d2.id) FROM market_research_decisions d2
                           WHERE d2.market_ticker=d.market_ticker AND d2.model_id=d.model_id
                             AND d2.model_version=d.model_version AND d2.model_probability_yes IS NOT NULL)
               ORDER BY d.id ASC LIMIT ?""",
            (cutoff, retry_cutoff, max_markets),
        ).fetchall()
        stats = {"checked": 0, "resolved": 0, "pending": 0, "failed": 0, "rejected_temporal": 0}
        for row in rows:
            ticker, category, model_id, model_version = str(row[0]), str(row[1] or "unclassified"), str(row[2]), str(row[3])
            probability = _num(row[4], float("nan"))
            evaluated_at = str(row[5])
            status_out = "error"
            try:
                market = _normalize_market(self._market_fetcher(ticker))
                status = str(market.get("status") or "").strip().lower()
                outcome = str(market.get("result") or market.get("settlement_result") or "").strip().upper()
                if status not in {"settled", "finalized", "resolved", "closed"} or outcome not in {"YES", "NO"}:
                    status_out = "pending"
                    stats["pending"] += 1
                else:
                    settled_stamp = None
                    for name in ("settlement_ts", "settled_at", "settled_time", "settlement_time"):
                        raw = market.get(name)
                        try:
                            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                                # Some exchange wrappers expose epoch milliseconds;
                                # normalize seconds/milliseconds to UTC without guessing
                                # from arbitrary short integers.
                                epoch = float(raw)
                                if epoch > 100_000_000_000:
                                    epoch /= 1000.0
                                if epoch > 1_000_000_000:
                                    settled_stamp = datetime.fromtimestamp(epoch, tz=timezone.utc)
                                    break
                            elif isinstance(raw, str) and raw.strip():
                                parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
                                if parsed.tzinfo is not None:
                                    settled_stamp = parsed.astimezone(timezone.utc)
                                    break
                        except (ValueError, OverflowError, OSError, TypeError):
                            continue
                    eval_dt = datetime.fromisoformat(evaluated_at.replace("Z", "+00:00"))
                    # If the exchange gives a real resolution timestamp, reject a
                    # forecast that was recorded after resolution (look-ahead).
                    if settled_stamp is not None and settled_stamp <= eval_dt:
                        status_out = "resolved_before_prediction"
                        stats["rejected_temporal"] += 1
                    else:
                        settled_at = settled_stamp.isoformat(timespec="seconds") if settled_stamp else _stamp()
                        if not (0.0 < probability < 1.0):
                            status_out = "invalid_probability"
                            stats["failed"] += 1
                        else:
                            cur = conn.execute(
                                """INSERT INTO market_research_prediction_outcomes
                                   (market_ticker,category,model_id,model_version,evaluated_at,settled_at,
                                    model_probability_yes,actual_outcome_yes,outcome_source,recorded_at)
                                   VALUES (?,?,?,?,?,?,?,?,?,?)
                                   ON CONFLICT(market_ticker,model_id,model_version) DO NOTHING""",
                                (ticker, category, model_id, model_version, evaluated_at, settled_at,
                                 probability, 1 if outcome == "YES" else 0,
                                 "kalshi_api:market_resolution_timestamp" if settled_stamp else "kalshi_api:resolution_observed",
                                 _stamp()),
                            )
                            if cur.rowcount > 0:
                                stats["resolved"] += 1
                            status_out = "resolved"
            except Exception as exc:
                log.debug("Prediction outcome collection failed (%s).", type(exc).__name__)
                status_out = "error"
                stats["failed"] += 1
            conn.execute(
                """INSERT INTO market_research_prediction_checks
                   (market_ticker,model_id,model_version,last_checked_at,check_status)
                   VALUES (?,?,?,?,?) ON CONFLICT(market_ticker,model_id,model_version)
                   DO UPDATE SET last_checked_at=excluded.last_checked_at,check_status=excluded.check_status""",
                (ticker, model_id, model_version, _stamp(), status_out),
            )
            stats["checked"] += 1
        return stats

    def _create_paper_trade(
        self, conn: Any, result: Mapping[str, Any], *, allow_manual_entry_once: bool = False,
    ) -> tuple[bool, str | None]:
        side = result.get("selected_side")
        ticker = result.get("ticker")
        model = result.get("model") if isinstance(result.get("model"), Mapping) else {}
        entry = result.get("entry_ask_cents")
        if side not in {"YES", "NO"} or not isinstance(ticker, str) or not ticker or not isinstance(entry, (int, float)):
            return False, "invalid_paper_entry_payload"
        from runtime_controls import market_autopilot_enabled
        if (
            not _bool_env("MARKET_AUTOPAPER_ENABLED", False)
            or (not market_autopilot_enabled(conn) and not allow_manual_entry_once)
            or os.getenv("TRADING_MODE", "paper").strip().lower() != "paper"
            or _bool_env("LIVE_TRADING_ENABLED", False)
        ):
            return False, "runtime_is_not_safe_paper_mode"
        # Match the minimum costs used by assess_market_opportunity so the saved
        # simulated entry/P&L cannot understate fees or slippage versus its edge gate.
        fee = max(2.0, _num(os.getenv("MARKET_ESTIMATED_FEE_CENTS", "2"), 2))
        slippage = max(1.0, _num(os.getenv("MARKET_ESTIMATED_SLIPPAGE_CENTS", "1"), 1))
        model_id = str(model.get("model_id") or "")[:100]
        model_version = str(model.get("model_version") or "")[:100]
        if not model_id or not model_version:
            return False, "model_identity_missing"

        # Hard single-position cap: environment overrides cannot create overlapping research exposure.
        max_open = min(
            MAX_OPEN_RESEARCH_POSITIONS,
            _int_env("MARKET_RESEARCH_MAX_OPEN_POSITIONS", 1, 1, 20),
        )
        max_open_cost = max(1.0, _num(os.getenv("MARKET_RESEARCH_MAX_OPEN_COST_CENTS", "2000"), 2000))
        daily_loss_limit = max(1.0, _num(os.getenv("MARKET_RESEARCH_MAX_DAILY_LOSS_CENTS", "500"), 500))
        open_row = conn.execute("SELECT COUNT(*), COALESCE(SUM(contracts*(entry_cents+estimated_fee_cents+estimated_slippage_cents)),0) FROM market_research_paper_trades WHERE status='open'").fetchone()
        if int(open_row[0] or 0) >= max_open:
            return False, "max_open_research_paper_trades_reached"
        projected_open_cost = float(open_row[1] or 0) + float(entry) + fee + slippage
        if projected_open_cost > max_open_cost:
            return False, "max_open_research_paper_exposure_reached"
        today = datetime.now(timezone.utc).date().isoformat()
        daily_row = conn.execute(
            "SELECT COALESCE(SUM(pnl_cents),0) FROM market_research_paper_trades WHERE status='settled' AND settled_at >= ?",
            (today + "T00:00:00+00:00",),
        ).fetchone()
        if float(daily_row[0] or 0) <= -daily_loss_limit:
            return False, "research_paper_daily_loss_limit_reached"

        cursor = conn.execute(
            """INSERT INTO market_research_paper_trades
               (created_at,market_ticker,category,side,contracts,entry_cents,estimated_fee_cents,estimated_slippage_cents,
                model_probability_yes,model_id,model_version,expected_net_edge_cents,status,source_snapshot_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(market_ticker,model_id,model_version) DO NOTHING""",
            (_stamp(), ticker, result.get("category"), side, 1, float(entry), fee, slippage,
             _num(result.get("model_probability_yes_pct"), 0) / 100, model_id, model_version,
             _num(result.get("expected_net_edge_cents"), 0), "open",
             json.dumps({"decision": dict(result)}, ensure_ascii=False, default=str, allow_nan=False)[:100_000]),
        )
        if cursor.rowcount <= 0:
            return False, "duplicate_market_model_paper_trade"
        return True, None

    def _settle_open_trades(self, conn: Any) -> None:
        rows = conn.execute("SELECT id,market_ticker,side,entry_cents,estimated_fee_cents,estimated_slippage_cents FROM market_research_paper_trades WHERE status='open' ORDER BY id LIMIT 20").fetchall()
        for row in rows:
            try:
                market = _normalize_market(self._market_fetcher(str(row[1])))
                status = str(market.get("status") or "").lower()
                resolution = str(market.get("result") or "").upper()
                if status not in {"settled", "finalized", "resolved"} or resolution not in {"YES", "NO"}:
                    continue
                payout = 100.0 if str(row[2]).upper() == resolution else 0.0
                pnl = payout - _num(row[3]) - _num(row[4]) - _num(row[5])
                conn.execute("UPDATE market_research_paper_trades SET settled_at=?,result=?,pnl_cents=?,status='settled' WHERE id=? AND status='open'",
                             (_stamp(), resolution, pnl, row[0]))
            except Exception as exc:
                log.debug("Paper-market settlement check failed (%s).", type(exc).__name__)

    def settle_open_positions_only(self) -> dict[str, Any]:
        """Check open research paper positions for official settlement without running any model analysis."""
        conn = None
        try:
            conn = connect_database()
            ensure_schema(conn)
            open_before = int(conn.execute(
                "SELECT COUNT(*) FROM market_research_paper_trades WHERE status='open'"
            ).fetchone()[0] or 0)
            if open_before <= 0:
                return {"checked": 0, "settled": 0, "open_before": 0, "open_after": 0, "errors": 0}
            with self._settlement_lock:
                self._settle_open_trades(conn)
            conn.commit()
            open_after = int(conn.execute(
                "SELECT COUNT(*) FROM market_research_paper_trades WHERE status='open'"
            ).fetchone()[0] or 0)
            return {
                "checked": open_before,
                "settled": max(0, open_before - open_after),
                "open_before": open_before,
                "open_after": open_after,
                "errors": 0,
            }
        except Exception as exc:
            log.warning("Research paper settlement-only pass failed (%s).", type(exc).__name__)
            return {"checked": 0, "settled": 0, "open_before": None, "open_after": None,
                    "errors": 1, "error_type": type(exc).__name__}
        finally:
            if conn is not None:
                conn.close()

    def run_once_for_tests(self, *, paper_entry_once: bool = False) -> dict[str, Any]:
        """Synchronous entry point for deterministic tests; uses the same pipeline."""
        with self._lock:
            self._state.update(status="idle")
        self._run(paper_entry_once=paper_entry_once)
        return self.snapshot()

    def start_scheduler(self, enabled: bool = True) -> None:
        if not enabled:
            return
        with self._lock:
            if self._scheduler_started:
                return
            self._scheduler_started = True
        threading.Thread(target=self._scheduler_loop, name="market-decision-scheduler", daemon=True).start()

    def settlement_monitor_status(self) -> dict[str, Any]:
        with self._lock:
            started = bool(self._scheduler_started)
        return {
            "scheduler_started": started,
            "interval_seconds": _int_env("MARKET_RESEARCH_SETTLEMENT_INTERVAL_SECONDS", 60, 30, 3600),
            "runs_when_master_autopilot_off": True,
            "uses_model_provider": False,
            "requires_official_final_market_result": True,
        }

    def _scheduler_loop(self) -> None:
        interval = _int_env("MARKET_ANALYSIS_INTERVAL_SECONDS", 300, 60, 86400)
        settlement_interval = _int_env("MARKET_RESEARCH_SETTLEMENT_INTERVAL_SECONDS", 60, 30, 3600)
        last_scheduled_run = 0.0
        last_settlement_check = 0.0
        from runtime_controls import market_autopilot_enabled
        while True:
            try:
                now = time.monotonic()
                # Settlement tracking stays alive while autopilot is OFF. This pass
                # performs only official market-detail checks for existing positions;
                # it does not call the model/evidence provider or open new trades.
                if now - last_settlement_check >= settlement_interval:
                    settlement_result = self.settle_open_positions_only()
                    last_settlement_check = now
                    if settlement_result.get("settled") or settlement_result.get("errors"):
                        log.info("Research paper settlement monitor: %s", settlement_result)

                if not market_autopilot_enabled():
                    time.sleep(5)
                    continue
                catalog = self._catalog_snapshot()
                ready = isinstance(catalog, dict) and catalog.get("status") in {"complete", "partial"}
                if ready and provider_configured() and now - last_scheduled_run >= interval:
                    started = self.start(force=True)
                    if started.get("status") == "running":
                        last_scheduled_run = now
                elif ready and not provider_configured():
                    with self._lock:
                        if self._state.get("status") != "running":
                            self._state.update(status="provider_not_configured", updated_at=_stamp(), provider_configured=False,
                                               last_error="Configure MARKET_ANALYSIS_PROVIDER_URL and MARKET_ANALYSIS_PROVIDER_API_KEY to return calibrated probabilities and fresh, source-attributed evidence before research decisions can be made.")
            except Exception as exc:
                log.debug("Market analysis scheduler iteration failed (%s).", type(exc).__name__)
            time.sleep(5)

    def reset_for_tests(self) -> None:
        with self._job_lock, self._lock:
            self._cursor = 0
            self._manual_entry_once_pending = False
            self._state = self._empty()
