"""Test suite for LangGraph SDN Congestion Control Agent — honest contract.

Tools return measured/real values or explicit ``unavailable`` reasons. No
fabricated constants anywhere. The tests attach a deterministic stub fabric so
the agent can be driven through every branch without a live Mininet fabric.
"""
import asyncio
import json
import os
import pytest
from future.agent.config import AgentConfig, agent_config
from future.agent.state import create_initial_state
from future.agent.tools import SdnAgentTools
from future.agent.llm import AgentLLM
from future.agent.graph import CongestionControlGraph

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
BOTTLENECK = ("s1--s2", "s2--s4")


class StubProject:
    def __init__(self, saturated=False, available=True):
        self.saturated = saturated
        self.available = available

    def _pin_moves_flow(self):
        """Mirror the live controller: a valid page constraint moves the flow."""
        try:
            from controller.constraints import read_constraints, blocked_pairs
            cons = read_constraints()
            blocked = {tuple(sorted(p)) for p in blocked_pairs(cons) if p}
            return any(tuple(sorted(b.split("--"))) in blocked for b in BOTTLENECK)
        except Exception:
            return False

    def link_utilisation(self, topo_id=None):
        if not self.available:
            return {"available": False,
                    "topo_id": topo_id or "diamond",
                    "timestamp": 0.0,
                    "links": {},
                    "reason": "fabric down or controller unavailable"}
        moved = self._pin_moves_flow()
        links = {}
        for key in LINKS:
            on_bottleneck = key in BOTTLENECK
            if moved:
                # The controller rerouted: measured load follows the new path.
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
        return [{"link": k, "probability": float(probs.get(k, 0.0)),
                 "congested": k in probs, "threshold": 0.5,
                 "horizon_s": 10.0, "reason": None,
                 "source": "stub", "direction": "forward"} for k in LINKS]

    def record_decision_event(self, data):
        return {"kind": "stub_decision", "recorded": True}

    def record_verify_event(self, data):
        return {"kind": "stub_verify", "recorded": True}


class StubFabric:
    def __init__(self, project, running=True):
        self.project = project
        self.is_running = running
        #: Detached fabric alive via pgrep — the post-restart case where the
        #: singleton says is_running=False but a fabric runs anyway.
        self._alive = running
        self.current_mode = "static_shortest"
        self.spike_active = False
        self.spike_duration_s = 45.0

    def running(self):
        return self._alive

    def start_spike(self, rate_mbps=14.0, duration_s=45.0):
        self.project.saturated = True
        self.spike_active = True
        return {"status": "spike_started", "rate_mbps": rate_mbps,
                "duration_s": duration_s}

    def set_routing_mode(self, mode):
        self.current_mode = mode
        self.project.saturated = False
        self.spike_active = False
        return {"status": "mode_updated", "mode": mode}

    def get_status(self):
        return {"fabric_running": self.is_running,
                "routing_mode": self.current_mode,
                "spike_active": self.spike_active,
                "spike_rate_mbps": 14.0 if self.spike_active else 0.0,
                "spike_elapsed_s": 0.0,
                "spike_duration_s": self.spike_duration_s}


def attach_stub(monkeypatch, tmp_path, saturated=False, fabric_running=True):
    project = StubProject(saturated=saturated, available=fabric_running)
    fabric = StubFabric(project, running=fabric_running)
    stub_controller_state(monkeypatch, tmp_path, mode="ml_aware")
    monkeypatch.setattr("future.api.live_fabric.LiveFabricManager.get_instance", lambda: fabric)
    from future.api.main import state
    state.project = project
    return project, fabric


@pytest.fixture
def stub_fabric(monkeypatch, tmp_path):
    yield attach_stub(monkeypatch, tmp_path, saturated=False, fabric_running=True)
    from future.api.main import state
    state.project = None


