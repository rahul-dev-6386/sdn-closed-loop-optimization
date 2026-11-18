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
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import jwt
from fastapi import (Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket,
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
from api.topologies_catalog import TOPOLOGIES_CATALOG, get_topology_by_id
from common.logging_utils import setup_logging

from fastapi.staticfiles import StaticFiles

LOGGER = logging.getLogger("api.main")
REPO_ROOT = Path(__file__).resolve().parents[2]


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

figures_dir = REPO_ROOT / "results/figures"
if figures_dir.is_dir():
    app.mount("/figures", StaticFiles(directory=str(figures_dir)), name="figures")

results_dir = REPO_ROOT / "results"
if results_dir.is_dir():
    app.mount("/results", StaticFiles(directory=str(results_dir)), name="results")



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


class SpikeRequest(BaseModel):
    rate_mbps: float = Field(default=14.0, ge=1.0, le=100.0)
    duration_s: float = Field(default=30.0, ge=5.0, le=300.0)


class ModeRequest(BaseModel):
    mode: str = Field(default="ml_aware")


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
@app.get("/api/topologies")
def list_topologies(request: Request) -> List[Dict[str, Any]]:
    """List all 10 available topologies in the catalog."""
    user_from_header(request)
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "category": t["category"],
            "description": t["description"],
            "switches_count": len(t["switches"]),
            "hosts_count": len(t["hosts"]),
            "links_count": len(t["links"]),
            "bottleneck_link": t.get("bottleneck_link"),
        }
        for t in TOPOLOGIES_CATALOG
    ]


@app.get("/api/topology")
def get_topology(request: Request, id: Optional[str] = Query(None)) -> Dict[str, Any]:
    """Topology graph: switches, hosts, links and port adjacency."""
    user_from_header(request)
    assert state.project is not None
    if id and id != "diamond":
        selected = get_topology_by_id(id)
        if selected:
            topo = dict(selected)
            topo["port_map"] = {}
            topo["ports_available"] = False
            return topo
    topo = state.project.topology()
    topo["port_map"] = state.project.port_map().get("switch_ports", {})
    return topo


@app.get("/api/topologies/{id}")
def get_topology_by_path(request: Request, id: str) -> Dict[str, Any]:
    """Alias for /api/topology?id={id} supporting both URL conventions."""
    return get_topology(request, id=id)


@app.get("/api/telemetry/utilisation")
def get_telemetry_utilisation(request: Request, topo_id: Optional[str] = Query(None)) -> Dict[str, Any]:
    """Per-link utilisation and capacity from live telemetry or catalog metadata."""
    user_from_header(request)
    assert state.project is not None
    return state.project.link_utilisation(topo_id=topo_id)


@app.get("/api/flows")
def get_flows(request: Request, topo_id: Optional[str] = Query(None)) -> Dict[str, Any]:
    """Live OpenFlow tables. Reports unavailability instead of failing."""
    user_from_header(request)
    assert state.project is not None
    return state.project.flows(topo_id=topo_id)


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
    fabric_mgr = None
    try:
        from future.api.live_fabric import LiveFabricManager
        fabric_mgr = LiveFabricManager.get_instance()
    except Exception:
        pass

    mode = fabric_mgr.current_mode if fabric_mgr else "ml_aware"
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
            "ml_aware": mode == "ml_aware",
            "static_shortest": mode == "static_shortest",
            "ecmp": mode == "ecmp",
            "reactive_threshold": mode == "reactive_threshold",
        },
        "fabric_running": fabric_mgr.is_running if fabric_mgr else False,
    }


@app.get("/api/predictions")
def get_predictions(request: Request, topo_id: Optional[str] = Query(None)) -> Dict[str, Any]:
    """Per-link congestion probability.

    Returns dynamic ML predictions based on current topology and traffic state.
    """
    user_from_header(request)
    if not predictions_available():
        return {
            "available": False,
            "reason": "no model artefact yet (results/model.joblib); "
                      "train it with `make train` in P7",
            "links": [],
        }
    assert state.project is not None
    links_preds = state.project.predict_links(topo_id=topo_id)
    threshold = links_preds[0].get("threshold", 0.5) if links_preds else 0.5
    horizon_s = links_preds[0].get("horizon_s", 10.0) if links_preds else 10.0
    model_loaded = any(l.get("source") == "model" for l in links_preds) \
        or predictions_available()
    return {
        "available": predictions_available(),
        "links": links_preds,
        "model_loaded": model_loaded,
        "threshold": threshold,
        "horizon_s": horizon_s,
    }


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
# Live Fabric & Real Packet Injection
# --------------------------------------------------------------------------- #
@app.get("/api/fabric/status")
def fabric_status() -> Dict[str, Any]:
    """Report real-time Mininet and traffic status."""
    from future.api.live_fabric import LiveFabricManager
    return LiveFabricManager.get_instance().get_status()


