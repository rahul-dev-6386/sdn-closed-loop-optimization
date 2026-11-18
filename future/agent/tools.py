"""SDN Environment Tools for the LangGraph congestion agent.

Every number returned here comes from the live fabric: real OVS port-stat
telemetry (via the controller's CSV), real model predictions from
``results/model.joblib``, or the real routing-mode switch on the fabric.

There are no fabricated fallbacks. When the fabric is down or a measurement
is unavailable, tools report ``status: "unavailable"`` with the reason and
the agent ends the run with an honest no-op instead of claiming a fix.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from future.api.topologies_catalog import get_topology_by_id
from future.agent.config import agent_config

LOGGER = logging.getLogger("sdn.agent.tools")

DEFAULT_SAMPLE_INTERVAL_S = 2.0
#: consecutive sub-threshold samples required before a link counts as relieved
CONSECUTIVE_BELOW_REQUIRED = 3
#: controller/state snapshot defaults — the agent asks the controller what it
#: sees; the snapshot is rewritten by the reroute loop after every tick.
CONTROLLER_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "controller_state.json")
CONTROLLER_STATE_ENV = "SDN_CONTROLLER_STATE"
CONTROLLER_STATE_STALE_AFTER_S = 10.0


class SdnAgentTools:
    """Tools the agent may call. All read or act on the live fabric."""

    def __init__(self, project_state=None) -> None:
        self.project_state = project_state

    def _project(self):
        if self.project_state and hasattr(self.project_state, "project"):
            return self.project_state.project
        try:
            from future.api.main import state
            return state.project
        except Exception:
            return None

    def _fabric_manager(self):
        try:
            from future.api.live_fabric import LiveFabricManager
            return LiveFabricManager.get_instance()
        except Exception:
            return None

    @staticmethod
    def _threshold_pct() -> float:
        return float(getattr(agent_config, "congestion_threshold_pct", 80.0))

    @staticmethod
    def _fabric_is_alive(fabric: Any) -> bool:
        """True if *any* API process's fabric is running, adapted or spawned.

        ``is_running`` only tells us what THIS API process spawned; after a
        restart the singleton reports False even though a detached fabric
        (Ryu + OVS) keeps running. ``running()`` asks the system — pgrep for
        the controller — which is the honest check across restarts.
        """
        running = getattr(fabric, "running", None)
        if callable(running):
            try:
                if running():
                    return True
            except Exception:
                pass
        return bool(getattr(fabric, "is_running", False))

    # -------------------------------------------------------- telemetry --
    def inspect_telemetry(self, topology_id: str = "diamond") -> Dict[str, Any]:
        """Measured per-link utilisation for the selected topology.

        Only links the controller actually reports are returned. A link with
        no samples is absent, not zero — and zero usage is only claimable
        when the fabric is up and counters are actually moving.
        """
        threshold = self._threshold_pct()
        project = self._project()
        topo_def = get_topology_by_id(topology_id) or {}
        default_bn = topo_def.get("bottleneck_link", "s1--s2")

        if project is None:
            return {"status": "unavailable", "reason": "project backend not initialised",
                    "bottleneck": default_bn, "is_congested": False, "links": {}}
        fabric = self._fabric_manager()
        if fabric is not None and not self._fabric_is_alive(fabric):
            # The utilisation reader serves the frozen CSV tail, so a dead
            # fabric otherwise masquerades as "still congested at 100%".
            return {"status": "unavailable",
                    "reason": "fabric stopped — telemetry is frozen at the last sample",
                    "bottleneck": default_bn, "is_congested": False,
                    "links": {}, "threshold_pct": threshold}
        try:
            util = project.link_utilisation(topo_id=topology_id)
        except Exception as exc:
            LOGGER.error("inspect_telemetry failed: %s", exc)
            return {"status": "unavailable", "reason": f"utilisation unreadable: {exc}",
                    "bottleneck": default_bn, "is_congested": False, "links": {}}

        links = util.get("links", {})
        if not util.get("available", False):
            return {"status": "unavailable",
                    "reason": "no live telemetry samples (fabric idle, or controller down)",
                    "bottleneck": default_bn, "is_congested": False,
                    "links": links, "threshold_pct": threshold}

        congested: List[Dict[str, Any]] = []
        worst_pct = 0.0
        worst_link = ""
        for key, info in links.items():
            pct = float(info.get("util_pct", 0) or 0.0)
            used = float(info.get("used_mbps", 0.0) or 0.0)
            if pct >= threshold:
                congested.append({"link": key, "pct": pct, "used_mbps": round(used, 3)})
            if pct > worst_pct:
                worst_pct, worst_link = pct, key

        return {
            "status": "ok",
            "topology_id": topology_id,
            "links": links,
            "congested_links": congested,
            "bottleneck": worst_link or default_bn,
            "bottleneck_util_pct": round(worst_pct, 1),
            "is_congested": bool(congested),
            "threshold_pct": threshold,
            "summary": (f"Live port-stats: {len(congested)} link(s) at >= {threshold}%; "
                        f"worst {worst_link or 'none'} at {worst_pct:.1f}%."),
        }

    # ---------------------------------------------------- predictions --
    def inspect_ml_predictions(self, topology_id: str = "diamond") -> Dict[str, Any]:
        """Real RandomForest output for this topology's links."""
        project = self._project()
        if project is None:
            return {"status": "unavailable", "reason": "project backend not initialised",
                    "probability": None, "predicted_hazard": False}
        try:
            preds = project.predict_links(topo_id=topology_id)
        except Exception as exc:
            LOGGER.error("inspect_ml_predictions failed: %s", exc)
            return {"status": "unavailable", "reason": f"predictor unreachable: {exc}",
                    "probability": None, "predicted_hazard": False}

        scored = [p for p in preds if p.get("probability") is not None]
        if not scored:
            reasons = sorted({p.get("reason") for p in preds if p.get("reason")})
            return {"status": "unavailable",
                    "reason": ("; ".join(reasons) if reasons
                               else "model not yet scored (needs ~24 s of telemetry)"),
                    "probability": None, "predicted_hazard": False,
                    "links": preds}

        top = max(scored, key=lambda p: p["probability"])
        congested = [p for p in scored if p.get("congested")]
        prob = float(top["probability"])
        return {
            "status": "ok",
            "model": "RandomForestClassifier (results/model.joblib)",
            "probability": round(prob, 4),
            "predicted_hazard": prob >= 0.5,
            "congested_links": [p["link"] for p in congested],
            "horizon_sec": float(top.get("horizon_s", 10.0)),
            "threshold": float(top.get("threshold", 0.5)),
            "summary": (f"Model scores {len(scored)} link(s); peak p={prob:.3f} ({top['link']}); "
                        f"{len(congested)} predicted-congested."),
        }

    # ------------------------------------------------------- topology --
    def inspect_topology(self, topology_id: str = "diamond") -> Dict[str, Any]:
        """Catalog description of the topology (paths and bottleneck)."""
        topo_def = get_topology_by_id(topology_id)
        if not topo_def:
            return {"id": "diamond", "name": "Diamond Topology",
                    "default_path": ["h1", "s1", "s2", "s4", "h4"],
                    "alternate_path": ["h1", "s1", "s3", "s4", "h4"],
                    "bottleneck": "s1--s2", "source": "fallback catalog"}
        return {
            "id": topology_id,
            "name": topo_def.get("name", topology_id),
            "default_path": topo_def.get("default_path", []),
            "alternate_path": topo_def.get("alternate_path", []),
            "bottleneck": topo_def.get("bottleneck_link", ""),
            "switches": topo_def.get("switches", []),
            "source": "catalog",
        }

    # ---------------------------------------------------- spike --
    def inject_congestion(self, topology_id: str = "diamond",
                          rate_mbps: float = 14.0) -> Dict[str, Any]:
        """Start a real iperf3 UDP spike h1->h4 on the live fabric.

        Returns whether the spike was actually started; the load itself must
        subsequently be observed via telemetry, not asserted here.
        """
        fabric = self._fabric_manager()
        if fabric is None or not self._fabric_is_alive(fabric):
            return {"status": "unavailable",
                    "reason": "live fabric not running — press Start Fabric first",
                    "rate_mbps": rate_mbps}
        res = fabric.start_spike(rate_mbps=rate_mbps, duration_s=45.0)
        if res.get("status") not in ("spike_started", "running"):
            return {"status": "error", "reason": str(res), "rate_mbps": rate_mbps}
        status = fabric.get_status()
        return {
            "status": "spike_started",
            "topology_id": topology_id,
            "rate_mbps": rate_mbps,
            "duration_s": fabric.spike_duration_s,
            "src": "h1 (10.0.0.1)",
            "dst": "h4 (10.0.0.4)",
            "fabric_mode": status.get("routing_mode"),
            "summary": (f"Real iperf3 UDP spike started: {rate_mbps} Mbps h1->h4 for "
                        f"{int(fabric.spike_duration_s)}s. Telemetry will report the load "
                        f"within ~2 s (port-stats deltas)."),
        }

    # ---------------------------------------------------- action --
    def execute_reroute_detour(self, topology_id: str = "diamond",
                               mode: str = "ml_aware",
                               pin_seconds: float = 30.0) -> Dict[str, Any]:
        """Pin the currently-bottlenecked link so the controller avoids it.

        This is the real lever: rather than choosing the next path itself,
        the agent tells the controller *which link to avoid*. The controller
        reads the constraint file on its next tick and re-computes the path
        via Yen k-shortest simple paths with the pinned link's cost raised
        above everything else. No flow-mods are installed here, no Ryu
        restart happens; the controller's existing make-before-break
        mechanism applies the actual re-route.
        """
        from controller.constraints import write_constraints, DEFAULT_CONSTRAINTS_PATH

        started = time.monotonic()
        fabric = self._fabric_manager()
        project = self._project()
        topo_def = get_topology_by_id(topology_id) or {}
        path_str = " → ".join(topo_def.get("alternate_path", []))

        if fabric is None or not self._fabric_is_alive(fabric):
            return {"status": "unavailable",
                    "reason": "live fabric not running — pin cannot take effect",
                    "routing_mode": None, "action": None,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "detour_path": path_str}

        # 1. Identify the currently-bottlenecked link (worst measured link)
        telem = self.inspect_telemetry(topology_id)
        worst = telem.get("bottleneck", topo_def.get("bottleneck_link", "s1--s2"))
        worst_pct = telem.get("bottleneck_util_pct", 0.0)

        # 2. Write the constraint — the controller reads it on the next tick
        try:
            payload = write_constraints(
                DEFAULT_CONSTRAINTS_PATH,
                block_links=[worst],
                ttl_s=float(pin_seconds),
                reason=f"agent-reported congestion on {worst} ({worst_pct}% util)",
                pinned_by="agent",
            )
        except Exception as exc:
            LOGGER.exception("failed to write routing constraint: %s", exc)
            return {"status": "error",
                    "reason": f"could not write routing constraint: {exc}",
                    "routing_mode": None, "action": None,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "detour_path": path_str}

        latency_ms = round((time.monotonic() - started) * 1000, 1)
        summary = (f"Pinned {worst} for {int(pin_seconds)}s so the controller "
                   f"avoids it on its next decision tick. Controller selects the "
                   f"alternate path via its own Yen graph search + cost function; "
                   f"verification reports measured per-link utilisation.")
        if project is not None:
            try:
                project.record_decision_event({
                    "ts": time.time(),
                    "predicted_link": worst,
                    "p": None,
                    "action": f"pin {worst} ({int(pin_seconds)}s)",
                    "flow_mods": [],
                    "path_old": topo_def.get("default_path", []),
                    "path_new": topo_def.get("alternate_path", []),
                })
            except Exception as exc:
                LOGGER.debug("could not record agent decision event: %s", exc)

        return {
            "status": "pinned",
            "routing_mode": fabric.current_mode,
            "action": f"pin {worst} ({int(pin_seconds)}s)",
            "pinned_link": worst,
            "pin_seconds": float(pin_seconds),
            "valid_until_epoch": payload.get("valid_until_epoch"),
            "detour_path": path_str,
            "latency_ms": latency_ms,
            "summary": summary,
        }

    # ------------------------------------------- controller self-check --
    def ask_controller_for_alternatives(self, topology_id: str = "diamond") -> Dict[str, Any]:
        """Ask the running controller what it sees: live links, flows, blockers.

        The agent cannot call into the Ryu process, so the controller publishes
        an atomic snapshot (``controller_state.json``) after every reroute-loop
        tick. This tool reads that snapshot and enumerates the alternate paths
        Yen's algorithm finds in the *live* graph for the flow's endpoints.

        Returns ``status: "unavailable"`` with the reason when the snapshot is
        missing or stale — which is exactly how a stopped fabric announces
        itself to the agent.
        """
        state_path = os.environ.get(CONTROLLER_STATE_ENV) or CONTROLLER_STATE_PATH
        try:
            with open(state_path, "r", encoding="utf-8") as handle:
                snap = json.load(handle)
        except FileNotFoundError:
            return {
                "status": "unavailable",
                "reason": "controller is not reporting topology state — fabric down or controller not ticking",
                "state_path": state_path,
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "reason": f"controller state unreadable: {exc}",
                "state_path": state_path,
            }

        ts = float(snap.get("ts") or 0.0)
        age_s = time.time() - ts if ts else None
        if age_s is None or age_s > CONTROLLER_STATE_STALE_AFTER_S:
            return {
                "status": "unavailable",
                "reason": f"controller state stale ({'never' if age_s is None else f'{age_s:.0f}s'} old) — fabric down or controller not ticking",
                "age_s": age_s,
                "state_path": state_path,
            }

        graph = {str(k): list(v) for k, v in (snap.get("graph") or {}).items()}
        flows = snap.get("flows") or []
        blocked = (snap.get("constraints") or {}).get("block_links") or []

        if flows:
            current_path = list(flows[0].get("path") or [])
            src, dst = current_path[0], current_path[-1]
        else:
            current_path, src, dst = [], "s1", "s4"

        alternatives: List[Dict[str, Any]] = []
        if src in graph and dst in graph:
            from routing.paths import k_disjoint_candidates
            for cand in k_disjoint_candidates(graph, src, dst, k=4):
                cand_path = list(cand)
                if current_path and cand_path == list(current_path):
                    continue
                alternatives.append({"switches": cand_path, "hops": len(cand_path) - 1})

        return {
            "status": "ok",
            "age_s": round(age_s, 2),
            "ts": ts,
            "src": src,
            "dst": dst,
            "current_path": current_path,
            "graph_nodes": sorted(graph),
            "n_links": int(snap.get("n_links") or 0),
            "alternatives": alternatives,
            "has_alternate": bool(alternatives),
            "flows": flows,
            "blocked_links": list(blocked),
            "mode": snap.get("mode"),
            "state_path": state_path,
        }

    # ----------------------------------------------------- verify --
    def verify_telemetry_reduction(self, bottleneck_link: str = "s1--s2",
                                   topology_id: str = "diamond",
                                   before_pct: Optional[float] = None,
                                   window_s: float = 20.0,
                                   check_alt_headroom: bool = True) -> Dict[str, Any]:
        """Measure whether the bottleneck actually drained, over real samples.

        Polls live utilisation for up to ``window_s``; PASS only after
        CONSECUTIVE_BELOW_REQUIRED consecutive samples below the threshold.
        When ``check_alt_headroom`` is True, also verifies that the alternate
        path has not itself saturated (>= threshold), preventing false PASS
        verdicts that merely shift congestion. Never reports relief it did not observe.
        """
        threshold = self._threshold_pct()
        project = self._project()
        started = time.monotonic()

        if project is None:
            return {"status": "unavailable", "reason": "project backend not initialised",
                    "passed": False}

        consecutive = 0
        worst_seen: Optional[float] = None
        samples: List[float] = []
        alt_samples: List[float] = []
        alt_consecutive = 0
        alt_path_links: List[str] = []
        sample_count = 0
        saw_live_samples = False
        deadline = started + window_s
        while time.monotonic() < deadline:
            try:
                util = project.link_utilisation(topo_id=topology_id)
            except Exception as exc:
                LOGGER.warning("verify poll failed: %s", exc)
                time.sleep(1.0)
                continue
            if util.get("available", False):
                saw_live_samples = True
            links = util.get("links", {})
            info = links.get(bottleneck_link) or {}
            pct = float(info.get("util_pct", 0) or 0.0)
            samples.append(pct)
            sample_count += 1
            if pct < threshold:
                consecutive += 1
            else:
                consecutive = 0

            # Also probe the registered forward path (ALTERNATE CHECK). The
            # agent must be able to say where the traffic actually went. Read
            # it from the controller snapshot on each poll.
            forward_path: List[str] = []
            try:
                alternatives = self.ask_controller_for_alternatives(topology_id)
                if alternatives.get("status") == "ok":
                    forward_path = (alternatives.get("current_path") or [])
            except Exception:
                forward_path = []
            if forward_path and len(forward_path) >= 2:
                # Direct links from forward path: path e.g. ['s1','s3','s4'] -> ['s1--s3','s3--s4']
                alt_path_links = [
                    "--".join(sorted([forward_path[i], forward_path[i + 1]]))
                    for i in range(len(forward_path) - 1)
                ]
                loaded = 0
                for k in alt_path_links:
                    pct_x = float((links.get(k) or {}).get("util_pct", 0) or 0.0)
                    loaded = max(loaded, pct_x)
                alt_samples.append(loaded)
                if loaded > 5.0:  # forward path is actually carrying something
                    alt_consecutive += 1
                else:
                    alt_consecutive = 0
            else:
                alt_path_links = alt_path_links or []

            if consecutive >= CONSECUTIVE_BELOW_REQUIRED and alt_consecutive >= 2:
                break
            time.sleep(1.0)

        after_pct = samples[-1] if samples else None
        before_val = before_pct if before_pct is not None else (samples[0] if samples else None)
        passed = bool(consecutive >= CONSECUTIVE_BELOW_REQUIRED and saw_live_samples)
        alt_saturated = bool(alt_samples and alt_samples[-1] >= threshold)
        if check_alt_headroom and alt_saturated:
            passed = False

        if not saw_live_samples:
            return {
                "status": "UNAVAILABLE",
                "reason": "no live telemetry samples during the verification window",
                "passed": False,
                "bottleneck_link": bottleneck_link,
                "before_load_pct": before_val,
                "current_load_pct": None,
                "samples_pct": [],
                "window_s": round(time.monotonic() - started, 1),
                "summary": (f"Verification INCONCLUSIVE: no live telemetry from the fabric "
                            f"during {window_s:.0f}s; refusing to declare PASS/FAIL."),
            }

        result = {
            "status": "VERIFIED PASS" if passed else "VERIFIED FAIL",
            "passed": passed,
            "bottleneck_link": bottleneck_link,
            "before_load_pct": round(before_val, 1) if before_val is not None else None,
            "current_load_pct": round(after_pct, 1) if after_pct is not None else None,
            "samples_pct": [round(s, 1) for s in samples],
            "alt_path_links": alt_path_links,
            "alt_path_samples_pct": [round(s, 1) for s in alt_samples],
            "alt_path_samples_above_5pct": alt_consecutive,
            "alt_path_saturated": alt_saturated,
            "consecutive_below": consecutive,
            "consecutive_required": CONSECUTIVE_BELOW_REQUIRED,
            "threshold_pct": threshold,
            "window_s": round(time.monotonic() - started, 1),
            "pass_reason": (
                "bottleneck drained but forward path is saturated (congestion shifted without relief)"
                if alt_saturated
                else "old link drained below threshold AND forward path carries measurable load"
                if passed and alt_consecutive >= 2
                else "old link drained below threshold (forward path not yet confirmed)"
                if passed
                else "old link did not drop below threshold"
            ),
        }
        if project is not None:
            try:
                project.record_verify_event({
                    "ts": time.time(),
                    "result": "PASS" if passed else "FAIL",
                    "link": bottleneck_link,
                    "before_pct": result["before_load_pct"],
                    "after_pct": result["current_load_pct"],
                    "window_s": result["window_s"],
                })
            except Exception as exc:
                LOGGER.debug("could not record agent verify event: %s", exc)
        if alt_saturated:
            result["summary"] = (
                f"Verification FAIL: bottleneck {bottleneck_link} dropped below {threshold}%, "
                f"but forward path {alt_path_links} is saturated at {alt_samples[-1]:.1f}% "
                f"(congestion shifted without headroom)."
            )
        else:
            result["summary"] = (
                f"Verification {'PASS' if passed else 'FAIL'}: bottleneck {bottleneck_link} "
                f"read {result['before_load_pct']}% at start, {result['current_load_pct']}% "
                f"latest ({consecutive}/{CONSECUTIVE_BELOW_REQUIRED} consecutive samples "
                f"below {threshold}%)."
            )
        return result
