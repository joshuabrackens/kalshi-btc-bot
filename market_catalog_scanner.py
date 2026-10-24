"""Background, paginated Kalshi open-market inventory scanner.

This service inventories every open market returned by Kalshi pagination, within a
strict page cap. It does not submit orders and does not route non-BTC markets to the
BTC strategy. Cross-category news/injury/fundamental context is explicitly marked
unavailable until the corresponding licensed/current source adapters are configured.
"""
from __future__ import annotations

import logging
import heapq
import os
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

from kalshi_api import KalshiAPIError
from market_router import classify_and_route

log = logging.getLogger("market_catalog_scanner")


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(lo, min(hi, value))


def _float_env(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    if value != value or value in (float("inf"), float("-inf")):
        value = default
    return max(lo, min(hi, value))


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _price_cents(market: dict[str, Any], side: str, kind: str) -> float | None:
    raw = market.get(f"{side}_{kind}_dollars")
    dollars = True
    if raw is None:
        raw = market.get(f"{side}_{kind}")
        dollars = False
    try:
        value = float(raw)
        if dollars:
            value *= 100
        if not 0 <= value <= 100:
            return None
        return round(value, 2)
    except (TypeError, ValueError, OverflowError):
        return None


def _market_probability(market: dict[str, Any]) -> float | None:
    yes_ask = _price_cents(market, "yes", "ask")
    if yes_ask is not None:
        return yes_ask
    no_ask = _price_cents(market, "no", "ask")
    if no_ask is not None:
        return round(100 - no_ask, 2)
    yes_bid = _price_cents(market, "yes", "bid")
    no_bid = _price_cents(market, "no", "bid")
    if yes_bid is not None and no_bid is not None:
        return round((yes_bid + (100 - no_bid)) / 2, 2)
    return None


def _safe_market_row(market: dict[str, Any]) -> dict[str, Any]:
    routed = classify_and_route(market)
    category = routed.get("category")
    if category == "btc_15m":
        context_status = "BTC signal pipeline only; no general news model is connected"
    elif category == "sports":
        context_status = "blocked: no current injury/lineup/stat provider is configured"
    else:
        context_status = "blocked: no category-specific source and calibrated probability model is configured"
    return {
        **routed,
        "market_implied_yes_probability_pct": _market_probability(market),
        "yes_ask_cents": _price_cents(market, "yes", "ask"),
        "no_ask_cents": _price_cents(market, "no", "ask"),
        "volume_24h": market.get("volume_24h_fp", market.get("volume_24h")),
        "open_interest": market.get("open_interest_fp", market.get("open_interest")),
        "close_time": str(market.get("close_time") or "")[:64] or None,
        "context_status": context_status,
        "trade_submitted": False,
    }


class MarketCatalogScanner:
    """Thread-safe single-flight catalog scanner with bounded cursor pagination."""

    def __init__(self, provider: Callable[..., dict[str, Any]]):
        self._provider = provider
        self._state_lock = threading.RLock()
        self._job_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_start_monotonic = 0.0
        self._max_pages = _int_env("MARKET_SCAN_MAX_PAGES", 500, 1, 1000)
        self._max_markets = _int_env("MARKET_SCAN_MAX_MARKETS", 100_000, 200, 200_000)
        self._page_delay = _float_env("MARKET_SCAN_PAGE_DELAY_SECONDS", 0.05, 0.0, 2.0)
        self._refresh_interval = _int_env("MARKET_SCAN_REFRESH_SECONDS", 600, 60, 86400)
        self._cursor: str | None = None
        self._seen_cursors: set[str] = set()
        self._seen_tickers: set[str] = set()
        self._category_counts: Counter[str] = Counter()
        self._route_counts: Counter[str] = Counter()
        self._samples: list[dict[str, Any]] = []
        # Keep a bounded, activity-ranked pool for deeper research. The scanner
        # still inventories every market up to the catalog safety cap; it never
        # fetches a book or places a trade for each one indiscriminately.
        self._analysis_pool_size = _int_env("MARKET_RESEARCH_POOL_SIZE", 500, 50, 2000)
        self._analysis_candidates: list[dict[str, Any]] = []
        self._analysis_heap: list[tuple[float, float, str, dict[str, Any]]] = []
        self._analysis_seen = 0
        self._state: dict[str, Any] = self._empty_state()
        self._scheduler_started = False
        self._scheduler_lock = threading.Lock()

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "status": "idle", "started_at": None, "updated_at": _stamp(),
            "last_completed_at": None, "pages_scanned": 0, "candidate_count": 0,
            "returned_count": 0, "category_counts": {}, "route_counts": {},
            "markets": [], "analysis_candidates": [], "cursor_available": False, "pagination_warning": None,
            "error_code": None, "error_message": None, "cache_hit": False,
            "source": "Kalshi public open-market catalog", "trade_submitted": False,
            "active_strategy": "btc_15m", "context_enrichment": "not configured for all market categories",
            "max_pages": 500, "max_markets": 100_000,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            data = dict(self._state)
            data["category_counts"] = dict(data.get("category_counts") or {})
            data["route_counts"] = dict(data.get("route_counts") or {})
            data["markets"] = [dict(row) for row in data.get("markets") or []]
            # Keep public polling payloads small. The internal decision pipeline uses
            # analysis_snapshot() to access the bounded ranked pool when explicitly needed.
            data["analysis_candidates"] = []
            data["analysis_pool_size"] = len(self._analysis_candidates)
            data["cache_hit"] = data.get("status") == "complete"
            return data

    def analysis_snapshot(self) -> dict[str, Any]:
        """Internal bounded top-liquidity pool for the analysis worker, not dashboard polling."""
        data = self.snapshot()
        with self._state_lock:
            data["analysis_candidates"] = [dict(row) for row in self._analysis_candidates]
        return data

    def start(self, *, force: bool = True) -> dict[str, Any]:
        with self._job_lock:
            now_mono = time.monotonic()
            with self._state_lock:
                status = self._state.get("status")
                if status == "running":
                    return self.snapshot()
                # Coalesce double-clicks/concurrent POSTs even if a tiny test or a
                # one-page catalog finishes before the next caller obtains the lock.
                if status == "complete" and now_mono - self._last_start_monotonic < 3.0:
                    return self.snapshot()
                if status == "complete" and not force:
                    completed = self._parse_stamp(self._state.get("last_completed_at"))
                    age = (datetime.now(timezone.utc) - completed).total_seconds() if completed else float("inf")
                    if age < self._refresh_interval:
                        return self.snapshot()
                if status == "complete" or status == "idle" or not self._cursor:
                    self._reset_scan()
                else:
                    # A failed or capped scan resumes from its last safe cursor; reset the
                    # per-run page budget while retaining aggregate counts and seen tickers.
                    self._state["pages_scanned"] = 0
                    self._state["started_at"] = _stamp()
                self._state.update({"status": "running", "error_code": None, "error_message": None,
                                    "updated_at": _stamp(), "started_at": self._state.get("started_at") or _stamp()})
                self._state["max_pages"] = self._max_pages
                self._state["max_markets"] = self._max_markets
            self._last_start_monotonic = time.monotonic()
            self._thread = threading.Thread(target=self._scan, name="kalshi-market-catalog-scan", daemon=True)
            self._thread.start()
            return self.snapshot()

    def _reset_scan(self) -> None:
        self._cursor = None
        self._seen_cursors.clear()
        self._seen_tickers.clear()
        self._category_counts.clear()
        self._route_counts.clear()
        self._samples.clear()
        self._analysis_candidates.clear()
        self._analysis_heap.clear()
        self._analysis_seen = 0
        self._state = self._empty_state()
        self._state["started_at"] = _stamp()
        self._state["max_pages"] = self._max_pages
        self._state["max_markets"] = self._max_markets

    @staticmethod
    def _parse_stamp(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None

    def _publish_progress(self, *, status: str | None = None, warning: str | None = None) -> None:
        with self._state_lock:
            if status:
                self._state["status"] = status
            if warning is not None:
                self._state["pagination_warning"] = warning
            self._state["updated_at"] = _stamp()
            self._state["candidate_count"] = len(self._seen_tickers)
            self._state["category_counts"] = dict(self._category_counts)
            self._state["route_counts"] = dict(self._route_counts)
            self._state["markets"] = list(self._samples)
            self._state["analysis_candidates"] = list(self._analysis_candidates)
            self._state["analysis_pool_size"] = self._analysis_pool_size
            self._state["returned_count"] = len(self._samples)
            self._state["cursor_available"] = bool(self._cursor)

    @staticmethod
    def _activity_score(row: dict[str, Any]) -> tuple[float, float, str]:
        """Rank by known 24h volume, then open interest; unknown values rank last."""
        def number(value: Any) -> float:
            try:
                result = float(value)
                return result if result == result and abs(result) != float("inf") and result >= 0 else 0.0
            except (TypeError, ValueError, OverflowError):
                return 0.0
        volume = number(row.get("volume_24h"))
        open_interest = number(row.get("open_interest"))
        return (volume, open_interest, str(row.get("ticker") or ""))

    def _rank_analysis_candidate(self, row: dict[str, Any]) -> None:
        """Retain top activity-ranked rows in O(log N) per market via a bounded min-heap."""
        volume, open_interest, ticker = self._activity_score(row)
        entry = (volume, open_interest, ticker, row)
        if len(self._analysis_heap) < self._analysis_pool_size:
            heapq.heappush(self._analysis_heap, entry)
        elif (volume, open_interest, ticker) > self._analysis_heap[0][:3]:
            heapq.heapreplace(self._analysis_heap, entry)
        self._analysis_seen += 1
        # The published list is sorted only as pages are published, not per market.
        self._analysis_candidates = [item[3] for item in sorted(self._analysis_heap, key=lambda x: x[:3], reverse=True)]

    def _scan(self) -> None:
        try:
            while True:
                with self._state_lock:
                    pages = int(self._state.get("pages_scanned") or 0)
                    count = len(self._seen_tickers)
                    cursor = self._cursor
                if pages >= self._max_pages:
                    self._publish_progress(status="partial", warning=f"Safety cap reached at {self._max_pages} pages; click scan again to continue from the saved cursor.")
                    return
                if count >= self._max_markets:
                    self._publish_progress(status="partial", warning=f"Safety cap reached at {self._max_markets} distinct markets; click scan again to continue.")
                    return
                response = self._provider(status="open", limit=200, cursor=cursor)
                if not isinstance(response, dict):
                    raise KalshiAPIError("Kalshi returned an invalid market-catalog response object.")
                items = response.get("markets", [])
                if not isinstance(items, list):
                    raise KalshiAPIError("Kalshi returned an invalid market-catalog page.")
                for market in items[:200]:
                    if not isinstance(market, dict):
                        continue
                    ticker_raw = market.get("ticker") or market.get("market_ticker")
                    ticker = ticker_raw[:200].strip() if isinstance(ticker_raw, str) else ""
                    # Public rows without tickers can't safely be deduplicated/classified; skip them.
                    if not ticker or ticker in self._seen_tickers:
                        continue
                    self._seen_tickers.add(ticker)
                    row = _safe_market_row(market)
                    category = str(row.get("category") or "unclassified")
                    route = str(row.get("route_status") or "blocked_unclassified")
                    self._category_counts[category] += 1
                    self._route_counts[route] += 1
                    if len(self._samples) < 50:
                        self._samples.append(row)
                    self._rank_analysis_candidate(row)
                    if len(self._seen_tickers) >= self._max_markets:
                        break
                with self._state_lock:
                    self._state["pages_scanned"] = pages + 1
                raw_cursor = response.get("cursor")
                if raw_cursor is None or raw_cursor == "" or raw_cursor is False:
                    self._cursor = None
                    self._publish_progress(status="complete")
                    with self._state_lock:
                        self._state["last_completed_at"] = _stamp()
                        self._state["pagination_warning"] = None
                    return
                if not isinstance(raw_cursor, str):
                    self._publish_progress(status="partial", warning="Catalog returned a non-text cursor; scan stopped safely.")
                    return
                next_cursor = raw_cursor.strip()
                if not next_cursor:
                    self._cursor = None
                    self._publish_progress(status="complete")
                    with self._state_lock:
                        self._state["last_completed_at"] = _stamp()
                    return
                if len(next_cursor) > 512:
                    self._publish_progress(status="partial", warning="Catalog cursor exceeded 512 characters; scan stopped safely.")
                    return
                if next_cursor in self._seen_cursors or next_cursor == cursor:
                    self._cursor = None
                    self._publish_progress(status="partial", warning="Catalog cursor repeated; scan stopped safely to avoid a pagination loop.")
                    return
                self._seen_cursors.add(next_cursor)
                self._cursor = next_cursor
                self._publish_progress(status="running")
                if self._page_delay:
                    time.sleep(self._page_delay)
        except KalshiAPIError as exc:
            log.warning("Kalshi market scan upstream failure (%s).", type(exc).__name__)
            self._publish_progress(status="failed")
            with self._state_lock:
                self._state["error_code"] = "kalshi_catalog_unavailable"
                self._state["error_message"] = "Kalshi's public market catalog request failed. Verify the configured API base URL and service connectivity; retry will resume the scan. No trades were attempted."
        except Exception as exc:  # Keep background thread healthy; never leak exception content to dashboard.
            log.exception("Unexpected market scanner failure (%s).", type(exc).__name__)
            self._publish_progress(status="failed")
            with self._state_lock:
                self._state["error_code"] = "market_scanner_internal_error"
                self._state["error_message"] = "The market scan stopped unexpectedly. Check the service logs; no trades were attempted."

    def start_scheduler(self, enabled: bool = True) -> None:
        if not enabled:
            return
        with self._scheduler_lock:
            if self._scheduler_started:
                return
            self._scheduler_started = True
        threading.Thread(target=self._scheduler_loop, name="kalshi-market-scan-scheduler", daemon=True).start()

    def _scheduler_loop(self) -> None:
        retry_seconds = _int_env("MARKET_SCAN_RETRY_SECONDS", 60, 30, 1800)
        from runtime_controls import market_autopilot_enabled
        while True:
            # Dashboard master switch is persisted in shared DB; OFF pauses new
            # automatic cycles. A scan already underway is allowed to finish its
            # bounded cursor pass, but it never places orders.
            if not market_autopilot_enabled():
                time.sleep(5)
                continue
            snap = self.snapshot()
            if snap["status"] == "idle":
                self.start(force=True)
            elif snap["status"] == "complete":
                completed = self._parse_stamp(snap.get("last_completed_at"))
                age = (datetime.now(timezone.utc) - completed).total_seconds() if completed else float("inf")
                if age >= self._refresh_interval:
                    self.start(force=True)
            elif snap["status"] in {"failed", "partial"}:
                updated = self._parse_stamp(snap.get("updated_at"))
                age = (datetime.now(timezone.utc) - updated).total_seconds() if updated else float("inf")
                if age >= retry_seconds:
                    self.start(force=True)
            time.sleep(5)

    def reset_for_tests(self) -> None:
        with self._job_lock, self._state_lock:
            self._cursor = None
            self._seen_cursors.clear()
            self._seen_tickers.clear()
            self._category_counts.clear()
            self._route_counts.clear()
            self._samples.clear()
            self._analysis_candidates.clear()
            self._analysis_heap.clear()
            self._analysis_seen = 0
            self._state = self._empty_state()
            self._thread = None
            self._last_start_monotonic = 0.0
