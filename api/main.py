"""P12 API: REST + WebSocket over the live SDN control plane.

Authentication is JWT bearer with short-lived access tokens and long-lived,
individually revocable refresh tokens. Every ``/api/*`` route below
``/api/auth`` requires a valid access token.

Live updates use a WebSocket at ``/ws/live``. Browsers cannot attach an
``Authorization`` header to a WebSocket handshake, so the access token is passed
as a ``?token=`` query parameter; it is verified with the same code path and the
socket is closed with code 4401 if it is missing or invalid.

Run with::

    .venv/bin/uvicorn api.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import jwt
from fastapi import (Depends, FastAPI, HTTPException, Query, Request, WebSocket,
                     WebSocketDisconnect, status)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.db import Database, MongoMirror
from api.sdn import ProjectData, controller_health, predictions_available
from api.security import (ACCESS, REFRESH, decode_token, hash_password,
                          issue_access_token, issue_refresh_token,
                          verify_password)
from api.settings import Settings, get_settings
from common.logging_utils import setup_logging

LOGGER = logging.getLogger("api.main")


# --------------------------------------------------------------------------- #
# App state
# --------------------------------------------------------------------------- #
class AppState:
    """Process-wide singletons, populated in the lifespan hook."""

    def __init__(self) -> None:
        self.settings: Optional[Settings] = None
        self.db: Optional[Database] = None
        self.mongo: Optional[MongoMirror] = None
        self.project: Optional[ProjectData] = None

    def record_event(self, kind: str, payload: Dict[str, Any]) -> None:
        """Persist an event to SQLite and mirror it to Mongo when enabled."""
        if self.db is not None:
            self.db.record_event(kind, payload, time.time())
        if self.mongo is not None:
            self.mongo.push_event(kind, payload)


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise and tear down shared resources."""
    settings = get_settings()
    setup_logging(settings, name="api", force=True)
    state.settings = settings
    state.db = Database(settings.sqlite_path)
    state.mongo = MongoMirror(settings.mongo_url, settings.mongo_db)
    state.project = ProjectData(
        settings.config_path, settings.port_map_path, settings.events_path
    )
    if not settings.jwt_secret:
        LOGGER.warning(
            "JWT_SECRET is unset -- using an ephemeral secret, so all tokens "
            "are invalidated on restart. Set it in .env for a stable session."
        )
    if not settings.mongo_enabled:
        LOGGER.info("MONGO_URL unset -- running SQLite-only")
    yield
    if state.mongo is not None and state.mongo.client is not None:
        try:
            state.mongo.client.close()
        except Exception:  # pragma: no cover
            pass


