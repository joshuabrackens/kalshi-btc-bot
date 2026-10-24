"""Encrypted persistence and read-only verification for Kalshi API credentials.

Credentials are encrypted with a separately managed Fernet key before storage. Only
a key ID suffix is exposed to the UI. This module intentionally provides no order
submission functions; live execution remains blocked until the order lifecycle is
implemented, demo-tested, and approved.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

import requests
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, ed25519

from db_store import DatabaseConnection

Environment = Literal["demo", "production"]
BASE_URLS: dict[str, str] = {
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "production": "https://external-api.kalshi.com/trade-api/v2",
}
CREDENTIAL_TABLE = "kalshi_credentials"
MAX_PRIVATE_KEY_LENGTH = 16_000


class CredentialVaultError(RuntimeError):
    pass


def normalize_environment(value: Any) -> str:
    environment = str(value or "").strip().lower()
    if environment not in BASE_URLS:
        raise ValueError("environment must be 'demo' or 'production'.")
    return environment


def _fernet() -> Fernet:
    key = os.getenv("KALSHI_CREDENTIALS_ENCRYPTION_KEY", "").strip()
    if not key:
        raise CredentialVaultError("KALSHI_CREDENTIALS_ENCRYPTION_KEY is not configured.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise CredentialVaultError("KALSHI_CREDENTIALS_ENCRYPTION_KEY is not a valid Fernet key.") from exc


def validate_private_key(api_key_id: Any, private_key_pem: Any) -> tuple[str, str, str]:
    if not isinstance(api_key_id, str):
        raise ValueError("API Key ID must be text.")
    key_id = api_key_id.strip()
    if not 1 <= len(key_id) <= 200 or any(ch.isspace() for ch in key_id):
        raise ValueError("API Key ID must be 1–200 non-whitespace characters.")
    if not isinstance(private_key_pem, str):
        raise ValueError("Private key PEM must be text.")
    pem = private_key_pem.strip()
    if not 100 <= len(pem) <= MAX_PRIVATE_KEY_LENGTH:
        raise ValueError(f"Private key PEM must be between 100 and {MAX_PRIVATE_KEY_LENGTH} characters.")
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise ValueError("Private key must be a valid unencrypted PEM private key generated for your Kalshi API key.") from exc
    if not isinstance(key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
        raise ValueError("Private key type is unsupported; use RSA or Ed25519 as documented by Kalshi.")
    suffix = key_id[-4:]
    fingerprint = hashlib.sha256(key_id.encode("utf-8")).hexdigest()[:12]
    return key_id, pem, f"…{suffix} · {fingerprint}"


def save_credentials(conn: DatabaseConnection, environment: Any, api_key_id: Any, private_key_pem: Any) -> dict[str, Any]:
    env = normalize_environment(environment)
    key_id, pem, hint = validate_private_key(api_key_id, private_key_pem)
    encryptor = _fernet()
    payload = json.dumps({"api_key_id": key_id, "private_key_pem": pem}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    encrypted = encryptor.encrypt(payload).decode("ascii")
    updated = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO kalshi_credentials (environment, key_id_hint, encrypted_payload, updated_at)
           VALUES (?,?,?,?) ON CONFLICT(environment) DO UPDATE SET
           key_id_hint=excluded.key_id_hint, encrypted_payload=excluded.encrypted_payload,
           updated_at=excluded.updated_at""",
        (env, hint, encrypted, updated),
    )
    return {"environment": env, "configured": True, "key_id_hint": hint, "updated_at": updated}


def credential_status(conn: DatabaseConnection) -> dict[str, Any]:
    rows = conn.execute("SELECT environment, key_id_hint, updated_at FROM kalshi_credentials ORDER BY environment").fetchall()
    items = {str(row[0]): {"configured": True, "key_id_hint": str(row[1]), "updated_at": str(row[2])} for row in rows}
    return {"configured_environments": items, "demo_configured": "demo" in items, "production_configured": "production" in items,
            "private_key_returned": False, "at_rest_encryption": "Fernet; key supplied via KALSHI_CREDENTIALS_ENCRYPTION_KEY"}


def load_credentials(conn: DatabaseConnection, environment: Any) -> dict[str, str]:
    env = normalize_environment(environment)
    row = conn.execute("SELECT encrypted_payload FROM kalshi_credentials WHERE environment=?", (env,)).fetchone()
    if not row:
        raise CredentialVaultError(f"No Kalshi {env} credentials have been saved.")
    try:
        payload = _fernet().decrypt(str(row[0]).encode("ascii"))
        data = json.loads(payload.decode("utf-8"))
        key_id, pem, _hint = validate_private_key(data.get("api_key_id"), data.get("private_key_pem"))
        return {"api_key_id": key_id, "private_key_pem": pem, "environment": env}
    except (InvalidToken, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CredentialVaultError("Saved credentials could not be decrypted or validated; check encryption-key continuity.") from exc


def delete_credentials(conn: DatabaseConnection, environment: Any) -> bool:
    env = normalize_environment(environment)
    conn.execute("DELETE FROM kalshi_credentials WHERE environment=?", (env,))
    return True


def _sign(private_key: Any, timestamp: str, method: str, path: str) -> str:
    message = f"{timestamp}{method.upper()}{path.split('?', 1)[0]}".encode("utf-8")
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        sig = private_key.sign(message)
    elif isinstance(private_key, rsa.RSAPrivateKey):
        sig = private_key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    else:
        raise CredentialVaultError("Unsupported private key type.")
    return base64.b64encode(sig).decode("ascii")


def verify_credentials_read_only(credentials: dict[str, str], *, timeout_seconds: float = 8.0) -> dict[str, Any]:
    """Verify saved API keys with one authenticated GET to the balance endpoint; no orders."""
    env = normalize_environment(credentials.get("environment"))
    base = BASE_URLS[env]
    key_id, pem, _hint = validate_private_key(credentials.get("api_key_id"), credentials.get("private_key_pem"))
    try:
        private_key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise CredentialVaultError("Saved private key is invalid.") from exc
    timestamp = str(int(time.time() * 1000))
    path = urlparse(base).path.rstrip("/") + "/portfolio/balance"
    headers = {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": _sign(private_key, timestamp, "GET", path),
        "Accept": "application/json",
    }
    try:
        response = requests.get(base.rstrip("/") + "/portfolio/balance", headers=headers,
                                timeout=max(2.0, min(float(timeout_seconds), 15.0)))
    except requests.RequestException as exc:
        raise CredentialVaultError("Could not reach Kalshi for read-only credential verification.") from exc
    if response.status_code == 401:
        raise CredentialVaultError("Kalshi rejected the credentials (401). Check that the key ID and private key match the selected environment.")
    if response.status_code >= 400:
        raise CredentialVaultError(f"Kalshi read-only verification returned HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise CredentialVaultError("Kalshi returned an invalid balance response.") from exc
    if not isinstance(data, dict):
        raise CredentialVaultError("Kalshi returned an invalid balance response.")
    # Return a sanitized summary only. Do not send response body or credentials to the browser.
    balance = data.get("balance")
    portfolio_value = data.get("portfolio_value")
    return {"verified": True, "environment": env, "balance_cents": balance if isinstance(balance, (int, float)) else None,
            "portfolio_value_cents": portfolio_value if isinstance(portfolio_value, (int, float)) else None,
            "read_only_check": True, "orders_placed": False}