@pytest.fixture
def stub_saturated_fabric(monkeypatch, tmp_path):
    yield attach_stub(monkeypatch, tmp_path, saturated=True, fabric_running=True)
    from controller.constraints import clear_constraints
    clear_constraints()
    from future.api.main import state
    state.project = None


@pytest.fixture
def down_fabric(monkeypatch, tmp_path):
    yield attach_stub(monkeypatch, tmp_path, saturated=False, fabric_running=False)
    from future.api.main import state
    state.project = None


def test_agent_config():
    """Verify agent configuration loading, masking, and model definitions."""
    cfg = AgentConfig()
    d = cfg.to_dict()
    assert "has_api_key" in d
    assert "masked_key" in d
    assert "model" in d
    assert len(d["available_models"]) >= 3


def test_agent_tools_telemetry(stub_fabric):
    """Verify tool inspection of link telemetry returns real fields."""
    tools = SdnAgentTools()
    res = tools.inspect_telemetry("diamond")
    assert res["status"] == "ok"
    assert "bottleneck" in res
    assert "bottleneck_util_pct" in res
    assert "is_congested" in res
    assert res["is_congested"] is False
    assert res["links"]["s1--s2"]["util_pct"] == 0.0


def test_agent_tools_telemetry_congested(stub_saturated_fabric):
    """A saturated bottleneck is detected from measurements."""
    tools = SdnAgentTools()
    res = tools.inspect_telemetry("diamond")
    assert res["status"] == "ok"
    assert res["is_congested"] is True
    assert res["bottleneck"] == "s1--s2"
    assert res["bottleneck_util_pct"] == 100.0
    assert any(l["link"] == "s1--s2" for l in res["congested_links"])


def test_agent_tools_predictions(stub_fabric):
    """Predictions carry real model probabilities, never fabricated constants."""
    tools = SdnAgentTools()
    idle = tools.inspect_ml_predictions("diamond")
    assert idle["status"] == "ok"
    assert idle["probability"] == 0.0
    assert idle["predicted_hazard"] is False


def test_agent_tools_predictions_congested(stub_saturated_fabric):
    tools = SdnAgentTools()
    hot = tools.inspect_ml_predictions("diamond")
    assert hot["status"] == "ok"
    assert hot["probability"] == pytest.approx(0.9, abs=1e-6)
    assert hot["predicted_hazard"] is True


def test_agent_tools_topology():
    """Verify topology inspection returns valid paths across multiple topologies."""
    tools = SdnAgentTools()
    topo = tools.inspect_topology("diamond")
    assert topo["id"] == "diamond"
    assert "default_path" in topo
    assert "alternate_path" in topo
    assert topo["alternate_path"] == ["h1", "s1", "s3", "s4", "h4"]

    dd_topo = tools.inspect_topology("double_diamond")
    assert dd_topo["id"] == "double_diamond"
    assert dd_topo["bottleneck"] == "s1--s2"
    assert "s3" in dd_topo["alternate_path"]

    ab_topo = tools.inspect_topology("abilene_wan")
    assert ab_topo["bottleneck"] == "DEN--ORD"
    assert "LAX" in ab_topo["alternate_path"]


def test_agent_inject_congestion(down_fabric):
    """inject_congestion reports failure honestly with no fabric running."""
    tools = SdnAgentTools()
    unavailable = tools.inject_congestion("diamond", 14.0)
    assert unavailable["status"] == "unavailable"


def test_agent_inject_congestion_live(stub_fabric):
    """With a fabric running, the spike actually starts."""
    tools = SdnAgentTools()
    res = tools.inject_congestion("diamond", 14.0)
    assert res["status"] == "spike_started"
    assert res["rate_mbps"] == 14.0


def test_agent_execute_and_verify(stub_saturated_fabric):
    """Detour action + closed-loop verification, all measured."""
    tools = SdnAgentTools()
    exec_res = tools.execute_reroute_detour("diamond", "ml_aware")
    assert exec_res["status"] == "pinned"
    assert "latency_ms" in exec_res
    # After the mode switch the stub moves the flow off the bottleneck
    verify_res = tools.verify_telemetry_reduction("s1--s2", "diamond")
    assert verify_res["status"] == "VERIFIED PASS"
    assert verify_res["passed"] is True
    assert verify_res["current_load_pct"] is not None