app = FastAPI(
    title="AI-Driven SDN Control Plane API",
    version="0.12.0",
    description=(
        "Topology, flow tables, controller events and experiment history for "
        "the closed-loop SDN optimiser."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class Credentials(BaseModel):
    """Username/password pair."""

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)


class TokenPair(BaseModel):
    """An access/refresh token pair."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(BaseModel):
    """Public user projection (never includes the hash)."""

    id: int
    username: str


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
def require_user(authorization: Optional[str] = Query(
        None, description="Bearer access token")) -> Dict[str, Any]:
    """Validate the ``access_token`` query parameter or Authorization header.

    FastAPI cannot inject a header and a query parameter into one dependency
    cleanly, so this reads the query parameter; :func:`require_user_header` is
    the header-based variant used by REST routes.
    """
    return _resolve_user(authorization)


def _resolve_user(token: Optional[str]) -> Dict[str, Any]:
    """Decode an access token into its claims, or raise 401."""
    settings = state.settings or get_settings()
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing access token")
    raw = token[7:] if token.lower().startswith("bearer ") else token
    try:
        claims = decode_token(settings, raw, expected_type=ACCESS)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "access token expired")
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}")
    return claims


def user_from_header(request: Request) -> Dict[str, Any]:
    """Validate the ``Authorization: Bearer`` header."""
    header = request.headers.get("Authorization")
    if not header:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing Authorization")
    return _resolve_user(header)


# --------------------------------------------------------------------------- #
# Auth routes
# --------------------------------------------------------------------------- #
@app.post("/api/auth/register", response_model=TokenPair,
          status_code=status.HTTP_201_CREATED)
def register(creds: Credentials) -> TokenPair:
    """Create an account and issue a token pair."""
    assert state.db is not None and state.settings is not None
    if state.db.get_user(creds.username) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"username {creds.username!r} is taken")
    try:
        user_id = state.db.create_user(
            creds.username, hash_password(creds.password)
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    access = issue_access_token(state.settings, user_id, creds.username)
    refresh, jti, expires_at = issue_refresh_token(state.settings, user_id)
    state.db.store_refresh(jti, user_id, expires_at.isoformat())
    state.record_event("user_registered", {"username": creds.username})
    LOGGER.info("registered user %s (id=%d)", creds.username, user_id)
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=state.settings.access_token_ttl_s,
    )


@app.post("/api/auth/login", response_model=TokenPair)
def login(creds: Credentials) -> TokenPair:
    """Exchange credentials for a token pair."""
    assert state.db is not None and state.settings is not None
    user = state.db.get_user(creds.username)
    # Verify against a dummy hash when the user is absent so the response time
    # does not reveal whether the username exists.
    stored = user["password_hash"] if user else hash_password("no-such-user")
    if not verify_password(creds.password, stored) or user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "incorrect username or password")
    access = issue_access_token(state.settings, user["id"], user["username"])
    refresh, jti, expires_at = issue_refresh_token(state.settings, user["id"])
    state.db.store_refresh(jti, user["id"], expires_at.isoformat())
    state.record_event("user_login", {"username": user["username"]})
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=state.settings.access_token_ttl_s,
    )


class RefreshBody(BaseModel):
    """Refresh token exchange body."""

    refresh_token: str


@app.post("/api/auth/refresh", response_model=TokenPair)
def refresh(body: RefreshBody) -> TokenPair:
    """Mint a new access token, rotating nothing (the old one still works).

    The presented refresh token must be active server-side; revocation takes
    effect immediately.
    """
    assert state.db is not None and state.settings is not None
    try:
        claims = decode_token(state.settings, body.refresh_token,
                              expected_type=REFRESH)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh token expired")
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}")

    jti = claims["jti"]
    if not state.db.is_refresh_active(jti):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh token revoked")

    user_id = int(claims["sub"])
    user = state.db.get_user(
        next((u for u in [claims.get("username")] if u), "")
    ) if claims.get("username") else None
    username = user["username"] if user else claims.get("username", "")
    access = issue_access_token(state.settings, user_id, username)
    LOGGER.info("refreshed access token for user %d", user_id)
    return TokenPair(access_token=access,
                     refresh_token=body.refresh_token,
                     expires_in=state.settings.access_token_ttl_s)


class LogoutBody(BaseModel):
    """Logout body."""

    refresh_token: str


@app.post("/api/auth/logout")
def logout(body: LogoutBody) -> Dict[str, Any]:
    """Revoke the presented refresh token.

    Returns 200 rather than 204: FastAPI rejects a 204 route that declares a
    response body, and an explicit result keeps the logout idempotent and
    observable from the dashboard.
    """
    assert state.db is not None and state.settings is not None
    revoked = False
    try:
        claims = decode_token(state.settings, body.refresh_token,
                              expected_type=REFRESH)
        revoked = state.db.revoke_refresh(claims["jti"])
        if revoked:
            LOGGER.info("revoked refresh token for user %s", claims.get("sub"))
    except jwt.PyJWTError:
        # Idempotent: an unusable token is already effectively logged out.
        revoked = False
    return {"revoked": revoked}


@app.get("/api/auth/me", response_model=UserOut)
def me(request: Request) -> UserOut:
    """Return the authenticated user."""
    claims = user_from_header(request)
    return UserOut(id=int(claims["sub"]), username=claims["username"])


# --------------------------------------------------------------------------- #
# Protected SDN routes
# --------------------------------------------------------------------------- #
@app.get("/api/topology")
def get_topology(request: Request) -> Dict[str, Any]:
    """Topology graph: switches, hosts, links and port adjacency."""
    user_from_header(request)
    assert state.project is not None
    topo = state.project.topology()
    topo["port_map"] = state.project.port_map().get("switch_ports", {})
    return topo


@app.get("/api/flows")
def get_flows(request: Request) -> Dict[str, Any]:
    """Live OpenFlow tables. Reports unavailability instead of failing."""
    user_from_header(request)
    assert state.project is not None
    return state.project.flows()


@app.get("/api/events")
def get_events(request: Request, limit: int = Query(100, ge=1, le=1000)) -> List[Dict[str, Any]]:
    """Tail of the controller event stream."""
    user_from_header(request)
    assert state.project is not None
    return state.project.events(limit=limit)


@app.get("/api/status")
def get_status(request: Request) -> Dict[str, Any]:
    """Controller liveness plus which later phases have delivered artefacts."""
    user_from_header(request)
    assert state.project is not None and state.settings is not None
    return {
        "controller": controller_health(state.project),
        "mongo_enabled": state.settings.mongo_enabled,
        "predictions_available": predictions_available(),
        "phases": {
            "P3_controller": True,
            "P4_discovery": (state.project.config().get("discovery") is not None
                             and state.project.port_map_path.is_file()),
            "P5_telemetry": (state.project.config_path.is_file()
                             and state.project.config().get("telemetry") is not None
                             and state.project.config().get("_telemetry_running", False)),
            "P7_model": predictions_available(),
        },
    }


@app.get("/api/predictions")
def get_predictions(request: Request) -> Dict[str, Any]:
    """Per-link congestion probability.

    Empty until P7 trains a model. Returns an explicit ``available: false`` so
    the dashboard can say *why* rather than rendering an empty chart.
    """
    user_from_header(request)
    if not predictions_available():
        return {
            "available": False,
            "reason": "no model artefact yet (results/model.joblib); "
                      "train it with `make train` in P7",
            "links": [],
        }
    return {"available": True, "links": []}


@app.get("/api/experiments")
def get_experiments(request: Request, limit: int = Query(100, ge=1, le=1000),
                    mode: Optional[str] = None) -> Dict[str, Any]:
    """Experiment history from SQLite, with a per-mode summary."""
    user_from_header(request)
    assert state.db is not None
    return {
        "rows": state.db.list_experiments(limit=limit, mode=mode),
        "summary": state.db.experiment_summary(),
    }


@app.post("/api/experiments", status_code=status.HTTP_201_CREATED)
def post_experiment(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Record one experiment result (used by P10's runner)."""
    user_from_header(request)
    assert state.db is not None
    required = ("run_id", "mode", "scenario", "repetition")
    missing = [k for k in required if k not in payload]
    if missing:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"missing fields: {missing}")
    state.db.record_experiment(
        payload["run_id"], payload["mode"], payload["scenario"],
        int(payload["repetition"]), payload.get("metrics", {}),
        payload.get("seed"),
    )
    return {"recorded": True}


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #
@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket, token: Optional[str] = Query(None)) -> None:
    """Push topology + controller state to an authenticated dashboard.

    The access token arrives as a query parameter because the WebSocket API
    cannot set request headers from a browser. An unauthenticated socket is
    closed with 4401 rather than upgraded.
    """
    settings = state.settings or get_settings()
    try:
        decode_token(settings, token or "", expected_type=ACCESS)
    except jwt.PyJWTError:
        await websocket.close(code=4401, reason="invalid or missing token")
        return

    await websocket.accept()
    LOGGER.info("websocket live connected")
    assert state.project is not None
    try:
        while True:
            frame = {
                "type": "snapshot",
                "at": time.time(),
                "topology": state.project.topology(),
                "controller": controller_health(state.project),
                "events": state.project.events(limit=25),
            }
            await websocket.send_json(frame)
            # Poll the file rather than streaming it: the controller appends
            # independently and a tail-read is adequate at 1 Hz for a dashboard.
            await asyncio.sleep(2.0)
    except WebSocketDisconnect:
        LOGGER.info("websocket live disconnected")
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("websocket error: %s", exc)


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Unauthenticated liveness probe."""
    return JSONResponse({"status": "ok", "at": time.time()})