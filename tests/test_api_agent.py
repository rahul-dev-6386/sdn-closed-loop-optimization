"""Integration tests for the Agentic AI FastAPI endpoints — honest contract.

The tests attach a *deterministic stub fabric* to the shared app state so the
agent reads real-shaped measurements it controls, instead of the old fake
constants. This tests the actual agent logic: measurement -> gating -> action
-> verification, with no fabricated numbers anywhere.
"""
import pytest
from fastapi.testclient import TestClient
from future.api.main import app, state
from future.api.security import issue_access_token
from future.api.settings import get_settings

def stub_controller_state(monkeypatch, tmp_path, mode="ml_aware", flows=None):
    """Mirror the reroute loop's snapshot publication for the test fabric."""
    import json as _json, time as _time
    if flows is None:
        flows = [{"flow": "s1->s4:00:00:00:00:00:02", "path": ["s1", "s2", "s4"]}]
    payload = {
        "ts": _time.time(),
        "mode": mode,
        "graph": {"s1": ["s2", "s3"], "s2": ["s1", "s4"], "s3": ["s1", "s4"], "s4": ["s2", "s3"]},
        "links": [{"a": "s1", "b": "s2"}, {"a": "s1", "b": "s3"},
                  {"a": "s2", "b": "s4"}, {"a": "s3", "b": "s4"}],
        "n_links": 4,
        "flows": flows,
        "constraints": {"block_links": [], "valid_until_epoch": 0, "reason": ""},
    }
    path = tmp_path / "controller_state.json"
    path.write_text(_json.dumps(payload))
    monkeypatch.setenv("SDN_CONTROLLER_STATE", str(path))
    return str(path)

LINKS = ("s1--s2", "s1--s3", "s2--s4", "s3--s4")
BOTTLENECK_LINKS = ("s1--s2", "s2--s4")


class StubProject:
    """Deterministic stand-in for ProjectData: a fabric with known load state."""

    def __init__(self):
        self.saturated = False  # 100% on the bottleneck path while a spike runs

    def _pin_moves_flow(self):
        """Mirror the live controller: a valid page constraint moves the flow."""
        try:
            from controller.constraints import read_constraints, blocked_pairs
            cons = read_constraints()
            blocked = {tuple(sorted(pair)) for pair in blocked_pairs(cons) if pair}
            return any(tuple(sorted(b.split("--"))) in blocked for b in BOTTLENECK_LINKS)
        except Exception:
            return False

    def link_utilisation(self, topo_id=None):
        moved = self._pin_moves_flow()
        links = {}
        for key in LINKS:
            on_bottleneck = key in BOTTLENECK_LINKS
            if moved:
                saturated = not on_bottleneck
            else:
                saturated = self.saturated and on_bottleneck
            util = 100.0 if saturated else 0.0
            src, dst = key.split("--")
            links[key] = {
                "key": key, "src": src, "dst": dst,
                "capacity_mbps": 10.0,
                "used_mbps": round(10.002 if saturated else 0.0005, 4),
                "util_pct": util,
                "is_stale": False,
                "source": "stub",
            }
        return {"available": True, "topo_id": topo_id or "diamond",
                "timestamp": 1700000000.0, "links": links}

    def predict_links(self, topo_id=None):
        probs = {"s1--s2": 0.9, "s2--s4": 0.8} if self.saturated else {}
        out = []
        for key in LINKS:
            p = float(probs.get(key, 0.0))
            out.append({"link": key, "probability": p,
                        "congested": p >= 0.5, "threshold": 0.5,
                        "horizon_s": 10.0, "reason": None,
                        "source": "stub", "direction": "forward"})
        return out

    def record_decision_event(self, data):
        return {"kind": "stub_decision", "recorded": True}

    def record_verify_event(self, data):
        return {"kind": "stub_verify", "recorded": True}


class StubFabric:
    """Deterministic stand-in for LiveFabricManager."""

    def __init__(self, project):
        self.project = project
        self.is_running = True
        self.spike_active = False
        self.current_mode = "static_shortest"
        self.spike_duration_s = 45.0

    def start_spike(self, rate_mbps=14.0, duration_s=45.0):
        self.project.saturated = True
        self.spike_active = True
        return {"status": "spike_started", "rate_mbps": rate_mbps,
                "duration_s": duration_s, "src": "h1 (10.0.0.1)",
                "dst": "h4 (10.0.0.4)"}

    def set_routing_mode(self, mode):
        self.current_mode = mode
        self.project.saturated = False  # controller moves the flow off the bottleneck
        self.spike_active = False
        return {"status": "mode_updated", "mode": mode}

    def get_status(self):
        return {"fabric_running": True, "routing_mode": self.current_mode,
                "spike_active": self.spike_active,
                "spike_rate_mbps": 14.0 if self.spike_active else 0.0,
                "spike_elapsed_s": 10.0 if self.spike_active else 0.0,
                "spike_duration_s": self.spike_duration_s}

    def is_congestion_injected(self):
        return self.project.saturated


@pytest.fixture
def stub_fabric(monkeypatch, tmp_path):
    project = StubProject()
    stub = StubFabric(project)
    stub_controller_state(monkeypatch, tmp_path, mode="ml_aware")
    monkeypatch.setattr("future.api.live_fabric.LiveFabricManager.get_instance", lambda: stub)
    state.project = project
    yield project, stub
    from controller.constraints import clear_constraints
    clear_constraints()
    state.project = None


