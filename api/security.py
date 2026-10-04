"""Password hashing and JWT issuing/verification.

Two token types are issued, deliberately distinct:

* **access** -- short TTL, sent on every request, carries the user id.
* **refresh** -- long TTL, *only* usable on ``/api/auth/refresh``, carries a
  ``jti`` so an individual token can be revoked server-side.

Keeping them separate in claim space means a stolen access token cannot be used
to mint new access tokens, and a stolen refresh token is revocable.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt

from api.settings import Settings

ACCESS = "access"
REFRESH = "refresh"


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str, *, iterations: int = 240_000) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256.

    Stored as ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`` -- the format
    ``passlib`` produces, so these hashes stay compatible if passlib is adopted
    later.

    Note: ``bcrypt`` is installed in this venv but is deliberately not used here:
    it truncates silently at 72 bytes, which is a real footgun for long
    passphrases.
    """
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verification of a password against a stored hash."""
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
    except (ValueError, AttributeError):
        return False
    # hmac.compare_digest avoids leaking the match position via timing.
    return hmac.compare_digest(expected.hex(), digest_hex)


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
def _encode(settings: Settings, claims: Dict[str, Any], ttl_s: int,
            token_type: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        **claims,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_s)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.resolved_secret(),
                      algorithm=settings.jwt_algorithm)


def issue_access_token(settings: Settings, user_id: int,
                       username: str) -> str:
    """Mint a short-lived access token."""
    return _encode(
        settings,
        {"sub": str(user_id), "username": username},
        settings.access_token_ttl_s,
        ACCESS,
    )


def issue_refresh_token(settings: Settings, user_id: int) -> tuple:
    """Mint a refresh token; returns ``(token, jti, expires_at)``."""
    token = _encode(
        settings,
        {"sub": str(user_id)},
        settings.refresh_token_ttl_s,
        REFRESH,
    )
    jti = jwt.decode(token, settings.resolved_secret(),
                     algorithms=[settings.jwt_algorithm])["jti"]
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=settings.refresh_token_ttl_s
    )
    return token, jti, expires_at


def decode_token(settings: Settings, token: str,
                 expected_type: Optional[str] = None) -> Dict[str, Any]:
    """Decode and validate a token.

    Raises
    ------
    jwt.PyJWTError
        On expiry, bad signature, or wrong token type.
    """
    payload = jwt.decode(
        token, settings.resolved_secret(), algorithms=[settings.jwt_algorithm]
    )
    if expected_type and payload.get("typ") != expected_type:
        raise jwt.InvalidTokenError(
            f"expected a {expected_type} token, got {payload.get('typ')!r}"
        )
    return payload