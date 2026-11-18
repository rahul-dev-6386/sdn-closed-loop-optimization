"""LangGraph StateGraph for the SDN congestion agent.

Decision policy, stated plainly:

* The agent acts only on **measured** congestion or a **measured** hazard
  prediction. An idle network terminates after monitor with a report.
* The mitigation lever is a real controller mode switch on the live fabric
  (``ml_aware``); the closed-loop controller itself decides whether and when
  to reroute individual flows. The agent does not fabricate flow-mods.
* The verdict is measurement: PASS only after consecutive sub-threshold
  samples; FAIL otherwise, reported with the numbers observed.
"""
from __future__ import annotations

import time
import logging
from typing import Any, Dict, Literal

from langgraph.graph import StateGraph, START, END

from future.agent.state import AgentState, StepLogEntry
from future.agent.tools import SdnAgentTools
from future.agent.llm import llm_client

LOGGER = logging.getLogger("sdn.agent.graph")


def get_timestamp() -> str:
    return time.strftime("%H:%M:%S")


class CongestionControlGraph:
    """Encapsulates the LangGraph closed-loop congestion mitigation workflow."""

    def __init__(self, project_state=None):
        self.tools = SdnAgentTools(project_state)
        self.workflow = self._build_graph()
        self.app = self.workflow.compile()

    # --- Node 1: Monitor ----------------------------------------------------
    async def monitor_node(self, state: AgentState) -> Dict[str, Any]:
        """Collect REAL telemetry + REAL predictions."""
        topo_id = state.get("topology_id", "diamond")
        telemetry = self.tools.inspect_telemetry(topo_id)
        predictions = self.tools.inspect_ml_predictions(topo_id)

        congested = bool(telemetry.get("is_congested"))
        hazard = bool(predictions.get("predicted_hazard"))
        util = telemetry.get("bottleneck_util_pct", 0.0)
        prob = predictions.get("probability")

        if telemetry.get("status") == "unavailable":
            step_entry: StepLogEntry = {
                "node": "monitor",
                "title": "Stage 1: Telemetry & Ingress Sampling",
                "description": (f"Could not read live telemetry: {telemetry.get('reason')}. "
                                "Agent will not act on an unseen network."),
                "timestamp": get_timestamp(),
                "status": "error",
                "meta": {"telemetry": telemetry, "predictions": predictions},
            }
            steps = list(state.get("steps_log", []))
            steps.append(step_entry)
            messages = list(state.get("messages", []))
            messages.append({
                "role": "assistant",
                "content": (f"**Cannot act:** live telemetry is unavailable "
                            f"({telemetry.get('reason')}). Start the fabric (Start Fabric "
                            f"button, or press Spike in live mode) and ask me again."),
                "time": get_timestamp(),
            })
            return {
                "network_state": telemetry,
                "predictions": predictions,
                "congestion_detected": False,
                "hazard": False,
                "bottleneck_link": telemetry.get("bottleneck", "s1--s2"),
                "status": "unavailable",
                "messages": messages,
                "steps_log": steps,
            }

        # Healthy-network fast end: report measured state and stop.
        if not congested and not hazard:
            step_entry = {
                "node": "monitor",
                "title": "Stage 1: Telemetry & Ingress Sampling",
                "description": (f"Live port-stats: worst link {telemetry.get('bottleneck')} "
                                f"at {util}% (threshold {telemetry.get('threshold_pct')}%). "
                                f"No link is congested and the ML forecast is clear."),
                "timestamp": get_timestamp(),
                "status": "success",
                "meta": {"telemetry": telemetry, "predictions": predictions},
            }
            steps = list(state.get("steps_log", []))
            steps.append(step_entry)
            messages = list(state.get("messages", []))
            messages.append({
                "role": "assistant",
                "content": (f"**Network healthy.** Worst link `{telemetry.get('bottleneck')}` "
                            f"at {util}% (threshold {telemetry.get('threshold_pct')}%). "
                            f"ML forecast: {predictions.get('summary') or 'not scored yet'}. "
                            f"No reroute applied — nothing to fix."),
                "time": get_timestamp(),
            })
            return {
                "network_state": telemetry,
                "predictions": predictions,
                "congestion_detected": False,
                "hazard": False,
                "bottleneck_link": telemetry.get("bottleneck", "s1--s2"),
                "status": "completed",
                "messages": messages,
                "steps_log": steps,
            }

        step_entry = {
            "node": "monitor",
            "title": "Stage 1: Telemetry & Ingress Sampling",
            "description": (f"Live port-stats: worst link {telemetry.get('bottleneck')} "
                            f"at {util}% (threshold {telemetry.get('threshold_pct')}%). "
                            f"ML peak p={prob if prob is not None else 'n/a'}."),
            "timestamp": get_timestamp(),
            "status": "warning" if (congested or hazard) else "success",
            "meta": {"telemetry": telemetry, "predictions": predictions},
        }
        steps = list(state.get("steps_log", []))
        steps.append(step_entry)
        return {
            "network_state": telemetry,
            "predictions": predictions,
            "congestion_detected": congested,
            "hazard": hazard,
            "bottleneck_link": telemetry.get("bottleneck", "s1--s2"),
            "status": "monitoring",
            "steps_log": steps,
        }

    # --- Node 2: Analyze ----------------------------------------------------
    async def analyze_node(self, state: AgentState) -> Dict[str, Any]:
        """LLM narration over MEASURED context only."""
        topo_id = state.get("topology_id", "diamond")
        ns = state.get("network_state", {})
        predictions = state.get("predictions", {})
        context = {
            "topology_id": topo_id,
            "bottleneck": ns.get("bottleneck"),
            "bottleneck_util_pct": ns.get("bottleneck_util_pct"),
            "congested_links": ns.get("congested_links", []),
            "links": ns.get("links", {}),
            "ml_probability": predictions.get("probability"),
            "ml_hazard": predictions.get("predicted_hazard"),
            "ml_summary": predictions.get("summary"),
        }
        prompt = (
            "You are analysing a live SDN testbed. Explain, in <= 3 sentences and only "
            "with the numbers provided, what is happening on the bottleneck link and "
            "whether the ML forecast supports acting now. Do not invent numbers."
        )
        analysis = await llm_client.generate_response(prompt, context)

        step_entry: StepLogEntry = {
            "node": "analyze",
            "title": "Stage 2: LLM Risk Assessment & Root Cause",
            "description": analysis[:200] + ("…" if len(analysis) > 200 else ""),
            "timestamp": get_timestamp(),
            "status": "success",
            "meta": {"analysis": analysis},
        }
        steps = list(state.get("steps_log", []))
        steps.append(step_entry)
        return {"analysis": analysis, "status": "analyzing", "steps_log": steps}

    # --- Node 3: Plan -------------------------------------------------------
    async def plan_node(self, state: AgentState) -> Dict[str, Any]:
        """Choose action from MEASURED path loads."""
        topo_id = state.get("topology_id", "diamond")
        ns = state.get("network_state", {}) or {}
        topo_info = self.tools.inspect_topology(topo_id)
        default_path = topo_info.get("default_path", [])
        alternate_path = topo_info.get("alternate_path", [])

        def path_load(path):
            links = ns.get("links", {})
            hops = []
            for i in range(len(path) - 1):
                key = "--".join(sorted([path[i], path[i + 1]]))
                hops.append(float((links.get(key) or {}).get("used_mbps", 0.0) or 0.0))
            # A path's effective throughput is its slowest hop; summing hops
            # double-counts the same flow bytes across every link it crosses.
            return max(hops) if hops else 0.0

        def_load = path_load(default_path)
        alt_load = path_load(alternate_path)
        bottleneck = ns.get("bottleneck")
        congested = bool(state.get("congestion_detected"))
        hazard = bool(state.get("hazard"))
        act = congested or hazard

        # Ask the controller's own live graph whether a divert is physically
        # possible right now — the agent cannot see LLDP state any other way.
        ctrl = self.tools.ask_controller_for_alternatives(topo_id)
        ctrl_block: Dict[str, Any] = {}
        if ctrl.get("status") == "ok":
            alts = ctrl.get("alternatives") or []
            ctrl_block = {
                "reachable": True,
                "current_path": ctrl.get("current_path"),
                "alternatives": alts,
                "n_links": ctrl.get("n_links"),
                "blocked_links": ctrl.get("blocked_links"),
                "mode": ctrl.get("mode"),
            }
        else:
            ctrl_block = {"reachable": False,
                          "reason": ctrl.get("reason")}

        if act:
            if ctrl.get("status") == "ok":
                if ctrl_block["alternatives"]:
                    alt = ctrl_block["alternatives"][0]
                    plan = (f"Controller reports an alternate path is available right now: "
                            f"{' → '.join(alt['switches'])} ({alt['hops']} hops). "
                            f"Constraining the bottleneck link forces the controller to divert to it. "
                            f"Measured slowest-hop load: default ≈ {def_load:.2f} Mbps, "
                            f"alternate ≈ {alt_load:.2f} Mbps. Bottleneck: {bottleneck}.")
                else:
                    plan = (f"Controller's live graph shows NO alternate path "
                            f"({ctrl.get('src')}→{ctrl.get('dst')}): {ctrl.get('n_links')} link(s) up "
                            f"[{', '.join(sorted(ctrl.get('graph_nodes') or []))}]. "
                            f"Congestion cannot be diverted — reduce the offered load or restore the missing link.")
            else:
                plan = (f"Controller state unavailable — {ctrl_block.get('reason')}. "
                        f"Measured slowest-hop load: default ≈ {def_load:.2f} Mbps, "
                        f"alternate ≈ {alt_load:.2f} Mbps. "
                        f"Bottleneck: {bottleneck}. Cannot divert without a confirmed alternate.")
        else:
            plan = (f"No action warranted: measured loads default ≈ {def_load:.2f} Mbps, "
                    f"alternate ≈ {alt_load:.2f} Mbps; no link over threshold and no ML hazard.")

        step_entry: StepLogEntry = {
            "node": "plan",
            "title": "Stage 3: Constrained Multipath Decision",
            "description": plan,
            "timestamp": get_timestamp(),
            "status": "success" if act else "success",
            "meta": {"default_load_mbps": round(def_load, 3),
                     "alternate_load_mbps": round(alt_load, 3),
                     "alternate_path": alternate_path,
                     "action": "ml_aware" if act else "none",
                     "controller": ctrl_block,
                     "alternatives": ctrl_block.get("alternatives")},
        }
        steps = list(state.get("steps_log", []))
        steps.append(step_entry)
        return {"plan": plan, "act": act, "status": "planning",
                "steps_log": steps, "controller_report": ctrl_block}

    # --- Node 4: Act --------------------------------------------------------
    async def act_node(self, state: AgentState) -> Dict[str, Any]:
        """Apply the real lever: routing mode switch on the live fabric."""
        topo_id = state.get("topology_id", "diamond")
        if not state.get("act", True):
            steps = list(state.get("steps_log", []))
            steps.append({
                "node": "act",
                "title": "Stage 4: Action",
                "description": "Skipped — no congestion and no predicted hazard.",
                "timestamp": get_timestamp(),
                "status": "success",
                "meta": {"action": "none"},
            })
            return {"action_taken": "no action (network healthy)", "status": "noop",
                    "steps_log": steps}

        # Ask the controller BEFORE touching anything — no point pinning a link
        # the controller cannot route around.
        ctrl = state.get("controller_report") or {}
        if ctrl.get("reachable") is True and not ctrl.get("alternatives"):
            step_entry = {
                "node": "act",
                "title": "Stage 4: Constraint Hint to Controller",
                "description": ("No alternate path exists in the controller's live graph — "
                               "nothing diverted. Reduce the offered load or restore the "
                               "missing link."),
                "timestamp": get_timestamp(),
                "status": "error",
                "meta": {"alternatives": [], "pinned": False, "controller": ctrl},
            }
            steps = list(state.get("steps_log", []))
            steps.append(step_entry)
            return {
                "action_taken": "no alternate path — nothing diverted",
                "status": "unavailable",
                "steps_log": steps,
            }
        if ctrl.get("reachable") is False:
            # Fabric/controller not reporting — a pin would be write-only.
            reason = ctrl.get("reason") or "controller silent"
            step_entry = {
                "node": "act",
                "title": "Stage 4: Constraint Hint to Controller",
                "description": (f"Controller is not reporting its live graph — {reason}. "
                               f"No path query possible; nothing pinned or diverted. "
                               f"Start the fabric (or fix the controller) and try again."),
                "timestamp": get_timestamp(),
                "status": "error",
                "meta": {"pinned": False, "controller": ctrl},
            }
            steps = list(state.get("steps_log", []))
            steps.append(step_entry)
            return {
                "action_taken": f"controller unavailable — {reason}",
                "status": "unavailable",
                "steps_log": steps,
            }

        if (state.get("controller_report") or {}).get("mode") == "static_shortest":
            step_entry = {
                "node": "act",
                "title": "Stage 4: Constraint Hint to Controller",
                "description": ("Controller is in static_shortest mode — it runs no ML decision "
                               "loop, so a constraint pin would never be read. Switch the routing "
                               "mode to ml_aware and retry."),
                "timestamp": get_timestamp(),
                "status": "error",
                "meta": {"pinned": False, "controller": state.get("controller_report") or {}},
            }
            steps = list(state.get("steps_log", []))
            steps.append(step_entry)
            return {
                "action_taken": "controller in static_shortest — nothing pinned",
                "status": "unavailable",
                "steps_log": steps,
            }

        result = self.tools.execute_reroute_detour(topo_id, mode="ml_aware",
                                                     pin_seconds=30.0)
        ok = result.get("status") in ("pinned", "applied", "ok")
        step_entry: StepLogEntry = {
            "node": "act",
            "title": "Stage 4: Constraint Hint to Controller",
            "description": result.get("summary") or result.get("reason", "unavailable"),
            "timestamp": get_timestamp(),
            "status": "success" if ok else "error",
            "meta": result,
        }
        steps = list(state.get("steps_log", []))
        steps.append(step_entry)
        return {
            "action_taken": result.get("summary", "unavailable"),
            "status": "acting" if ok else "unavailable",
            "steps_log": steps,
        }

    # --- Node 5: Verify -----------------------------------------------------
    async def verify_node(self, state: AgentState) -> Dict[str, Any]:
        """Verdict from live samples only — PASS or FAIL, never both."""
        topo_id = state.get("topology_id", "diamond")
        ns = state.get("network_state", {}) or {}
        bottleneck = state.get("bottleneck_link", "s1--s2")
        before = ns.get("bottleneck_util_pct")

        if state.get("status") == "noop" or state.get("act") is False:
            verification = {
                "status": "NOT NEEDED",
                "passed": True,
                "bottleneck_link": bottleneck,
                "before_load_pct": before,
                "current_load_pct": before,
                "summary": "No action taken; nothing to verify.",
            }
        elif state.get("status") == "unavailable":
            # Divert was impossible (no alternate path or controller silent);
            # say so, don't dress it up as a measurement result.
            action_reason = state.get("action_taken", "controller/fabric unavailable")
            verification = {
                "status": "NOT APPLIED",
                "passed": False,
                "skipped": True,
                "bottleneck_link": bottleneck,
                "before_load_pct": before,
                "current_load_pct": before,
                "summary": (f"Not applicable — divert impossible: {action_reason}. "
                            f"Congestion `{bottleneck}` cannot be rerouted until the "
                            f"alternate path is restored."),
            }
        else:
            verification = self.tools.verify_telemetry_reduction(
                bottleneck, topo_id, before_pct=before)

        passed = bool(verification.get("passed"))
        step_entry: StepLogEntry = {
            "node": "verify",
            "title": "Stage 5: Closed-Loop Telemetry Verification",
            "description": verification.get("summary", ""),
            "timestamp": get_timestamp(),
            "status": "success" if passed else "error",
            "meta": verification,
        }
        steps = list(state.get("steps_log", []))
        steps.append(step_entry)

        verdict = "PASS" if passed else "FAIL"
        if state.get("status") == "noop" or state.get("act") is False:
            assistant_msg = (
                f"Nothing to fix: bottleneck `{bottleneck}` is below threshold "
                f"({before}%) and the ML forecast is not flagging it. The network "
                f"is healthy — no reroute applied."
            )
            return {
                "verification": verification,
                "status": "healthy",
                "steps_log": steps,
                "messages": list(state.get("messages", [])) + [{
                    "role": "assistant",
                    "content": assistant_msg,
                    "time": get_timestamp(),
                }],
                "iteration": state.get("iteration", 0) + 1,
            }
        if verification.get("skipped"):
            assistant_msg = (
                f"**Could not divert** `{bottleneck}`: the controller's live graph "
                f"offers no alternate path (or the controller state is unavailable). "
                f"{verification.get('summary')}"
            )
            return {
                "verification": verification,
                "status": "unavailable",
                "steps_log": steps,
                "messages": list(state.get("messages", [])) + [{
                    "role": "assistant",
                    "content": assistant_msg,
                    "time": get_timestamp(),
                }],
                "iteration": state.get("iteration", 0) + 1,
            }
        else:
            assistant_msg = (
                f"Closed-loop verification on `{bottleneck}`: **{verdict}**. "
                f"Before: {verification.get('before_load_pct')}% · "
                f"Latest: {verification.get('current_load_pct')}% · "
                f"{verification.get('consecutive_below', 0)}/{verification.get('consecutive_required', 3)} "
                f"consecutive samples below threshold."
            )

        messages = list(state.get("messages", []))
        messages.append({"role": "assistant", "content": assistant_msg, "time": get_timestamp()})

        return {
            "verification": verification,
            "status": "completed" if passed else "failed",
            "steps_log": steps,
            "messages": messages,
            "iteration": state.get("iteration", 0) + 1,
        }

    # --- Conditional Edge Logic ---------------------------------------------
    def check_congestion_edge(self, state: AgentState) -> Literal["analyze", "healthy_end"]:
        if state.get("status") == "unavailable":
            # Blind network — monitor already wrote the honest message; stop here.
            return "healthy_end"
        if state.get("congestion_detected"):
            return "analyze"
        if state.get("hazard"):
            return "analyze"
        return "healthy_end"

    # --- Graph Assembly -----------------------------------------------------
    def _build_graph(self) -> StateGraph:
        builder = StateGraph(AgentState)

        builder.add_node("monitor", self.monitor_node)
        builder.add_node("analyze", self.analyze_node)
        builder.add_node("plan", self.plan_node)
        builder.add_node("act", self.act_node)
        builder.add_node("verify", self.verify_node)

        builder.add_edge(START, "monitor")
        builder.add_conditional_edges(
            "monitor",
            self.check_congestion_edge,
            {"analyze": "analyze", "healthy_end": END},
        )
        builder.add_edge("analyze", "plan")
        builder.add_edge("plan", "act")
        builder.add_edge("act", "verify")
        builder.add_edge("verify", END)
        return builder

    async def run(self, initial_state: AgentState) -> AgentState:
        """Execute the compiled LangGraph workflow asynchronously."""
        run_input = dict(initial_state)
        run_input["steps_log"] = []
        return await self.app.ainvoke(run_input)
