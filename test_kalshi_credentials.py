"""Regression tests for encrypted one-time credential storage and admin-only APIs."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

import main
from admin_session import create_admin_cookie, validate_admin_cookie, SESSION_TTL_SECONDS
from db_store import connect_database, ensure_schema


def ed25519_pem() -> str:
    key = ed25519.Ed25519PrivateKey.generate()
    return key.private_bytes(encoding=serialization.Encoding.PEM,
                             format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption()).decode("ascii")


def test_admin_cookie_is_signed_expiring_and_tamper_resistant():
    secret = "S" * 48
    token = create_admin_cookie(secret, now=1000)
    assert validate_admin_cookie(token, secret, now=1001)
    assert not validate_admin_cookie(token, "T" * 48, now=1001)
    assert not validate_admin_cookie(token + "x", secret, now=1001)
    assert not validate_admin_cookie(token, secret, now=1000 + SESSION_TTL_SECONDS + 1)
    assert not validate_admin_cookie("junk", secret, now=1001)


def test_vault_encrypts_credentials_and_status_never_returns_pem():
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "vault.sqlite3"),
               "KALSHI_CREDENTIALS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
               "MARKET_SCANNER_AUTO_START": "false"}
        pem = ed25519_pem()
        with patch.dict(os.environ, env, clear=False):
            conn = connect_database()
            try:
                ensure_schema(conn)
                from kalshi_credentials import save_credentials, credential_status, load_credentials
                saved = save_credentials(conn, "demo", "my-test-key-id-9876", pem)
                conn.commit()
                status = credential_status(conn)
                row = conn.execute("SELECT encrypted_payload,key_id_hint FROM kalshi_credentials WHERE environment=?", ("demo",)).fetchone()
                decrypted = load_credentials(conn, "demo")
                assert saved["configured"] is True
                assert status["demo_configured"] is True
                assert status["private_key_returned"] is False
                assert pem not in str(row[0]) and "my-test-key-id-9876" not in str(row[0])
                assert pem not in str(status)
                assert decrypted["private_key_pem"] == pem.strip()
                assert decrypted["api_key_id"] == "my-test-key-id-9876"
            finally:
                conn.close()


def test_vault_requires_valid_fernet_key_and_valid_private_key():
    from kalshi_credentials import CredentialVaultError, save_credentials
    with tempfile.TemporaryDirectory() as tmp:
        env = {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "bad-vault.sqlite3"),
               "KALSHI_CREDENTIALS_ENCRYPTION_KEY": "not-a-fernet-key"}
        with patch.dict(os.environ, env, clear=False):
            conn = connect_database()
            try:
                ensure_schema(conn)
                try:
                    save_credentials(conn, "demo", "key-id", ed25519_pem())
                except CredentialVaultError:
                    pass
                else:
                    raise AssertionError("invalid Fernet key should fail closed")
                try:
                    save_credentials(conn, "demo", "key-id", "-----BEGIN PRIVATE KEY-----\nnot-valid\n-----END PRIVATE KEY-----")
                except ValueError:
                    pass
                else:
                    raise AssertionError("invalid PEM should be rejected")
            finally:
                conn.close()


def test_admin_routes_require_login_and_saved_keys_are_not_returned():
    with tempfile.TemporaryDirectory() as tmp:
        admin = "admin-test-secret-that-is-long-enough-987654321"
        env = {"DATABASE_URL": "", "BOT_DB_PATH": str(Path(tmp) / "vault-api.sqlite3"),
               "KALSHI_VAULT_ADMIN_SECRET": admin,
               "PAPER_ADMIN_API_SECRET": "",
               "KALSHI_CREDENTIALS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
               "PAPER_WORKER_ENABLED": "false", "MARKET_SCANNER_AUTO_START": "false"}
        pem = ed25519_pem()
        with patch.dict(os.environ, env, clear=False):
            with TestClient(main.app) as client:
                assert client.get("/api/kalshi-credentials").status_code == 401
                # The vault's dedicated secret must not enable the separate manual write API.
                from types import SimpleNamespace
                from fastapi import HTTPException
                try:
                    main._require_manual_api_auth(SimpleNamespace(headers={}))
                except HTTPException as exc:
                    assert exc.status_code == 403
                else:
                    raise AssertionError("vault setup must not enable the manual paper-write API")
                bad = client.post("/api/admin/login", json={"secret": "not-the-admin-secret"})
                assert bad.status_code == 401
                oversized_login = client.post("/api/admin/login", content='{"secret":"' + ('x' * 3000) + '"}', headers={"content-type":"application/json"})
                assert oversized_login.status_code == 413
                login = client.post("/api/admin/login", json={"secret": admin})
                assert login.status_code == 200
                cookie = login.cookies.get("btc_admin_session")
                assert cookie and "httponly" in login.headers.get("set-cookie", "").lower()
                assert "samesite=strict" in login.headers.get("set-cookie", "").lower()
                too_large = client.put("/api/kalshi-credentials", json={"environment": "demo", "api_key_id": "example-demo-key-1234", "private_key_pem": "x" * 21000})
                assert too_large.status_code == 413
                saved = client.put("/api/kalshi-credentials", json={"environment": "demo", "api_key_id": "example-demo-key-1234", "private_key_pem": pem})
                assert saved.status_code == 200, saved.text
                assert saved.json()["key_id_hint"].startswith("…1234")
                status = client.get("/api/kalshi-credentials").json()
                assert status["demo_configured"] is True and status["production_configured"] is False
                assert pem not in str(status) and "example-demo-key-1234" not in str(status)
                from main import db_connect
                with db_connect() as conn:
                    encrypted = conn.execute("SELECT encrypted_payload FROM kalshi_credentials WHERE environment='demo'").fetchone()[0]
                assert pem not in encrypted and "example-demo-key-1234" not in encrypted
                logout = client.post("/api/admin/logout", json={})
                assert logout.status_code == 200
                assert client.get("/api/kalshi-credentials").status_code == 401


def test_readonly_credential_verification_signs_request_and_never_places_orders():
    from kalshi_credentials import verify_credentials_read_only
    pem = ed25519_pem()
    fake_response = Mock(status_code=200)
    fake_response.json.return_value = {"balance": 1234, "portfolio_value": 1333, "private": "do-not-return"}
    with patch("kalshi_credentials.requests.get", return_value=fake_response) as get:
        result = verify_credentials_read_only({"environment": "demo", "api_key_id": "demo-key-1234", "private_key_pem": pem})
    assert result["verified"] is True
    assert result["balance_cents"] == 1234
    assert result["orders_placed"] is False
    assert "private" not in str(result) and pem not in str(result)
    args, kwargs = get.call_args
    assert args[0] == "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/balance"
    assert kwargs["headers"]["KALSHI-ACCESS-KEY"] == "demo-key-1234"
    assert kwargs["headers"]["KALSHI-ACCESS-SIGNATURE"]


def test_default_public_kalshi_host_matches_current_documented_endpoint():
    import kalshi_api
    if "KALSHI_BASE_URL" not in os.environ:
        assert kalshi_api.BASE_URL == "https://external-api.kalshi.com/trade-api/v2"
