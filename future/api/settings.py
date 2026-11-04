"""Runtime settings for the P12 API.

All secrets come from the environment (or a local ``.env``), never from code.
``.env`` is git-ignored; ``.env.example`` documents what is needed.
"""
from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Any
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Environment-driven configuration.

    Attributes
    ----------
    jwt_secret:
        HMAC key for access/refresh tokens. Generated per-process when unset,
        which invalidates tokens on restart -- correct for local dev, and a
        loud reminder that a real deployment must set it.
    mongo_url:
        Optional MongoDB connection string. When empty, the app runs
        SQLite-only. Events are mirrored to Mongo when this is set.
    """

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- auth -------------------------------------------------------------
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_ttl_s: int = 15 * 60          # short-lived access token
    refresh_token_ttl_s: int = 7 * 24 * 3600   # long-lived refresh token

    # --- server -----------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- storage ----------------------------------------------------------
    sqlite_path: str = str(REPO_ROOT / "data" / "sdn.db")
    mongo_url: str = ""
    mongo_db: str = "sdn"

    # --- project data sources --------------------------------------------
    config_path: str = str(REPO_ROOT / "config.yaml")
    port_map_path: str = str(REPO_ROOT / "data" / "portmap.json")
    events_path: str = str(REPO_ROOT / "data" / "logs" / "events.jsonl")

    @property
    def mongo_enabled(self) -> bool:
        """True when a MongoDB URL was supplied."""
        return bool(self.mongo_url.strip())

    def get(self, key: str, default: Any = None) -> Any:
        """Mapping-style read, so ``Settings`` can stand in for a ``Config``.

        ``common.logging_utils.setup_logging`` expects ``.get(key, default)``,
        which pydantic models deliberately do not provide. This shim lets the
        API reuse the project's logging setup without duplicating it.
        """
        return getattr(self, key, default)

    @property
    def cors_origin_list(self) -> list:
        """Parsed ``cors_origins`` as a list."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def resolved_secret(self) -> str:
        """Return the JWT secret, generating an ephemeral one if unset."""
        if self.jwt_secret:
            return self.jwt_secret
        return _EPHEMERAL_SECRET


#: Process-lifetime fallback so the app still runs without a configured secret.
_EPHEMERAL_SECRET = secrets.token_urlsafe(48)


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor (FastAPI dependency)."""
    return Settings()