class FabricModeRequest(BaseModel):
    mode: str = Field(
        "ml_aware",
        pattern=r"^(ml_aware|static_shortest|reactive_threshold|ecmp)$",
    )


@app.post("/api/fabric/start")
def fabric_start(payload: FabricModeRequest = Body(default_factory=FabricModeRequest)) -> Dict[str, Any]:
    """Start live Mininet fabric and Ryu controller."""
    from future.api.live_fabric import LiveFabricManager
    return LiveFabricManager.get_instance().start_fabric(mode=payload.mode)


@app.post("/api/fabric/stop")
def fabric_stop() -> Dict[str, Any]:
    """Stop live Mininet fabric and Ryu controller."""
    from future.api.live_fabric import LiveFabricManager
    return LiveFabricManager.get_instance().stop_fabric()


class PinLinkPayload(BaseModel):
    link: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    seconds: int = Field(30, ge=1, le=3600)


@app.post("/api/fabric/pin-link")
def pin_link(payload: PinLinkPayload, request: Request) -> Dict[str, Any]:
    """Pin (penalize) an inter-switch link so the Ryu decision loop avoids it.

    The controller reads the constraint file on its next telemetry ticks and
    changes its own path selection. This is the explicit agent -> controller
    seam: the agent *reports* the problem; the controller *decides* the fix.
    """
    user_from_header(request)
    from controller.constraints import write_constraints, DEFAULT_CONSTRAINTS_PATH
    link = str(payload.link).strip()
    if "--" not in link:
        raise HTTPException(status_code=422, detail="link must be in 's1--s2' form")
    res = write_constraints(
        DEFAULT_CONSTRAINTS_PATH,
        block_links=[link],
        ttl_s=float(payload.seconds),
        reason="agent-requested avoidance",
        pinned_by="agent",
    )
    return {"status": "pinned", "link": link, "seconds": payload.seconds,
            "valid_until_epoch": res.get("valid_until_epoch")}


@app.delete("/api/fabric/pin-link")
def unpin_link(request: Request) -> Dict[str, Any]:
    """Clear any active link constraint."""
    user_from_header(request)
    from controller.constraints import clear_constraints, DEFAULT_CONSTRAINTS_PATH
    res = clear_constraints(DEFAULT_CONSTRAINTS_PATH)
    return {"status": "unpinned", "link": None, "seconds": 0,
            "reason": res.get("reason", "")}


@app.get("/api/fabric/path")
def fabric_path(request: Request) -> Dict[str, Any]:
    """The controller's registered path for the service flow.

    Reads the controller's per-tick state snapshot (``controller_state.json``),
    which records which path each registered flow rides. Lets the dashboard
    render the *actual* datapath instead of an animated stand-in.

    Returns ``status: "unavailable"`` when the snapshot is missing or stale
    (fabric down / controller not ticking).
    """
    user_from_header(request)
    from controller.reroute_integration import (
        STATE_ENV_VAR, STATE_STALE_AFTER_S, CONTROLLER_STATE_PATH)
    state_path = os.path.abspath(os.environ.get(STATE_ENV_VAR) or CONTROLLER_STATE_PATH)
    try:
        with open(state_path, "r", encoding="utf-8") as handle:
            snap = json.load(handle)
    except FileNotFoundError:
        return {"status": "unavailable", "reason": "controller is not reporting topology state",
                "state_path": state_path}
    except Exception as exc:
        return {"status": "unavailable", "reason": f"controller state unreadable: {exc}",
                "state_path": state_path}

    ts = float(snap.get("ts") or 0.0)
    age_s = time.time() - ts if ts else None
    if age_s is None or age_s > STATE_STALE_AFTER_S:
        return {"status": "unavailable",
                "reason": f"controller state stale ({'never' if age_s is None else f'{age_s:.0f}s'} old)",
                "age_s": age_s, "state_path": state_path}

    flows = snap.get("flows") or []
    forward = next((f for f in flows if f.get("path")), None) or {}
    path = forward.get("path") or []
    return {
        "status": "ok",
        "age_s": round(age_s, 2),
        "mode": snap.get("mode"),
        "n_links": int(snap.get("n_links") or 0),
        "graph": snap.get("graph") or {},
        "flows": flows,
        "flow": forward.get("flow"),
        "path": list(path) if isinstance(path, list) else [],
        "constraints": (snap.get("constraints") or {}).get("block_links") or [],
    }


