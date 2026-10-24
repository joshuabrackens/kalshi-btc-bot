"""Short-lived, server-signed admin cookie for protected dashboard controls.

The secret is never delivered to JavaScript or stored in browser storage. SameSite
Strict and HttpOnly cookies let the browser preserve an unlocked admin session while
keeping the admin key out of subsequent API request bodies.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

COOKIE_NAME = "btc_admin_session"
SESSION_TTL_SECONDS = 12 * 60 * 60


def create_admin_cookie(secret: str, *, now: int | None = None) -> str:
    issued = int(time.time() if now is None else now)
    nonce = secrets.token_urlsafe(24)
    payload = f"v1.{issued}.{nonce}"
    signature = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def validate_admin_cookie(value: Any, secret: str, *, now: int | None = None) -> bool:
    if not isinstance(value, str) or len(value) > 256 or not isinstance(secret, str) or len(secret) < 32:
        return False
    pieces = value.split(".")
    if len(pieces) != 4 or pieces[0] != "v1":
        return False
    _, issued_raw, nonce, signature = pieces
    if not nonce or len(nonce) > 64 or len(signature) != 64:
        return False
    try:
        issued = int(issued_raw)
    except (TypeError, ValueError, OverflowError):
        return False
    current = int(time.time() if now is None else now)
    if issued > current + 60 or current - issued > SESSION_TTL_SECONDS:
        return False
    payload = f"v1.{issued}.{nonce}"
    expected = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
