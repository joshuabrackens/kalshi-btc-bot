from datetime import datetime, timezone, timedelta
import pytest

from multi_user_security import (
    create_session_token, hash_password, multi_user_readiness,
    session_is_valid, session_token_digest, verify_password,
)


def test_password_hash_is_salted_and_verifiable():
    first = hash_password("A-long-passphrase-123")
    second = hash_password("A-long-passphrase-123")
    assert first["salt_b64"] != second["salt_b64"]
    assert verify_password("A-long-passphrase-123", first)
    assert not verify_password("wrong-passphrase-456", first)


def test_password_rejects_short_values_and_unknown_hash_scheme():
    with pytest.raises(ValueError):
        hash_password("too-short")
    assert not verify_password("A-long-passphrase-123", {"scheme": "plaintext", "digest_b64": "x", "salt_b64": "y"})


def test_session_secret_is_random_and_only_digest_should_be_stored():
    token, digest = create_session_token()
    token2, digest2 = create_session_token()
    assert token != token2
    assert digest != token and digest == session_token_digest(token)
    assert digest != digest2


def test_session_expiry_and_revocation_fail_closed():
    now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    assert session_is_valid(expires_at=now + timedelta(minutes=10), now=now)
    assert not session_is_valid(expires_at=now, now=now)
    assert not session_is_valid(expires_at=now + timedelta(minutes=10), revoked_at=now - timedelta(seconds=1), now=now)
    assert not session_is_valid(expires_at="not-a-date", now=now)


def test_multi_user_readiness_does_not_claim_isolation_is_complete():
    status = multi_user_readiness()
    assert status["enabled"] is False
    assert status["production_ready"] is False
    assert status["public_signup_enabled"] is False
    assert any(item["key"] == "tenant_scoping" and item["status"] == "blocked" for item in status["checks"])


def test_password_hashing_bounds_untrusted_inputs_and_requires_strong_salt():
    from multi_user_security import MAX_PASSWORD_LENGTH, MIN_SALT_LENGTH

    with pytest.raises(ValueError):
        hash_password("A" * (MAX_PASSWORD_LENGTH + 1))
    with pytest.raises(ValueError):
        hash_password("A-long-passphrase-123", salt=b"weak")
    record = hash_password("A-long-passphrase-123")
    assert not verify_password("A" * (MAX_PASSWORD_LENGTH + 1), record)
    assert not verify_password("A-long-passphrase-123", {"scheme": "scrypt", "salt_b64": "YWJj", "digest_b64": record["digest_b64"]})


def test_malformed_password_records_fail_closed_without_raising():
    good = hash_password("A-long-passphrase-123")
    for record in (None, [], "not-a-record", 123, {}, {"scheme": "scrypt"},
                   {"scheme": "scrypt", "salt_b64": "!!!!", "digest_b64": good["digest_b64"]},
                   {"scheme": "scrypt", "salt_b64": good["salt_b64"], "digest_b64": "Z" * 100000}):
        assert not verify_password("A-long-passphrase-123", record)


def test_password_hash_rejects_overlong_supplied_salt_and_verifier_rejects_overlong_stored_salt():
    from multi_user_security import MAX_SALT_LENGTH

    with pytest.raises(ValueError):
        hash_password("A-long-passphrase-123", salt=b"s" * (MAX_SALT_LENGTH + 1))
    good = hash_password("A-long-passphrase-123")
    good["salt_b64"] = __import__("base64").urlsafe_b64encode(b"x" * (MAX_SALT_LENGTH + 1)).decode("ascii")
    assert not verify_password("A-long-passphrase-123", good)


def test_any_populated_revocation_field_invalidates_session_even_if_timestamp_is_future():
    now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    future_revocation = now + timedelta(days=2)
    assert not session_is_valid(expires_at=now + timedelta(days=5), revoked_at=future_revocation, now=now)