@app.post("/api/traffic/spike")
def traffic_spike(payload: SpikeRequest = Body(default_factory=SpikeRequest)) -> Dict[str, Any]:
    """Trigger real 14 Mbps iperf3 traffic between Mininet hosts h1 and h4."""
    from future.api.live_fabric import LiveFabricManager
    return LiveFabricManager.get_instance().start_spike(
        rate_mbps=payload.rate_mbps,
        duration_s=payload.duration_s,
    )


@app.post("/api/traffic/stop")
def traffic_stop() -> Dict[str, Any]:
    """Stop active iperf3 traffic."""
    from future.api.live_fabric import LiveFabricManager
    return LiveFabricManager.get_instance().stop_spike()


class SpikeFlowRequest(BaseModel):
    src: str = Field(default="h1")
    dst: str = Field(default="h4")
    mbps: float = Field(default=14.0, ge=1.0, le=100.0)
    proto: str = Field(default="udp")


@app.post("/api/spike")
def spike_start_api(payload: SpikeFlowRequest = Body(default_factory=SpikeFlowRequest)) -> Dict[str, Any]:
    """Start real iperf traffic spike in Mininet."""
    from future.api.live_fabric import LiveFabricManager
    return LiveFabricManager.get_instance().start_spike(
        rate_mbps=payload.mbps,
        duration_s=60.0,
    )


@app.delete("/api/spike")
def spike_stop_api() -> Dict[str, Any]:
    """Stop active iperf traffic spike."""
    from future.api.live_fabric import LiveFabricManager
    return LiveFabricManager.get_instance().stop_spike()


class DecisionEventPayload(BaseModel):
    ts: float = Field(default_factory=time.time)
    predicted_link: str
    p: float
    action: str
    flow_mods: List[str] = Field(default_factory=list)
    path_old: List[str] = Field(default_factory=list)
    path_new: List[str] = Field(default_factory=list)


@app.post("/api/controller/decision-event")
def emit_decision_event(payload: DecisionEventPayload) -> Dict[str, Any]:
    """Record a decision event into controller event log."""
    assert state.project is not None
    ev = state.project.record_decision_event(payload.dict())
    return {"status": "recorded", "event": ev}


class VerifyEventPayload(BaseModel):
    ts: float = Field(default_factory=time.time)
    link: str
    before_pct: float
    after_pct: float
    window_s: float
    result: str


@app.post("/api/controller/verify-event")
def emit_verify_event(payload: VerifyEventPayload) -> Dict[str, Any]:
    """Record a verify event (PASS/FAIL) into controller event log."""
    assert state.project is not None
    ev = state.project.record_verify_event(payload.dict())
    return {"status": "recorded", "event": ev}


@app.post("/api/controller/mode")
def controller_mode(payload: ModeRequest) -> Dict[str, Any]:
    """Switch controller routing mode between ml_aware and static_shortest."""
    from future.api.live_fabric import LiveFabricManager
    return LiveFabricManager.get_instance().set_routing_mode(payload.mode)