def test_agent_verify_denied_when_unavailable(down_fabric):
    """Verification never reports relief without live telemetry."""
    tools = SdnAgentTools()
    verify_res = tools.verify_telemetry_reduction("s1--s2", "diamond", window_s=1.0)
    assert verify_res["passed"] is False
    assert verify_res["status"] == "UNAVAILABLE"


def test_agent_verify_fails_when_alternate_path_saturates(monkeypatch, tmp_path):
    """When the alternate path itself saturates, verification honestly declares FAIL."""
    class SaturatedBothProject(StubProject):
        def link_utilisation(self, topo_id=None):
            res = super().link_utilisation(topo_id)
            for k in res["links"]:
                res["links"][k]["util_pct"] = 95.0
            res["links"]["s1--s2"]["util_pct"] = 0.0
            return res

    project = SaturatedBothProject()
    fabric = StubFabric(project, running=True)
    stub_controller_state(monkeypatch, tmp_path, mode="ml_aware",
                          flows=[{"flow": "s1->s4", "path": ["s1", "s3", "s4"]}])
    monkeypatch.setattr("future.api.live_fabric.LiveFabricManager.get_instance", lambda: fabric)
    from future.api.main import state
    state.project = project
    try:
        tools = SdnAgentTools()
        res = tools.verify_telemetry_reduction("s1--s2", "diamond", window_s=2.0)
        assert res["passed"] is False
        assert res["status"] == "VERIFIED FAIL"
        assert res["alt_path_saturated"] is True
        assert "saturated" in res["pass_reason"].lower()
    finally:
        state.project = None


def test_agent_llm_response(monkeypatch):
    """With no provider keys configured, the deterministic factual reporter answers."""
    from future.agent.config import agent_config
    monkeypatch.setattr(agent_config, "api_key", "")
    monkeypatch.setattr(agent_config, "groq_api_key", "")

    async def _test():
        llm = AgentLLM()
        context = {
            "topology_id": "diamond",
            "bottleneck": "s1--s2",
            "bottleneck_util_pct": 88.0,
            "ml_probability": 0.91,
            "congested": True,
            "telemetry_status": "ok",
            "links": {},
        }
        resp = await llm.generate_response("What is happening on the network?", context)
        assert isinstance(resp, str)
        assert len(resp) > 20
        assert "s1--s2" in resp or "bottleneck" in resp.lower()

    asyncio.run(_test())


class _StubResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)

    def json(self):
        return self._payload


class _StubAsyncClient:
    """httpx.AsyncClient stand-in; scripts per-model responses."""

    calls = []
    script = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        model = (json or {}).get("model")
        _StubAsyncClient.calls.append({"url": url, "auth": (headers or {}).get("Authorization"),
                                       "model": model})
        status, payload = _StubAsyncClient.script.get(model, (500, {"error": "unscripted"}))
        return _StubResponse(status, payload)


def _llm_stub(monkeypatch, cfg_overrides, script):
    import future.agent.llm as llm_mod
    from future.agent.config import agent_config
    _StubAsyncClient.calls = []
    _StubAsyncClient.script = script
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _StubAsyncClient)
    for k, v in cfg_overrides.items():
        monkeypatch.setattr(agent_config, k, v)
    return agent_config


def _ok(text):
    return (200, {"choices": [{"message": {"content": text * 3}}]})


