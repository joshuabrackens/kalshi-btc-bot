"""Small, dependency-free helpers for safely storing untrusted webhook payloads."""
from __future__ import annotations

import re
from typing import Any

_SENSITIVE_PARTS = (
    "secret", "token", "apikey", "authorization", "password", "passwd",
    "privatekey", "credential", "clientsecret", "clientkey",
)
_KEY_NORMALIZER = re.compile(r"[^a-z0-9]+")


def _sensitive_key(key: Any) -> bool:
    normalized = _KEY_NORMALIZER.sub("", str(key).lower())
    return any(part in normalized for part in _SENSITIVE_PARTS)


def sanitize_sensitive_payload(value: Any, *, max_depth: int = 16, _depth: int = 0) -> Any:
    """Return JSON-compatible payload data with secrets removed at every depth.

    Webhook contents are untrusted. Sensitive-looking keys are omitted recursively,
    including keys inside nested dictionaries and lists. Very deeply nested payloads
    are cut off to avoid pathological recursion. The function does not mutate input.
    """
    if _depth > max(1, int(max_depth)):
        return "[truncated: maximum payload nesting depth]"
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if _sensitive_key(key):
                continue
            cleaned[str(key)] = sanitize_sensitive_payload(item, max_depth=max_depth, _depth=_depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [sanitize_sensitive_payload(item, max_depth=max_depth, _depth=_depth + 1) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