@pytest.fixture
def client():
    settings = get_settings()
    state.settings = settings
    return TestClient(app)


@pytest.fixture
def auth_headers():
    settings = get_settings()
    token = issue_access_token(settings, 1, "testuser")
    return {"Authorization": f"Bearer {token}"}


def test_agent_config_unauth(client):
    """Ensure agent config requires JWT bearer authentication."""
    resp = client.get("/api/agent/config")
    assert resp.status_code == 401


def test_agent_config_auth(client, auth_headers):
    """Retrieve agent config with authenticated token."""
    resp = client.get("/api/agent/config", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "has_api_key" in data
    assert "model" in data
    assert "available_models" in data
    assert len(data["available_models"]) >= 3


def test_agent_config_update(client, auth_headers):
    """Update agent model selection."""
    resp = client.post(
        "/api/agent/config",
        headers=auth_headers,
        json={"model": "nvidia/nemotron-3.5-lightning:free"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "nvidia/nemotron-3.5-lightning:free"


def test_agent_state(client, auth_headers):
    """Retrieve agent current state."""
    resp = client.get("/api/agent/state", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "messages" in data
    assert "steps_log" in data


def test_agent_chat_query(client, auth_headers, stub_fabric):
    """Chat with agent for telemetry audit / query."""
    resp = client.post(
        "/api/agent/chat",
        headers=auth_headers,
        json={"message": "What is the status of the network?", "topology_id": "diamond"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) >= 2
    last_msg = data["messages"][-1]
    assert last_msg["role"] == "assistant"
    assert len(last_msg["content"]) > 10


def test_agent_chat_action_langgraph_healthy(client, auth_headers, stub_fabric):
    """A fix request on a healthy network terminates without touching the fabric."""
    resp = client.post(
        "/api/agent/chat",
        headers=auth_headers,
        json={"message": "Fix congestion on link s1-s2", "topology_id": "diamond"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    steps = data["steps_log"]
    assert [s["node"] for s in steps] == ["monitor"]
    assert "healthy" in data["messages"][-1]["content"].lower()


def test_agent_run_endpoint_healthy(client, auth_headers, stub_fabric):
    """Autonomous loop on a healthy fabric does nothing and says so."""
    resp = client.post(
        "/api/agent/run",
        headers=auth_headers,
        json={"topology_id": "diamond"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert [s["node"] for s in data["steps_log"]] == ["monitor"]


def test_agent_create_congestion_endpoint(client, auth_headers, stub_fabric):
    """Create congestion on the live fabric reports the real spike."""
    resp = client.post(
        "/api/agent/create-congestion",
        headers=auth_headers,
        json={"topology_id": "diamond", "rate_mbps": 14.0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "congested"
    assert data["congestion_detected"] is True
    assert data["bottleneck_link"] == "s1--s2"
    assert len(data["messages"]) >= 2
    assert "14.0 Mbps" in data["messages"][-1]["content"]


def test_agent_create_congestion_fabric_down(client, auth_headers, monkeypatch):
    """With no fabric running, the agent refuses to claim a bottleneck event."""
    project = StubProject()

    class DownFabric:
        is_running = False
        spike_duration_s = 45.0
        spike_active = False
        current_mode = "static_shortest"

        def start_spike(self, rate_mbps=14.0, duration_s=45.0):
            return {"status": "error", "error": "live fabric not running — press Start Fabric first"}

        def get_status(self):
            return {"fabric_running": False, "routing_mode": self.current_mode,
                    "spike_active": False, "spike_rate_mbps": 0.0,
                    "spike_elapsed_s": 0.0, "spike_duration_s": 45.0}

    monkeypatch.setattr("future.api.live_fabric.LiveFabricManager.get_instance", lambda: DownFabric())
    state.project = project
    try:
        resp = client.post(
            "/api/agent/create-congestion",
            headers=auth_headers,
            json={"topology_id": "diamond", "rate_mbps": 14.0},
        )
    finally:
        state.project = None
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "unavailable"
    assert data["congestion_detected"] is False


def test_agent_chat_create_and_fix_loop(client, auth_headers, stub_fabric):
    """Full loop, driven entirely by the stub fabric:

    create congestion -> agent verifies congestion -> fix -> controller moves
    the flow -> verification passes on the relief.
    """
    # 1. Ask agent to create congestion
    create_resp = client.post(
        "/api/agent/chat",
        headers=auth_headers,
        json={"message": "create congestion on the network", "topology_id": "diamond"},
    )
    assert create_resp.status_code == 200
    c_data = create_resp.json()
    assert c_data["congestion_detected"] is True
    assert c_data["status"] == "congested"
    assert "spike started" in c_data["messages"][-1]["content"].lower()

    # 2. Telemetry now shows the bottleneck saturated
    telem = client.get("/api/telemetry/utilisation?topo_id=diamond", headers=auth_headers)
    assert telem.status_code == 200
    assert telem.json()["links"]["s1--s2"]["util_pct"] == 100.0

    # 3. Ask agent to fix it: measured mitigation, measured verification
    fix_resp = client.post(
        "/api/agent/chat",
        headers=auth_headers,
        json={"message": "please fix the congestion now", "topology_id": "diamond"},
    )
    assert fix_resp.status_code == 200
    f_data = fix_resp.json()
    assert f_data["status"] == "completed"
    assert f_data["verification"]["passed"] is True
    steps = [s["node"] for s in f_data["steps_log"]]
    assert steps == ["monitor", "analyze", "plan", "act", "verify"]
