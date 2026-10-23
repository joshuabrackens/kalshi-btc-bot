"""Small, audited security primitives for future account/session integration.

These primitives are not by themselves a multi-user product: do not enable public
account creation until authentication middleware and every journal/worker query are
scoped to the authenticated account. Session tokens are stored as digests only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Any

SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
MIN_SALT_LENGTH = 16
MAX_SALT_LENGTH = 64


def hash_password(password: str, *, salt: bytes | None = None) -> dict[str, str]:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must contain at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must not exceed {MAX_PASSWORD_LENGTH} characters.")
    if salt is not None and len(salt) < MIN_SALT_LENGTH:
        raise ValueError(f"Password salt must contain at least {MIN_SALT_LENGTH} bytes.")
    if salt is not None and len(salt) > MAX_SALT_LENGTH:
        raise ValueError(f"Password salt must not exceed {MAX_SALT_LENGTH} bytes.")
    salt_bytes = salt if salt is not None else secrets.token_bytes(MIN_SALT_LENGTH)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt_bytes, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return {
        "scheme": "scrypt",
        "salt_b64": base64.urlsafe_b64encode(salt_bytes).decode("ascii"),
        "digest_b64": base64.urlsafe_b64encode(digest).decode("ascii"),
    }


def _decode_b64url_field(value: Any, *, max_chars: int, field_name: str) -> bytes:
    """Strictly decode a size-bounded base64url field from a password record."""
    if not isinstance(value, str) or not value or len(value) > max_chars:
        raise ValueError(f"Invalid {field_name} encoding length.")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"Invalid {field_name} encoding.") from exc
    encoded += b"=" * (-len(encoded) % 4)
    return base64.b64decode(encoded, altchars=b"-_", validate=True)


def verify_password(password: str, record: dict[str, Any]) -> bool:
    """Return False for malformed/untrusted records rather than raising or doing unbounded work."""
    if not isinstance(record, dict):
        return False
    try:
        if record.get("scheme") != "scrypt":
            return False
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
            return False
        # Hash-generated salt/digest fields are tiny. Bound them before decoding so a
        # corrupt database record cannot force large allocations or excessive work.
        salt = _decode_b64url_field(record.get("salt_b64"), max_chars=88, field_name="salt")
        expected = _decode_b64url_field(record.get("digest_b64"), max_chars=44, field_name="digest")
        if not MIN_SALT_LENGTH <= len(salt) <= MAX_SALT_LENGTH or len(expected) != SCRYPT_DKLEN:
            return False
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
        return hmac.compare_digest(candidate, expected)
    except (KeyError, TypeError, ValueError, UnicodeError, AttributeError):
        return False


def create_session_token() -> tuple[str, str]:
    """Return (one-time raw bearer token, SHA-256 verifier to store in DB)."""
    token = secrets.token_urlsafe(32)
    return token, session_token_digest(token)


def session_token_digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8", errors="replace")).hexdigest()


def parse_utc_timestamp(value: str | datetime) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def session_is_valid(*, expires_at: str | datetime, revoked_at: str | datetime | None = None,
                     now: datetime | None = None) -> bool:
    expiry = parse_utc_timestamp(expires_at)
    if expiry is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if expiry <= current.astimezone(timezone.utc):
        return False
    if revoked_at is not None:
        # `revoked_at` is populated only after revocation. Never let a malformed
        # or future timestamp turn a revoked record back into a valid session.
        return False
    return True


def multi_user_readiness() -> dict[str, Any]:
    checks = [
        {"key": "password_hashing", "status": "implemented_primitives", "detail": "Scrypt password hashing and constant-time verification helpers are present; no login endpoint is wired."},
        {"key": "session_tokens", "status": "implemented_primitives", "detail": "Cryptographically random session tokens and digest-only storage helpers are present; lifecycle storage is not wired."},
        {"key": "auth_middleware", "status": "blocked", "detail": "All private routes need server-side session authentication and authorization."},
        {"key": "tenant_scoping", "status": "blocked", "detail": "Trade journal, alerts, analytics, webhook queue, learning history, and worker queries are not yet account-scoped."},
        {"key": "account_risk_limits", "status": "blocked", "detail": "Per-user limits and atomic portfolio exposure reservations are not enabled."},
        {"key": "account_webhooks", "status": "blocked", "detail": "Each account needs its own rotatable webhook credential and idempotency namespace."},
        {"key": "cross_tenant_tests", "status": "blocked", "detail": "Cross-account read/write and worker isolation tests are not complete."},
    ]
    return {
        "enabled": False,
        "production_ready": False,
        "public_signup_enabled": False,
        "checks": checks,
        "decision": "Multi-user access remains disabled until all account-scoping and cross-tenant checks pass.",
    }