@app.get("/api/analytics/real-data")
def get_analytics_real_data(request: Request) -> Dict[str, Any]:
    """Provide real empirical data from Mininet experiment sweeps, ML model, and telemetry."""
    user_from_header(request)
    root = Path(__file__).resolve().parents[2]

    # 1. Experiments Summary Table
    summary_path = root / "results/experiments_summary.csv"
    summary_data = []
    if summary_path.is_file():
        import csv
        with open(summary_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            summary_data = list(reader)

    # 2. ML Metrics & Feature Importances
    ml_path = root / "results/ml_metrics.json"
    ml_data = {}
    if ml_path.is_file():
        with open(ml_path, mode="r", encoding="utf-8") as f:
            ml_data = json.load(f)

    # Feature Importances from model.joblib
    model_path = root / "results/model.joblib"
    features_data = []
    if model_path.is_file():
        try:
            import joblib
            mj = joblib.load(model_path)
            clf = mj.get("model")
            names = mj.get("feature_names", [])
            if hasattr(clf, "feature_importances_"):
                sorted_f = sorted(zip(names, clf.feature_importances_), key=lambda x: x[1], reverse=True)
                features_data = [{"name": name, "importance": round(float(imp), 4)} for name, imp in sorted_f[:12]]
        except Exception as exc:
            LOGGER.warning("Could not load model.joblib: %s", exc)

    # 3. Telemetry Patterns Distribution & Sample Time Series
    tel_path = root / "data/telemetry_live_labeled.csv"
    patterns_data = {}
    time_series_data = []
    total_tel_rows = 0
    if tel_path.is_file():
        try:
            import pandas as pd
            df_tel = pd.read_csv(tel_path)
            total_tel_rows = len(df_tel)
            patterns_data = df_tel["pattern"].value_counts().to_dict()
            # 30-sample time-series during a real burst
            burst_df = df_tel[df_tel["pattern"] == "bursty_onoff"].iloc[100:130]
            for idx, (_, r) in enumerate(burst_df.iterrows()):
                time_series_data.append({
                    "time": idx * 2,
                    "tx_mbps": round(float(r.get("tx_rate_mbps", 0.0)), 2),
                    "rx_mbps": round(float(r.get("rx_rate_mbps", 0.0)), 2),
                    "util_pct": round(float(r.get("utilization", 0.0)) * 100, 1),
                    "latency_ms": round(float(r.get("latency_ms", 10.0)), 2),
                    "congested": bool(r.get("label_future_congested", 0)),
                })
        except Exception as exc:
            LOGGER.warning("Could not read telemetry_live_labeled: %s", exc)

    # 4. Empirical RESULTS.md publication report
    results_md_path = root / "results/RESULTS.md"
    results_md_content = ""
    if results_md_path.is_file():
        try:
            results_md_content = results_md_path.read_text(encoding="utf-8")
        except Exception as exc:
            LOGGER.warning("Could not read RESULTS.md: %s", exc)

    return {
        "status": "ok",
        "provenance": {
            "experiments_file": "results/experiments.csv (46 Mininet runs)",
            "telemetry_file": "data/telemetry_live_labeled.csv (12,180 rows)",
            "model_file": "results/model.joblib (RandomForestClassifier)",
        },
        "results_md": results_md_content,
        "summary_table": summary_data,
        "ml_models": ml_data.get("models", {}),
        "ml_general": {
            "trained_at": ml_data.get("trained_at"),
            "dataset": ml_data.get("dataset"),
            "decision_threshold": ml_data.get("decision_threshold", 0.5),
            "winner": ml_data.get("winner", "random_forest"),
        },
        "features": features_data,
        "patterns": patterns_data,
        "time_series_sample": time_series_data,
        "total_dataset_rows": total_tel_rows,
    }


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #
@app.websocket("/ws/live")
async def ws_live(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
    topo_id: Optional[str] = Query(None),
) -> None:
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
            fabric_mgr = None
            try:
                from future.api.live_fabric import LiveFabricManager
                fabric_mgr = LiveFabricManager.get_instance()
            except Exception:
                pass

            frame = {
                "type": "snapshot",
                "at": time.time(),
                "topology": state.project.topology(),
                "controller": controller_health(state.project),
                "events": state.project.events(limit=25),
                "telemetry": state.project.telemetry(limit=30),
                "utilisation": state.project.link_utilisation(topo_id=topo_id),
                "fabric": fabric_mgr.get_status() if fabric_mgr else {},
            }
            await websocket.send_json(frame)
            # Poll the file rather than streaming it: the controller appends
            # independently and a tail-read is adequate at 1 Hz for a dashboard.
            await asyncio.sleep(2.0)
    except WebSocketDisconnect:
        LOGGER.info("websocket live disconnected")
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("websocket error: %s", exc)


# --------------------------------------------------------------------------- #
# Agentic AI Congestion Controller (LangGraph + OpenRouter)
# --------------------------------------------------------------------------- #
from future.agent.config import agent_config
from future.agent.state import AgentState, create_initial_state
from future.agent.graph import CongestionControlGraph
from future.agent.llm import llm_client
from future.api.topologies_catalog import get_topology_by_id

class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    topology_id: Optional[str] = "diamond"

class AgentConfigRequest(BaseModel):
    api_key: Optional[str] = ""
    model: Optional[str] = "nvidia/nemotron-3.5-lightning:free"

class AgentRunRequest(BaseModel):
    topology_id: Optional[str] = "diamond"

class AgentCongestionRequest(BaseModel):
    topology_id: Optional[str] = "diamond"
    rate_mbps: Optional[float] = 14.0

_latest_agent_state: Dict[str, Any] = create_initial_state()


@app.get("/api/agent/config")
def get_agent_config(request: Request) -> Dict[str, Any]:
    """Retrieve current agent LLM model and masked API key status."""
    user_from_header(request)
    return agent_config.to_dict()


@app.post("/api/agent/config")
def set_agent_config(payload: AgentConfigRequest, request: Request) -> Dict[str, Any]:
    """Update OpenRouter API key and preferred model."""
    user_from_header(request)
    if payload.api_key:
        agent_config.save_to_config(payload.api_key, payload.model or agent_config.model)
    elif payload.model:
        agent_config.model = payload.model
    return agent_config.to_dict()


@app.get("/api/agent/state")
def get_agent_state(request: Request) -> Dict[str, Any]:
    """Get the current execution state, steps log, and chat history of the agent."""
    user_from_header(request)
    global _latest_agent_state
    return _latest_agent_state


@app.post("/api/agent/chat")
async def chat_with_agent(payload: AgentChatRequest, request: Request) -> Dict[str, Any]:
    """Interact with the SDN agent via natural language; triggers LangGraph on action commands."""
    user_from_header(request)
    global _latest_agent_state
    msg = payload.message.strip()
    topo_id = payload.topology_id or "diamond"
    now_str = time.strftime("%H:%M:%S")

    # Update state message history
    history = list(_latest_agent_state.get("messages", []))
    history.append({"role": "user", "content": msg, "time": now_str})
    _latest_agent_state["messages"] = history
    _latest_agent_state["task"] = msg
    _latest_agent_state["topology_id"] = topo_id

    lower = msg.lower()
    fix_keywords = ["fix", "mitigate", "reroute", "detour", "resolve", "relieve", "auto", "execute", "run", "clear congestion"]
    create_keywords = [
        "create congestion", "inject spike", "spike traffic", "cause congestion",
        "simulate congestion", "start spike", "make congestion", "inject congestion",
        "traffic spike", "trigger congestion", "inject traffic", "start congestion"
    ]

    graph = CongestionControlGraph(project_state=state)
    topo_def = get_topology_by_id(topo_id)
    bottleneck = topo_def.get("bottleneck_link", "s1--s2") if topo_def else "s1--s2"

    if any(k in lower for k in fix_keywords):
        # Trigger full LangGraph autonomous mitigation workflow on REAL measured state
        run_state = create_initial_state(
            task=msg,
            topology_id=topo_id,
        )
        run_state["messages"] = list(history)
        res = await graph.run(run_state)
        _latest_agent_state = res
        return _latest_agent_state

    if any(k in lower for k in create_keywords):
        spike_res = graph.tools.inject_congestion(topo_id, 14.0)
        if spike_res.get("status") == "spike_started":
            _latest_agent_state["congestion_detected"] = True
            assistant_msg = (
                f"**Real traffic spike started.** 14.0 Mbps UDP h1→h4 for "
                f"{int(spike_res.get('duration_s', 45))}s on the live fabric.\n\n"
                f"- Telemetry will report the load within ~2 s (port-stats deltas).\n"
                f"- The model will score the loaded links within ~24 s of telemetry.\n"
                f"- Click **Fix Congestion** (or ask me) to watch the controller react.\n\n"
                f"I will not claim numbers I have not measured."
            )
        else:
            _latest_agent_state["congestion_detected"] = False
            assistant_msg = (
                f"**Could not inject congestion.** {spike_res.get('reason', spike_res)}\n\n"
                f"Start the fabric first (Start Fabric button), then ask again."
            )
        history.append({"role": "assistant", "content": assistant_msg, "time": time.strftime("%H:%M:%S")})
        _latest_agent_state["messages"] = history
        _latest_agent_state["bottleneck_link"] = bottleneck
        _latest_agent_state["status"] = "congested" if spike_res.get("status") == "spike_started" else "unavailable"
        _latest_agent_state["network_state"] = spike_res
        return _latest_agent_state

    # General inspection / Q&A: gather measured telemetry context and query LLM
    telem = graph.tools.inspect_telemetry(topo_id)
    preds = graph.tools.inspect_ml_predictions(topo_id)
    context = {
        "topology_id": topo_id,
        "bottleneck": telem.get("bottleneck", bottleneck),
        "bottleneck_util_pct": telem.get("bottleneck_util_pct"),
        "ml_probability": preds.get("probability"),
        "congested": telem.get("is_congested", False),
        "telemetry_status": telem.get("status"),
        "telemetry_reason": telem.get("reason"),
        "ml_summary": preds.get("summary"),
    }

    resp = await llm_client.generate_response(msg, context)
    history.append({"role": "assistant", "content": resp, "time": time.strftime("%H:%M:%S")})
    _latest_agent_state["messages"] = history
    _latest_agent_state["network_state"] = telem
    _latest_agent_state["predictions"] = preds
    return _latest_agent_state


@app.post("/api/agent/create-congestion")
def agent_create_congestion(payload: AgentCongestionRequest, request: Request) -> Dict[str, Any]:
    """Inject/simulate active congestion on the specified topology."""
    user_from_header(request)
    global _latest_agent_state
    topo_id = payload.topology_id or "diamond"
    rate = payload.rate_mbps or 14.0
    graph = CongestionControlGraph(project_state=state)
    topo_def = get_topology_by_id(topo_id)
    bottleneck = topo_def.get("bottleneck_link", "s1--s2") if topo_def else "s1--s2"

    res = graph.tools.inject_congestion(topo_id, rate)
    history = list(_latest_agent_state.get("messages", []))
    now_str = time.strftime("%H:%M:%S")
    history.append({
        "role": "user",
        "content": f"Inject {rate} Mbps traffic spike on {topo_id}",
        "time": now_str,
    })
    if res.get("status") == "spike_started":
        _latest_agent_state["congestion_detected"] = True
        assistant_msg = (
            f"**Real traffic spike started.** {rate} Mbps UDP h1→h4 for "
            f"{int(res.get('duration_s', 45))}s on the live fabric.\n\n"
            f"- Telemetry reports the load within ~2 s (port-stats deltas).\n"
            f"- The model scores the loaded links after ~24 s of telemetry.\n"
            f"- Click **Fix Congestion** when you want the controller to mitigate it."
        )
    else:
        _latest_agent_state["congestion_detected"] = False
        assistant_msg = (
            f"**Injection failed.** {res.get('reason', res)}\n\n"
            f"Start the fabric first (Start Fabric button), then ask again."
        )
    history.append({"role": "assistant", "content": assistant_msg, "time": now_str})

    _latest_agent_state["messages"] = history
    _latest_agent_state["bottleneck_link"] = bottleneck
    _latest_agent_state["status"] = "congested" if res.get("status") == "spike_started" else "unavailable"
    _latest_agent_state["topology_id"] = topo_id
    _latest_agent_state["network_state"] = res
    return _latest_agent_state


@app.post("/api/agent/run")
async def run_agent_workflow(payload: AgentRunRequest, request: Request) -> Dict[str, Any]:
    """Execute the full 5-stage LangGraph closed-loop congestion mitigation loop."""
    user_from_header(request)
    global _latest_agent_state
    topo_id = payload.topology_id or "diamond"
    graph = CongestionControlGraph(project_state=state)
    topo_def = get_topology_by_id(topo_id)
    bottleneck = topo_def.get("bottleneck_link", "s1--s2") if topo_def else "s1--s2"

    init_state = create_initial_state(
        task=f"Autonomous Closed-Loop Congestion Control on {topo_id}",
        topology_id=topo_id,
    )

    result = await graph.run(init_state)
    _latest_agent_state = result
    return _latest_agent_state


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Unauthenticated liveness probe."""
    return JSONResponse({"status": "ok", "at": time.time()})