def test_llm_groq_fallback_when_openrouter_fails(monkeypatch):
    """Selected OpenRouter model failing hands off to Groq, with correct keys."""
    _llm_stub(
        monkeypatch,
        {"api_key": "or-key", "groq_api_key": "gsk-test-key",
         "model": "nvidia/nemotron-3.5-lightning:free"},
        {
            "nvidia/nemotron-3.5-lightning:free": (429, {"error": "rate limited"}),
            "qwen/qwen3.8-27b": _ok("Groq answers the congestion question. "),
        },
    )

    async def _test():
        llm = AgentLLM()
        resp = await llm.generate_response("Fix congestion", {"topology_id": "diamond"})
        assert "Groq answers" in resp

    asyncio.run(_test())
    tried = [c["model"] for c in _StubAsyncClient.calls]
    assert tried == ["nvidia/nemotron-3.5-lightning:free", "qwen/qwen3.8-27b"]
    assert _StubAsyncClient.calls[0]["url"].startswith("https://openrouter.ai")
    assert _StubAsyncClient.calls[1]["url"].startswith("https://api.groq.com")
    assert _StubAsyncClient.calls[0]["auth"] == "Bearer or-key"
    assert _StubAsyncClient.calls[1]["auth"] == "Bearer gsk-test-key"


def test_llm_openrouter_skipped_when_no_key_but_groq_works(monkeypatch):
    """With no OpenRouter key the chain goes straight to Groq."""
    _llm_stub(
        monkeypatch,
        {"api_key": "", "groq_api_key": "gsk-test-key",
         "model": "nvidia/nemotron-3.5-lightning:free"},
        {"qwen/qwen3.8-27b": _ok("Groq without OpenRouter key. ")},
    )

    async def _test():
        llm = AgentLLM()
        resp = await llm.generate_response("Fix congestion", {"topology_id": "diamond"})
        assert "Groq without OpenRouter" in resp

    asyncio.run(_test())
    tried = [c["model"] for c in _StubAsyncClient.calls]
    assert tried == ["qwen/qwen3.8-27b"]


def test_llm_full_chain_failure_falls_back_to_deterministic(monkeypatch):
    """Total provider failure still yields the factual reporter, never invented text."""
    _llm_stub(
        monkeypatch,
        {"api_key": "or-key", "groq_api_key": "gsk-test-key",
         "model": "nvidia/nemotron-3.5-lightning:free"},
        {},  # every model 500s
    )

    async def _test():
        llm = AgentLLM()
        context = {"topology_id": "diamond", "bottleneck": "s1--s2",
                   "bottleneck_util_pct": 88.0, "ml_probability": 0.91,
                   "congested": True, "telemetry_status": "ok", "links": {}}
        resp = await llm.generate_response("What is happening?", context)
        assert "s1--s2" in resp
        assert len(resp) > 20

    asyncio.run(_test())
    tried = [c["model"] for c in _StubAsyncClient.calls]
    assert tried[0] == "nvidia/nemotron-3.5-lightning:free"
    assert "qwen/qwen3.8-27b" in tried


def test_llm_to_dict_exposes_groq_status(monkeypatch):
    from future.agent.config import AgentConfig
    cfg = AgentConfig()
    monkeypatch.setattr(cfg, "api_key", "or-key-123456789")
    monkeypatch.setattr(cfg, "groq_api_key", "gsk-key-abcdefgh")
    d = cfg.to_dict()
    assert d["has_api_key"] is True
    assert d["has_groq_api_key"] is True
    assert d["groq_masked_key"].startswith("gsk-...")
    providers = {m.get("provider") for m in d["available_models"]}
    assert providers == {"openrouter", "groq"}
    assert any(m["id"] == "qwen/qwen3.8-27b" for m in d["available_models"])


def test_llm_fallback_order_selected_first():
    from future.agent.config import FALLBACK_PRIORITY
    from future.agent.config import agent_config
    order = agent_config.fallback_order()
    assert order[0] == agent_config.model
    assert len(order) == len(set(order))
    assert set(order) == {*FALLBACK_PRIORITY, agent_config.model}


