from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from trading_mode import LiveModeLocked, live_readiness_report, request_mode, trading_mode_status


def test_default_deployment_is_effectively_paper_and_never_live():
    with patch.dict(os.environ, {"TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false"}, clear=False):
        status = trading_mode_status()
    assert status["effective_mode"] == "paper"
    assert status["paper_execution_enabled"] is True
    assert status["live_execution_enabled"] is False
    assert status["real_order_adapter_installed"] is False


@pytest.mark.parametrize("env", [
    {"TRADING_MODE": "live", "LIVE_TRADING_ENABLED": "false"},
    {"TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "true"},
])
def test_unsafe_environment_configuration_fails_closed(env):
    with patch.dict(os.environ, env, clear=False):
        status = trading_mode_status()
    assert status["effective_mode"] == "blocked"
    assert status["live_execution_enabled"] is False
    assert status["paper_execution_enabled"] is False
    assert status["warnings"]


def test_real_mode_request_is_always_rejected_and_no_order_path_exists():
    with pytest.raises(LiveModeLocked) as exc:
        request_mode("real")
    assert exc.value.status["live_execution_enabled"] is False
    assert "No real-order adapter" in " ".join(exc.value.status["live_blockers"])


def test_paper_request_acknowledges_deployment_control_not_browser_mutation():
    with patch.dict(os.environ, {"TRADING_MODE": "paper", "LIVE_TRADING_ENABLED": "false"}, clear=False):
        result = request_mode("paper")
    assert result["accepted"] is True
    assert result["persisted"] is False
    assert result["effective_mode"] == "paper"


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError):
        request_mode("maybe")


def test_live_readiness_never_passes_until_all_gates_are_complete():
    report = live_readiness_report()
    assert report["live_ready"] is False
    assert report["live_execution_enabled"] is False
    assert report["passed_checks"] < report["total_checks"]
    assert any(item["key"] == "kill_switch" and item["status"] == "blocked" for item in report["checks"])
