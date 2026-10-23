"""Fail-closed trading-mode status and dashboard selection gate.

This build is paper-only.  A real-order submission/cancel/fill-reconciliation
adapter does not exist here, so LIVE_TRADING_ENABLED can never authorize live
execution by itself.  This module deliberately contains no broker/order calls.
"""
from __future__ import annotations

import os
from typing import Any

# This is a code-level invariant, not an environment toggle. It must remain False
# until a separately reviewed and tested live order adapter is actually implemented.
REAL_ORDER_ADAPTER_INSTALLED = False
LIVE_APPROVAL_RECORDED = False

LIVE_BLOCKERS: tuple[str, ...] = (
    "No real-order adapter is installed; this build has no order-submission path.",
    "An end-to-end authenticated order/settlement cycle has not been proven against Kalshi Demo with separate demo credentials.",
    "Order idempotency, partial-fill handling, reject/cancel reconciliation, and restart recovery are not validated for live orders.",
    "Market-specific fees and expected slippage have not been integrated into a live pre-trade gate.",
    "An authenticated dashboard, per-user data isolation, and per-user risk limits are not production-enabled.",
    "A live emergency stop that cancels resting orders and reconciles exchange state is not implemented.",
    "Live activation has not passed the required separate operational approval and staged rollout.",
)


def _truthy(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def trading_mode_status() -> dict[str, Any]:
    """Report the effective safe state, not merely what environment variables say."""
    configured_mode = os.getenv("TRADING_MODE", "paper").strip().lower()
    configured_live_flag = _truthy("LIVE_TRADING_ENABLED", False)
    paper_config_safe = configured_mode == "paper" and not configured_live_flag

    warnings: list[str] = []
    if configured_mode != "paper":
        warnings.append("TRADING_MODE is not 'paper'; the paper worker will refuse to run.")
    if configured_live_flag:
        warnings.append("LIVE_TRADING_ENABLED is true but this build has no real-order adapter; live trading remains unavailable.")

    return {
        "build_mode": "paper_only",
        "effective_mode": "paper" if paper_config_safe else "blocked",
        "configured_mode": configured_mode,
        "paper_execution_enabled": paper_config_safe,
        "live_execution_enabled": False,
        "live_mode_available": bool(REAL_ORDER_ADAPTER_INSTALLED and LIVE_APPROVAL_RECORDED),
        "real_order_adapter_installed": REAL_ORDER_ADAPTER_INSTALLED,
        "live_approval_recorded": LIVE_APPROVAL_RECORDED,
        "selected_mode": "paper" if paper_config_safe else "blocked",
        "warnings": warnings,
        "live_blockers": list(LIVE_BLOCKERS),
        "control_behavior": "The dashboard live button only requests a readiness check; this version cannot enable or place real orders.",
    }


class LiveModeLocked(RuntimeError):
    """Raised when a caller requests live trading while the hard safety gate is closed."""

    def __init__(self, status: dict[str, Any] | None = None):
        self.status = status or trading_mode_status()
        super().__init__("Real trading is locked in this paper-only build.")


def request_mode(mode: str) -> dict[str, Any]:
    """Handle dashboard mode requests without changing deployment configuration.

    Paper selection is acknowledged when the deployment is configured safely.
    Live selection always fails closed until code and operational gates are reviewed.
    """
    normalized = str(mode or "").strip().lower()
    if normalized not in {"paper", "real"}:
        raise ValueError("requested_mode must be 'paper' or 'real'.")

    status = trading_mode_status()
    if normalized == "real":
        if not (REAL_ORDER_ADAPTER_INSTALLED and LIVE_APPROVAL_RECORDED):
            raise LiveModeLocked(status)
        # Intentionally unreachable in this version. Do not add an order call here.
        raise LiveModeLocked(status)

    if not status["paper_execution_enabled"]:
        return {
            "accepted": False,
            "effective_mode": "blocked",
            "message": "Set TRADING_MODE=paper and LIVE_TRADING_ENABLED=false in the deployment environment, then redeploy.",
            "status": status,
        }
    return {
        "accepted": True,
        "effective_mode": "paper",
        "persisted": False,
        "message": "Paper mode is active. The selected mode is controlled by deployment configuration, not a browser-only switch.",
        "status": status,
    }


def live_readiness_report() -> dict[str, Any]:
    """Operational checklist for a future real-order implementation; no enablement."""
    checks = [
        {"key": "paper_safety_lock", "label": "Paper-only safety lock is enforced in code", "status": "pass", "detail": "This build contains no real-order submission code."},
        {"key": "worker_mode_guard", "label": "Worker rejects non-paper configuration", "status": "pass", "detail": "The paper worker refuses to run if TRADING_MODE is not paper or LIVE_TRADING_ENABLED is true."},
        {"key": "order_adapter", "label": "Authenticated order submit, cancel, and status adapter", "status": "blocked", "detail": "Not implemented; current Kalshi module is read-only public market data."},
        {"key": "demo_validation", "label": "End-to-end order, fill, cancel, and settlement tests against Kalshi Demo", "status": "blocked", "detail": "No authenticated Demo credentials or order adapter were available during this session. Demo and production credentials must remain separate."},
        {"key": "idempotency", "label": "Exchange order idempotency and duplicate-submit recovery", "status": "blocked", "detail": "Must be proven using client order IDs and simulated timeout/retry cases."},
        {"key": "fills", "label": "Partial fills, rejected orders, cancel races, and restart reconciliation", "status": "blocked", "detail": "Must reconcile application state against exchange orders/fills/positions."},
        {"key": "fees_slippage", "label": "Live fee schedule, round-trip fees, spread, and slippage gates", "status": "blocked", "detail": "Must be market-aware and based on current exchange rules; not just a fixed paper fee assumption."},
        {"key": "risk_limits", "label": "Hard portfolio exposure, per-user budget, daily loss cap, and stale-data gates", "status": "blocked", "detail": "Requires account-scoped risk state and concurrency-safe reservations."},
        {"key": "auth_tenant_isolation", "label": "Authenticated dashboard/API and end-to-end multi-user isolation", "status": "blocked", "detail": "Current dashboard APIs are not yet scoped to authenticated tenant identities."},
        {"key": "kill_switch", "label": "Emergency stop and cancel-all/resting-order reconciliation", "status": "blocked", "detail": "A flag alone is insufficient; exchange state must be reconciled after stop."},
        {"key": "deployment_approval", "label": "Separate approval, canary deployment, rollback, and monitoring runbook", "status": "blocked", "detail": "Must be completed after all other checks and out-of-sample paper evidence."},
    ]
    ready = all(c["status"] == "pass" for c in checks)
    return {
        "live_ready": ready,
        "live_execution_enabled": False,
        "mode": "SIMULATION_ONLY",
        "passed_checks": sum(c["status"] == "pass" for c in checks),
        "total_checks": len(checks),
        "checks": checks,
        "decision": "BLOCKED: do not enable real trading. The current build is paper-only.",
    }
