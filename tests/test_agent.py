"""Test suite for LangGraph SDN Congestion Control Agent."""
import pytest
import asyncio
from future.agent.config import AgentConfig, agent_config
from future.agent.state import create_initial_state
from future.agent.tools import SdnAgentTools
from future.agent.llm import AgentLLM
from future.agent.graph import CongestionControlGraph


def test_agent_config():
    """Verify agent configuration loading, masking, and model definitions."""
    cfg = AgentConfig()
    d = cfg.to_dict()
    assert "has_api_key" in d
    assert "masked_key" in d
    assert "model" in d
    assert len(d["available_models"]) >= 3
    if cfg.api_key:
        assert d["has_api_key"] is True
        assert d["masked_key"].startswith("sk-or-")
        assert d["masked_key"].endswith("44c0")


def test_agent_tools_telemetry():
    """Verify tool inspection of link telemetry."""
    tools = SdnAgentTools()
    res = tools.inspect_telemetry("diamond")
    assert "bottleneck" in res
    assert "bottleneck_util_pct" in res
    assert "is_congested" in res


def test_agent_tools_predictions():
    """Verify tool inspection of ML forecasts."""
    tools = SdnAgentTools()
    preds = tools.inspect_ml_predictions("diamond")
    assert "probability" in preds
    assert "predicted_hazard" in preds
    assert 0.0 <= preds["probability"] <= 1.0


def test_agent_tools_topology():
    """Verify topology inspection returns valid paths across multiple topologies."""
    tools = SdnAgentTools()
    topo = tools.inspect_topology("diamond")
    assert topo["id"] == "diamond"
    assert "primary_path" in topo
    assert "alternate_path" in topo
    assert topo["alternate_path"] == ["h1", "s1", "s3", "s4", "h4"]

    # Verify double diamond
    dd_topo = tools.inspect_topology("double_diamond")
    assert dd_topo["id"] == "double_diamond"
    assert dd_topo["bottleneck"] == "s1--s2"
    assert "s3" in dd_topo["alternate_path"]

    # Verify Abilene WAN
    ab_topo = tools.inspect_topology("abilene_wan")
    assert ab_topo["bottleneck"] == "DEN--ORD"
    assert "LAX" in ab_topo["alternate_path"]


def test_agent_inject_congestion():
    """Verify agent inject congestion tool creates traffic spike on bottleneck."""
    tools = SdnAgentTools()
    res = tools.inject_congestion("diamond", 14.0)
    assert res["status"] == "congestion_active"
    assert res["bottleneck_link"] == "s1--s2"
    assert res["offered_rate_mbps"] == 14.0
    assert res["hazard_probability"] >= 0.80


def test_agent_tools_execution_and_verify():
    """Verify detour reroute execution and closed-loop verification."""
    tools = SdnAgentTools()
    exec_res = tools.execute_reroute_detour("diamond", "ml_aware")
    assert exec_res["status"] == "success"
    assert exec_res["flow_mods_installed"] == 2
    assert "latency_ms" in exec_res

    verify_res = tools.verify_telemetry_reduction("s1--s2", "diamond")
    assert verify_res["status"] == "VERIFIED PASS"
    assert verify_res["passed"] is True
    assert verify_res["current_load_pct"] < 80.0


def test_agent_llm_response():
    """Verify LLM response generation with fallback and domain prompts."""
    async def _test():
        llm = AgentLLM()
        context = {
            "topology_id": "diamond",
            "bottleneck": "s1--s2",
            "bottleneck_util_pct": 88.0,
            "ml_probability": 0.91,
        }
        resp = await llm.generate_response("Analyze current network congestion", context)
        assert isinstance(resp, str)
        assert len(resp) > 20
        assert "s1--s2" in resp or "bottleneck" in resp.lower()

    asyncio.run(_test())


def test_langgraph_workflow_execution():
    """Verify full end-to-end LangGraph state transitions."""
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
        assert len(result["messages"]) >= 2
        assert "VERIFIED PASS" in result["messages"][-1]["content"]

    asyncio.run(_test())

