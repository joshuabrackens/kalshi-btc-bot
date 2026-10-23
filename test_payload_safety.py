"""Security regression tests for webhook payload redaction and size limits."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from payload_safety import sanitize_sensitive_payload


def _contains_sensitive_key(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
            if any(part in normalized for part in ("secret", "token", "apikey", "authorization", "password", "passwd", "privatekey", "credential", "clientkey")):
                return True
            if _contains_sensitive_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def test_nested_sensitive_values_are_removed_without_mutating_input():
    source = {
        "direction": "UP",
        "secret": "root-secret",
        "metadata": {"apiKey": "key-123", "ok": 1, "nested": [{"access_token": "token-456", "text": "keep"}]},
        "authorization": "Bearer abc",
    }
    cleaned = sanitize_sensitive_payload(source)
    assert not _contains_sensitive_key(cleaned)
    assert cleaned["direction"] == "UP"
    assert cleaned["metadata"]["ok"] == 1
    assert cleaned["metadata"]["nested"][0]["text"] == "keep"
    assert "secret" in source  # sanitizer is non-mutating


def test_deep_nesting_is_bounded():
    value = {"level": {"level": {"level": "end"}}}
    assert sanitize_sensitive_payload(value, max_depth=1)["level"]["level"] == "[truncated: maximum payload nesting depth]"


def test_webhook_storage_redacts_nested_credentials():
    # Import after setting module stubs only if a provider isn't already loaded.
    import main
    import db_store

    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "DATABASE_URL": "",
            "BOT_DB_PATH": str(Path(tmp) / "webhook.sqlite3"),
            "GAINZALGO_WEBHOOK_SECRET": "expected-secret",
            "PAPER_WORKER_ENABLED": "false",
        }
        payload = {
            "secret": "expected-secret", "direction": "UP", "symbol": "BTCUSDT", "timeframe": "1m",
            "metadata": {"token": "nested-secret", "apiKey": "key-abc", "comment": "safe"},
        }

        class FakeRequest:
            async def json(self):
                return payload

        with patch.dict(os.environ, env, clear=False):
            result = asyncio.run(main.gainzalgo_webhook(FakeRequest()))
            assert result["status"] == "recorded"
            conn = db_store.connect_database()
            try:
                db_store.ensure_schema(conn)
                row = conn.execute("SELECT payload_json FROM gainzalgo_signals ORDER BY id DESC LIMIT 1").fetchone()
                stored = json.loads(row[0])
            finally:
                conn.close()
            assert not _contains_sensitive_key(stored)
            assert stored["metadata"]["comment"] == "safe"


def test_large_content_length_is_rejected_before_json_parse():
    import main

    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {
            "DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "size.sqlite3"),
            "GAINZALGO_WEBHOOK_SECRET": "expected-secret", "GAINZALGO_WEBHOOK_MAX_BODY_BYTES": "1024",
        }, clear=False):
            class FakeRequest:
                headers = {"content-length": "2048"}
                async def json(self):
                    raise AssertionError("must reject from content-length before parsing")

            with pytest.raises(HTTPException) as exc:
                asyncio.run(main.gainzalgo_webhook(FakeRequest()))
            assert exc.value.status_code == 413


def test_streaming_body_limit_rejects_chunked_payload_without_content_length():
    import main

    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {
            "DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "stream-size.sqlite3"),
            "GAINZALGO_WEBHOOK_SECRET": "expected-secret", "GAINZALGO_WEBHOOK_MAX_BODY_BYTES": "1024",
        }, clear=False):
            class FakeStreamingRequest:
                headers = {}
                async def stream(self):
                    yield b"x" * 600
                    yield b"x" * 600

            with pytest.raises(HTTPException) as exc:
                asyncio.run(main.gainzalgo_webhook(FakeStreamingRequest()))
            assert exc.value.status_code == 413