def test_langgraph_workflow_execution(stub_saturated_fabric):
    """Full closed-loop: congestion detected, action taken, verified PASS."""
    async def _test():
        graph = CongestionControlGraph()
        state = create_initial_state("Resolve bottleneck congestion", "diamond")
        state["congestion_detected"] = True
        state["bottleneck_link"] = "s1--s2"

        result = await graph.run(state)
        assert result["status"] == "completed"
        assert len(result["steps_log"]) == 5
        nodes = [s["node"] for s in result["steps_log"]]
        assert nodes == ["monitor", "analyze", "plan", "act", "verify"]
        assert result["verification"]["passed"] is True
        assert "VERIFIED PASS" in result["messages"][-1]["content"] or "PASS" in result["messages"][-1]["content"]

    asyncio.run(_test())


def test_ask_controller_alternatives_available(stub_saturated_fabric, tmp_path, monkeypatch):
    """Snapshot present and fresh → alternatives enumerate from the live graph."""
    import time as _time, json as _json
    tools = SdnAgentTools()
    state_path = os.environ.get("SDN_CONTROLLER_STATE")
    res = tools.ask_controller_for_alternatives("diamond")
    assert res["status"] == "ok"
    assert res["has_alternate"] is True
    paths = [alt["switches"] for alt in res["alternatives"]]
    assert ["s1", "s3", "s4"] in paths
    assert res["current_path"] == ["s1", "s2", "s4"]
    assert res["n_links"] == 4


def test_ask_controller_alternatives_missing(tmp_path, monkeypatch):
    """No snapshot → honest unavailable, never fabricated paths."""
    monkeypatch.setenv("SDN_CONTROLLER_STATE", str(tmp_path / "nonexistent.json"))
    tools = SdnAgentTools()
    res = tools.ask_controller_for_alternatives("diamond")
    assert res["status"] == "unavailable"
    assert "not reporting" in res["reason"] or "fabric" in res["reason"]


def test_ask_controller_alternatives_stale(tmp_path, monkeypatch):
    """Snapshot older than the staleness bound → unavailable (fabric down)."""
    import time as _time, json as _json
    payload = {
        "ts": _time.time() - 120,
        "mode": "ml_aware",
        "graph": {"s1": ["s2"], "s2": ["s1"]},
        "links": [{"a": "s1", "b": "s2"}],
        "n_links": 1,
        "flows": [{"flow": "f", "path": ["s1", "s2", "s4"]}],
        "constraints": {},
    }
    path = tmp_path / "stale.json"
    path.write_text(_json.dumps(payload))
    monkeypatch.setenv("SDN_CONTROLLER_STATE", str(path))
    tools = SdnAgentTools()
    res = tools.ask_controller_for_alternatives("diamond")
    assert res["status"] == "unavailable"
    assert "stale" in res["reason"]


def test_telemetry_reports_detached_fabric_as_alive(monkeypatch):
    """Post-restart: singleton reports unspawned but a detached fabric runs."""
    project = StubProject(saturated=True, available=True)
    fabric = StubFabric(project, running=False)   # never spawned by this API
    fabric._alive = True                          # but 16:54-style fabric lives
    monkeypatch.setattr("future.api.live_fabric.LiveFabricManager.get_instance", lambda: fabric)
    from future.api.main import state
    state.project = project
    tools = SdnAgentTools()
    res = tools.inspect_telemetry("diamond")
    state.project = None
    assert res["status"] == "ok"
    assert res["is_congested"] is True
    assert res["bottleneck"] == "s1--s2"


def test_telemetry_reports_true_death_as_unavailable(monkeypatch):
    project = StubProject(saturated=False, available=True)
    fabric = StubFabric(project, running=False)
    fabric._alive = False
    monkeypatch.setattr("future.api.live_fabric.LiveFabricManager.get_instance", lambda: fabric)
    from future.api.main import state
    state.project = project
    tools = SdnAgentTools()
    res = tools.inspect_telemetry("diamond")
    state.project = None
    assert res["status"] == "unavailable"
    assert "frozen" in res["reason"]
