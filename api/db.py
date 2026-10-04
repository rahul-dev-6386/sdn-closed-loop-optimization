"""Persistence: SQLite as the source of truth, MongoDB as an optional mirror.

Why SQLite first: the spec calls for local SQLite for experiment history, it
needs no server, and it keeps ``make``-driven runs self-contained. Mongo is
opt-in via ``MONGO_URL``; when it is unset the app never touches it, and when it
is set but unreachable the app logs and degrades rather than failing requests.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

LOGGER = logging.getLogger("api.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

-- One row per issued refresh token so an individual session can be revoked.
CREATE TABLE IF NOT EXISTS refresh_tokens (
    jti        TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT    NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id);

CREATE TABLE IF NOT EXISTS experiments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT    NOT NULL,
    mode        TEXT    NOT NULL,
    scenario    TEXT    NOT NULL,
    repetition  INTEGER NOT NULL,
    seed        INTEGER,
    metrics     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE(run_id, mode, scenario, repetition)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    wall_ts    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
"""


def _now() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thin SQLite wrapper with a per-thread connection.

    ``sqlite3`` connections are not shareable across threads, and FastAPI runs
    sync endpoints in a threadpool, so each thread gets its own connection.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
        LOGGER.info("sqlite ready at %s", path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a thread-local connection configured for dict-like rows."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            self._local.conn = conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ------------------------------------------------------------- users --
    def create_user(self, username: str, password_hash: str) -> int:
        """Insert a user and return its id."""
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES (?, ?, ?)",
                (username, password_hash, _now()),
            )
            return int(cur.lastrowid)

    def get_user(self, username: str) -> Optional[sqlite3.Row]:
        """Look up a user by username."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()

    def count_users(self) -> int:
        """Total number of registered users."""
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    # --------------------------------------------------- refresh tokens --
    def store_refresh(self, jti: str, user_id: int, expires_at: str) -> None:
        """Record a freshly issued refresh token."""
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO refresh_tokens (jti, user_id, expires_at, "
                "revoked, created_at) VALUES (?, ?, ?, 0, ?)",
                (jti, user_id, expires_at, _now()),
            )

    def is_refresh_active(self, jti: str) -> bool:
        """True if the token exists, is not revoked, and has not expired."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT revoked, expires_at FROM refresh_tokens WHERE jti = ?",
                (jti,),
            ).fetchone()
        if row is None or int(row["revoked"]):
            return False
        return _parse_iso(row["expires_at"]) > datetime.now(timezone.utc)

    def revoke_refresh(self, jti: str) -> bool:
        """Revoke one refresh token; returns True if a row was updated."""
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                "UPDATE refresh_tokens SET revoked = 1 WHERE jti = ?", (jti,)
            )
            return cur.rowcount > 0

    def revoke_all_for_user(self, user_id: int) -> int:
        """Revoke every refresh token for a user (used on password change)."""
        with self._write_lock, self.connect() as conn:
            cur = conn.execute(
                "UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?",
                (user_id,),
            )
            return cur.rowcount

    # ------------------------------------------------------- experiments --
    def record_experiment(self, run_id: str, mode: str, scenario: str,
                          repetition: int, metrics: Dict[str, Any],
                          seed: Optional[int] = None) -> None:
        """Insert one experiment result, ignoring exact duplicates."""
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO experiments "
                "(run_id, mode, scenario, repetition, seed, metrics, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, mode, scenario, repetition, seed,
                 json.dumps(metrics), _now()),
            )

    def list_experiments(self, limit: int = 100,
                         mode: Optional[str] = None) -> List[Dict[str, Any]]:
        """Recent experiment rows, newest first, optionally filtered by mode."""
        query = "SELECT * FROM experiments"
        params: List[Any] = []
        if mode:
            query += " WHERE mode = ?"
            params.append(mode)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["metrics"] = json.loads(item["metrics"])
            except json.JSONDecodeError:
                pass
            out.append(item)
        return out

    def experiment_summary(self) -> Dict[str, Any]:
        """Per-mode count and mean of the headline throughput metric."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT mode, metrics FROM experiments ORDER BY id DESC LIMIT 2000"
            ).fetchall()
        grouped: Dict[str, List[float]] = {}
        for row in rows:
            try:
                value = float(json.loads(row["metrics"]).get("throughput_mbps", 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            grouped.setdefault(row["mode"], []).append(value)
        return {
            mode: {
                "runs": len(values),
                "mean_throughput_mbps": round(sum(values) / len(values), 3),
            }
            for mode, values in grouped.items()
        }

    # ----------------------------------------------------------- events --
    def record_event(self, kind: str, payload: Dict[str, Any],
                     wall_ts: float) -> None:
        """Append an event for later dashboard replay."""
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO events (kind, payload, wall_ts) VALUES (?, ?, ?)",
                (kind, json.dumps(payload, default=str), wall_ts),
            )

    def recent_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Most recent events, newest first."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item["payload"])
            except json.JSONDecodeError:
                pass
            out.append(item)
        return out


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string into an aware datetime (UTC assumed)."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Optional MongoDB mirror
# --------------------------------------------------------------------------- #
class MongoMirror:
    """Best-effort event mirror; never lets a Mongo fault break a request."""

    def __init__(self, url: str, db_name: str) -> None:
        self.url = url
        self.db_name = db_name
        self.client = None
        self.available = False
        self._tried = False

    def connect(self) -> bool:
        """Attempt a connection once; cache the outcome."""
        if self._tried:
            return self.available
        self._tried = True
        if not self.url:
            return False
        try:
            from pymongo import MongoClient  # type: ignore

            self.client = MongoClient(self.url, serverSelectionTimeoutMS=1500)
            self.client.admin.command("ping")
            self.available = True
            LOGGER.info("mongo mirror connected (%s)", self.db_name)
        except Exception as exc:
            LOGGER.warning(
                "mongo mirror unavailable (%s); continuing with SQLite only", exc
            )
            self.available = False
        return self.available

    def push_event(self, kind: str, payload: Dict[str, Any]) -> None:
        """Mirror one event into Mongo if connected."""
        if not self.connect():
            return
        try:
            self.client[self.db_name]["events"].insert_one(
                {"kind": kind, "payload": payload, "at": _now()}
            )
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("mongo insert failed: %s", exc)