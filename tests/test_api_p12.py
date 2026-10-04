"""Tests for the P12 API's pure logic: password hashing and JWT handling.

These run without a server. End-to-end HTTP behaviour (status codes, revocation)
is exercised separately by ``scripts/verify_p12.sh``.
"""
from __future__ import annotations

import time

import jwt
import pytest

from api.security import (ACCESS, REFRESH, decode_token, hash_password,
                          issue_access_token, issue_refresh_token,
                          verify_password)
from api.settings import Settings

SECRET = "test-secret-not-for-production"


@pytest.fixture
def settings() -> Settings:
    return Settings(jwt_secret=SECRET, access_token_ttl_s=60,
                    refresh_token_ttl_s=3600)


# ---------------------------------------------------------------- passwords --
def test_hash_and_verify_roundtrip() -> None:
    encoded = hash_password("correct horse battery")
    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("correct horse battery", encoded) is True
    assert verify_password("wrong", encoded) is False


def test_hashes_are_salted_and_unique() -> None:
    """Two identical passwords must not produce identical hashes."""
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b


def test_empty_password_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        hash_password("")


@pytest.mark.parametrize(
    "broken",
    ["", "nonsense", "pbkdf2_sha256$notanint$aa$bb", "other$1000$aa$bb", "a$b$c"],
)
def test_malformed_hashes_verify_false_rather_than_raising(
    broken: str,
) -> None:
    """A corrupt stored hash must fail closed, not crash the login path."""
    assert verify_password("anything", broken) is False


def test_long_passphrase_is_not_silently_truncated() -> None:
    """Guards against bcrypt's 72-byte truncation footgun."""
    long_pass = "x" * 200 + "TAIL"
    assert verify_password(long_pass, hash_password(long_pass)) is True
    assert verify_password("x" * 200 + "OTHER", hash_password(long_pass)) is False


# -------------------------------------------------------------------- tokens --
def test_access_token_carries_identity_and_type(settings: Settings) -> None:
    token = issue_access_token(settings, 7, "operator")
    claims = decode_token(settings, token, expected_type=ACCESS)
    assert claims["sub"] == "7"
    assert claims["username"] == "operator"
    assert claims["typ"] == ACCESS


def test_refresh_token_omits_username(settings: Settings) -> None:
    """Refresh tokens are deliberately minimal: fewer claims, less to leak."""
    token, jti, expires_at = issue_refresh_token(settings, 7)
    claims = decode_token(settings, token, expected_type=REFRESH)
    assert claims["sub"] == "7"
    assert "username" not in claims
    assert claims["jti"] == jti
    assert expires_at is not None


def test_refresh_token_cannot_be_used_as_access(settings: Settings) -> None:
    """Token-type separation is the reason the two token kinds differ."""
    refresh, _, _ = issue_refresh_token(settings, 1)
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(settings, refresh, expected_type=ACCESS)


def test_access_token_cannot_be_used_as_refresh(settings: Settings) -> None:
    access = issue_access_token(settings, 1, "operator")
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(settings, access, expected_type=REFRESH)


def test_every_token_has_a_unique_jti(settings: Settings) -> None:
    """Unique ids are what make per-token revocation possible."""
    ids = {decode_token(settings, issue_access_token(settings, 1, "u"))["jti"]
           for _ in range(25)}
    assert len(ids) == 25


def test_token_signed_with_another_secret_is_rejected(settings: Settings) -> None:
    other = Settings(jwt_secret="a-different-secret")
    token = issue_access_token(other, 1, "operator")
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(settings, token, expected_type=ACCESS)


def test_expired_access_token_is_rejected(settings: Settings) -> None:
    expiring = Settings(jwt_secret=SECRET, access_token_ttl_s=-10)
    token = issue_access_token(expiring, 1, "operator")
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(settings, token, expected_type=ACCESS)


def test_expiry_is_in_the_future(settings: Settings) -> None:
    token = issue_access_token(settings, 1, "operator")
    assert decode_token(settings, token)["exp"] > int(time.time())


def test_garbage_token_is_rejected(settings: Settings) -> None:
    with pytest.raises(jwt.PyJWTError):
        decode_token(settings, "not.a.token", expected_type=ACCESS)


# ------------------------------------------------------------------ settings --
def test_settings_defaults_secret_is_ephemeral_when_unset() -> None:
    """An unset JWT_SECRET must not silently become a known constant."""
    unset = Settings(jwt_secret="")
    assert unset.resolved_secret() == unset.resolved_secret()
    assert len(unset.resolved_secret()) >= 32


def test_settings_exposes_mapping_get_for_logging_setup() -> None:
    """setup_logging() consumes Config-like objects; Settings must fit."""
    s = Settings()
    assert s.get("mongo_db", "sdn") == "sdn"
    assert s.get("nonexistent_key", "fallback") == "fallback"


def test_mongo_disabled_when_url_blank() -> None:
    assert Settings(mongo_url="").mongo_enabled is False
    assert Settings(mongo_url="mongodb://localhost:27017").mongo_enabled is True


def test_cors_origins_parse_to_a_list() -> None:
    s = Settings(cors_origins="http://a:1, http://b:2 ,")
    assert s.cors_origin_list == ["http://a:1", "http://b:2"]