"""Integration tests for Agentic AI FastAPI endpoints."""
import pytest
from fastapi.testclient import TestClient
from future.api.main import app, state
from future.api.security import issue_access_token
from future.api.settings import get_settings


@pytest.fixture
def client():
    # Ensure settings are populated
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
    assert "status" in data


def test_agent_chat_query(client, auth_headers):
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


def test_agent_chat_action_langgraph(client, auth_headers):
    """Send an action command ('fix congestion') triggering the LangGraph workflow."""
    resp = client.post(
        "/api/agent/chat",
        headers=auth_headers,
        json={"message": "Fix congestion on link s1-s2", "topology_id": "diamond"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert len(data["steps_log"]) == 5
    nodes = [s["node"] for s in data["steps_log"]]
    assert nodes == ["monitor", "analyze", "plan", "act", "verify"]
    assert data["verification"]["passed"] is True


def test_agent_run_endpoint(client, auth_headers):
    """Trigger autonomous mitigation loop via POST /api/agent/run."""
    resp = client.post(
        "/api/agent/run",
        headers=auth_headers,
        json={"topology_id": "abilene_wan"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert len(data["steps_log"]) == 5
    assert data["verification"]["passed"] is True


def test_agent_create_congestion_endpoint(client, auth_headers):
    """Trigger congestion injection via dedicated POST /api/agent/create-congestion endpoint."""
    resp = client.post(
        "/api/agent/create-congestion",
        headers=auth_headers,
        json={"topology_id": "double_diamond", "rate_mbps": 14.0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "congested"
    assert data["congestion_detected"] is True
    assert data["bottleneck_link"] == "s1--s2"
    assert len(data["messages"]) >= 2
    assert "14.0 Mbps" in data["messages"][-1]["content"]


def test_agent_chat_create_and_fix_loop(client, auth_headers):
    """Test full interactive conversation loop: create congestion -> agent fixes congestion."""
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
    assert "injected" in c_data["messages"][-1]["content"].lower()

    # 2. Ask agent to fix it
    fix_resp = client.post(
        "/api/agent/chat",
        headers=auth_headers,
        json={"message": "please fix the congestion now", "topology_id": "diamond"},
    )
    assert fix_resp.status_code == 200
    f_data = fix_resp.json()
    assert f_data["status"] == "completed"
    assert f_data["verification"]["passed"] is True
    assert len(f_data["steps_log"]) == 5
