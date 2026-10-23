"""Manual journal/evaluation writes are disabled by default and require a configured secret."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import main


def test_manual_write_api_is_disabled_when_admin_secret_is_missing():
    with patch.dict(os.environ, {"PAPER_ADMIN_API_SECRET": ""}, clear=False):
        with pytest.raises(HTTPException) as exc:
            main._require_manual_api_auth(SimpleNamespace(headers={}))
        assert exc.value.status_code == 403


def test_manual_write_api_rejects_missing_or_wrong_secret():
    with patch.dict(os.environ, {"PAPER_ADMIN_API_SECRET": "long-random-secret"}, clear=False):
        for headers in ({}, {"x-paper-admin-secret": "wrong"}):
            with pytest.raises(HTTPException) as exc:
                main._require_manual_api_auth(SimpleNamespace(headers=headers))
            assert exc.value.status_code == 401


def test_manual_write_api_accepts_correct_secret():
    with patch.dict(os.environ, {"PAPER_ADMIN_API_SECRET": "long-random-secret"}, clear=False):
        main._require_manual_api_auth(SimpleNamespace(headers={"x-paper-admin-secret": "long-random-secret"}))


def test_evaluation_request_caps_each_timeframe_at_500_candles():
    field = main.EvaluateRequest.model_fields["candles_1m"]
    assert field.metadata
    assert any(getattr(meta, "max_length", None) == 500 for meta in field.metadata)
