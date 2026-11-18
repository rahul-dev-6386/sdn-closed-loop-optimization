"""Wire the P9 reroute loop into the Ryu controller (P9).

The decision logic lives in :mod:`controller.reroute_loop` as a plain object
driven by two callables, so it can be tested without a fabric. This module is the
adapter that supplies those callables against a live controller:

* ``snapshot`` reads the telemetry collector's current link records,
* ``apply`` turns a :class:`routing.reroute.Operation` into real flow-table
  changes through :class:`controller.flow_manager.FlowProgrammer`.

It also owns the decision thread. Keeping the timer here rather than inside the
loop means the loop itself stays a pure function of its inputs, and a test can
drive it synchronously.

Why this is separate
--------------------
Without this wiring ``routing.mode`` would be a configuration value that changes
nothing, and P10's four-mode comparison would measure four identical
experiments. The comparison is the point of P10, so the loop must actually be
reachable from the configuration.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ryu.lib import hub  # noqa: E402

from controller.reroute_loop import LoopConfig, RerouteLoop  # noqa: E402
from routing.reroute import Operation, RerouteError  # noqa: E402

LOGGER = logging.getLogger("controller.reroute_integration")

#: Environment variable holding a path the loop's summary report is written to
#: after every tick. The P10 experiment harness sets this per run so it can
#: record what the loop actually did (effective mode, fallbacks, degraded
#: ticks, reroutes planned) instead of assuming the configured mode held.
REPORT_ENV_VAR = "SDN_LOOP_REPORT"

#: Environment variable (or default) for the controller's topology state
#: snapshot — links, graph adjacency, registered flows — which the agent
#: reads to ask "is there an alternate path right now?". Written atomically
#: after every tick.
STATE_ENV_VAR = "SDN_CONTROLLER_STATE"
CONTROLLER_STATE_PATH = "data/controller_state.json"
STATE_STALE_AFTER_S = 10.0


def make_snapshot_fn(
    telemetry: Any,
) -> Callable[[], Sequence[Mapping[str, Any]]]:
    """Build the ``snapshot`` callable from a telemetry collector.

    Each link is reported once, using the collector's latest record. Stale
    records are included rather than filtered here: :mod:`ml.predict` detects
    staleness itself and reports ``reason="stale"``, and duplicating that rule in
    two places is how they drift apart.
    """

    def snapshot() -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for (src, dst, direction), record in telemetry.latest_link_records.items():
            # Every field the P8 predictor's allowlist needs must be present,
            # or the feature builder marks each window unusable and every link
            # silently reports "missing_features" -- an inert forecast that
            # looks identical to a healthy ml_aware run.
            rows.append({
                "timestamp": record.timestamp,
                "src_switch": src,
                "dst_switch": dst,
                "direction": direction,
                "utilization": record.utilization,
                "latency_ms": record.latency_ms,
                "loss_pct": record.loss_pct,
                "tx_rate_mbps": record.tx_rate_mbps,
                # getattr with None: a record missing the allowlisted fields is
                # reported by the predictor as unscored, never as zero-load.
                "rx_rate_mbps": getattr(record, "rx_rate_mbps", None),
                "tx_packet_rate_pps": getattr(record, "tx_packet_rate_pps", None),
                "rx_packet_rate_pps": getattr(record, "rx_packet_rate_pps", None),
                "capacity_mbps": record.capacity_mbps,
                "is_congested": record.is_congested,
            })
        return rows

    return snapshot


def make_flow_applier(
    programmer: Any,
    port_of: Callable[[str, str], Optional[int]],
    host_port_of: Optional[Callable[[str], Optional[int]]] = None,
) -> Callable[[Operation], bool]:
    """Build an ``apply`` callable that installs real exact-match flows.

    Parameters
    ----------
    programmer:
        A :class:`controller.flow_manager.FlowProgrammer`.
    port_of:
        ``port_of(a, b)`` -> the port on ``a`` facing ``b``, or ``None``.
    host_port_of:
        ``host_port_of(switch)`` -> that switch's port facing its host. Needed
        for the first switch of a path (where the packet arrives from a host
        rather than from another switch) and for the last (where it leaves to
        one).

    Notes
    -----
    **One rule per switch, not one per operation.** A path is expressed as a
    chain of exact-match rules: at each switch, match the port the packet
    arrived on and send it out the port facing the next hop. So an
    ``install_ingress`` and an ``install_egress`` naming the same intermediate
    switch resolve to the *same* rule -- the plan distinguishes them because it
    controls ordering during the make-before-break overlap, not because they are
    different rules. Installing the same rule twice is idempotent.

    Operations that cannot be resolved to a concrete rule return ``False``,
    which triggers the plan's rollback rather than installing a partial one.
    """
    from controller.flow_manager import FlowSpec

    # Where each flow is. Ports cannot be resolved without it, since an
    # operation names a switch but not a port.
    #
    # BOTH paths are held, because a single make-before-break plan touches each
    # one: installs resolve against the new path, deletes against the old.
    # Keeping only the old path -- which an earlier version of this adapter did
    # -- makes every install unresolvable, so every reroute fails and rolls back.
    context: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {}

    _FLOW_MAC_RE = re.compile(r"(?:^|:)([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})$")

    def _flow_dst_mac(flow: str) -> Optional[str]:
        match = _FLOW_MAC_RE.search(flow or "")
        return match.group(1) if match else None

    def set_path(flow: str, path: Sequence[str]) -> None:
        """Record a flow's steady-state path (no reroute in flight)."""
        path = tuple(path)
        context[flow] = (path, path)

    def set_plan(flow: str, old_path: Sequence[str],
                 new_path: Sequence[str]) -> None:
        """Record both paths for a reroute in flight."""
        context[flow] = (tuple(old_path), tuple(new_path))

    def _host_port(switch: str) -> Optional[int]:
        return host_port_of(switch) if host_port_of else None

    def apply(op: Operation) -> bool:
        flow = str((op.match or {}).get("flow", "") or "")
        paths = context.get(flow)
        if not paths or len(paths[0]) < 2:
            LOGGER.debug("no path context for flow %r; refusing %s on %s",
                         flow, op.kind, op.switch)
            return False
        old_path, new_path = paths
        # Installs belong to the path being installed; deletes to the path being
        # removed. Conflating the two resolves operations against the wrong
        # topology.
        path = new_path if op.is_install else old_path
        try:
            index = path.index(op.switch)
        except ValueError:
            LOGGER.debug("%s names %s, which is not on the %s path",
                         op.kind, op.switch, "new" if op.is_install else "old")
            return False

        last = len(path) - 1
        if index == 0:
            # The packet arrives from the host, not from another switch.
            in_port = _host_port(path[0])
            out_port = port_of(path[0], path[1])
        elif index == last:
            in_port = port_of(path[index], path[index - 1])
            out_port = _host_port(path[index])
        else:
            in_port = port_of(path[index], path[index - 1])
            out_port = port_of(path[index], path[index + 1])

        if in_port is None or out_port is None:
            LOGGER.debug("cannot resolve ports for %s on %s "
                         "(in=%s out=%s)", op.kind, op.switch, in_port, out_port)
            return False

        dst_mac = _flow_dst_mac(flow)
        match = {"in_port": in_port}
        if dst_mac is not None:
            match["eth_dst"] = dst_mac
        spec = FlowSpec(switch=op.switch, match=match,
                        out_port=out_port, priority=op.priority,
                        idle_timeout=0, hard_timeout=0,
                        reason=f"reroute {flow}")
        if op.is_install:
            return bool(programmer.install(spec))
        return bool(programmer.delete(spec))

    apply.set_path = set_path  # type: ignore[attr-defined]
    apply.set_plan = set_plan  # type: ignore[attr-defined]
    return apply


class RerouteIntegration:
    """Owns the decision thread and the loop's lifecycle inside the controller.

    Parameters
    ----------
    controller:
        The :class:`controller.sdn_controller.SdnController` instance.
    """

    def __init__(self, controller: Any) -> None:
        self.controller = controller
        self.config = LoopConfig.from_config(controller.config)
        self._applier = None
        self.loop: Optional[RerouteLoop] = None
        self.thread = None
        self._stopped = False
        self.report_path: Optional[str] = os.environ.get(REPORT_ENV_VAR) or None
        from controller.constraints import DEFAULT_CONSTRAINTS_PATH
        self.constraints_path: Optional[str] = (
            os.environ.get("SDN_CONSTRAINTS") or DEFAULT_CONSTRAINTS_PATH)

    # ------------------------------------------------------------- setup --
    def start(self) -> None:
        """Build the loop and spawn the decision thread."""
        if self.config.mode == "static_shortest":
            LOGGER.info("routing mode is static_shortest; no decision thread")
            self._publish_static_state()
            self._write_report()
            return

        telemetry = self.controller.telemetry
        programmer = self.controller.flows
        if programmer is None:
            LOGGER.warning("no flow programmer yet; reroute loop not started")
            self._write_report()
            return

        self._applier = make_flow_applier(
            programmer, self.controller.port_of,
            self.controller.host_port_of,
        )
        predictor = None
        if self.config.mode == "ml_aware":
            predictor = self._load_predictor()
            if predictor is None:
                LOGGER.warning(
                    "ml_aware selected but no model could be loaded; the loop "
                    "will degrade to reactive_threshold and record it")

        self.loop = RerouteLoop(
            snapshot=make_snapshot_fn(telemetry),
            apply=self._applier,
            constraints_path=self.constraints_path,
            predictor=predictor,
            stalled=telemetry.is_telemetry_stalled,
            config=self.config,
            pre_plan=self._sync_plan_context,
        )
        self.loop.observe_links(self._discovered_links())
        self._write_report()
        self.thread = hub.spawn(self._decision_loop)
        LOGGER.info("reroute loop started in %s mode", self.config.mode)

    def _load_predictor(self) -> Optional[Any]:
        """Load the trained model, or ``None`` if it is absent or unusable."""
        try:
            from ml.predict import CongestionPredictor

            return CongestionPredictor.from_config(self.controller.config)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("could not load the congestion predictor: %s", exc)
            return None

    def _discovered_links(self) -> List[Tuple[str, str]]:
        """Inter-switch links from the live topology discovery."""
        links: List[Tuple[str, str]] = []
        for link in self.controller.topology.links.values():
            pair = (link.src_switch, link.dst_switch)
            if pair not in links:
                links.append(pair)
        return links

    # -------------------------------------------------------------- loop --
    def _decision_loop(self) -> None:
        """Tick the loop on its configured interval until stopped."""
        interval = max(0.5, self.config.decision_interval_s)
        while not self._stopped:
            hub.sleep(interval)
            if self._stopped or self.loop is None:
                break
            try:
                # The graph must track whatever LLDP discovers *now*, not
                # whatever it looked like when the loop started -- discovery
                # lags fabric bring-up, and a stale graph makes every candidate
                # enumeration return empty, silently disabling rerouting.
                self.loop.observe_links(self._discovered_links())
                tick_report = self.loop.tick()
                import sys as _sys
                n_flows = len(self.loop.controller.flows)
                first_flows = list(self.loop.controller.flows)[:2]
                print(f"TICK n_flows={n_flows} effective={tick_report.effective_mode} flows={first_flows}", file=_sys.stderr)
                self.publish_forecasts()
                self._sync_paths()
                self._publish_state(tick_report)
            except RerouteError as exc:
                LOGGER.debug("reroute tick skipped: %s", exc)
            except Exception as exc:  # noqa: BLE001
                # A failure here must never take the controller down; the next
                # tick retries, and the loop records its own degradation.
                LOGGER.error("reroute tick failed: %s", exc)
            self._write_report()

    def publish_forecasts(self) -> int:
        """Copy the loop's latest forecasts into the telemetry collector.

        Without this the predictor's output never leaves the loop, so the
        telemetry CSV has no record of a forecast and neither the demo nor the
        experiment harness can correlate a prediction with what followed it.

        Returns the number of links that carried a usable forecast.
        """
        if self.loop is None:
            return 0
        latest = getattr(self.loop, "last_forecasts", None) or {}
        return self.controller.telemetry.apply_forecasts(latest)

    def _sync_paths(self) -> None:
        """Keep the flow applier's path context in step with the loop."""
        if self.loop is None or self._applier is None:
            return
        for flow, state in self.loop.controller.flows.items():
            self._applier.set_path(flow, state.path)  # type: ignore[attr-defined]

    def _sync_plan_context(self, plan: Any) -> None:
        """Give the applier both paths before a plan executes.

        Without this the applier resolves installs against the flow's *current*
        (old) path, so every install is unresolvable, the plan rolls back, and
        no reroute ever succeeds -- while the decision log cheerfully records a
        reroute each time.
        """
        if self._applier is None or plan.old_path is None:
            return
        self._applier.set_plan(plan.flow, plan.old_path,  # type: ignore[attr-defined]
                               plan.new_path)

    # --------------------------------------------------------- lifecycle --
    def register(self, flow: str, path: Sequence[str]) -> None:
        """Tell the loop and the applier where a flow is."""
        if self.loop is not None:
            self.loop.register_flow(flow, tuple(path))
        if self._applier is not None:
            self._applier.set_path(flow, path)  # type: ignore[attr-defined]

    def stop(self) -> None:
        """Stop the decision thread."""
        self._stopped = True
        self._write_report()

    def _write_report(self) -> None:
        """Write the loop's summary to ``$SDN_LOOP_REPORT``, atomically.

        No-op unless the harness (or an operator) asked for the file. Never
        raises: a report-writing failure must not take the control loop down.
        """
        if not self.report_path:
            return
        try:
            payload = {"ts": time.time(), "started": self.loop is not None,
                       "mode": self.config.mode,
                       "configured_mode": self.config.mode}
            if self.loop is not None:
                payload.update(self.loop.mode_report())
            tmp = self.report_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.report_path)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("could not write loop report to %s: %s",
                           self.report_path, exc)

    def _publish_static_state(self) -> None:
        """One-shot snapshot for static_shortest (no decision loop exists).

        The graph is still reported so the agent's alternate-path query works;
        without it the agent would conclude the fabric is down when it is merely
        in a non-deciding mode.
        """
        try:
            from routing.paths import build_graph
            adjacency = build_graph(self._discovered_links())
            payload = {
                "ts": time.time(),
                "mode": "static_shortest",
                "effective_mode": "static_shortest",
                "graph": adjacency,
                "links": [{"a": a, "b": b} for a, b in self._discovered_links()],
                "n_links": len(self._discovered_links()),
                "flows": [],
                "constraints": {"block_links": [], "valid_until_epoch": 0, "reason": ""},
            }
            env_path = os.environ.get(STATE_ENV_VAR) or CONTROLLER_STATE_PATH
            state_path = os.path.abspath(env_path)
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            tmp = state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, state_path)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("could not publish static controller state: %s", exc)

    def _publish_state(self, tick_report: Any = None) -> None:
        """Write the topology/flows snapshot the agent asks about.

        The agent cannot call into the controller process, but it needs the
        same three facts the decision loop uses: which links LLDP currently
        sees, which flows are registered and where they ride, and which links
        are pinned to infinity by an operator/agent hint. Published as JSON,
        atomically, every tick. Never raises — a missing snapshot means
        "fabric down or controller not ticking", which the agent reports.
        """
        if self.loop is None:
            return
        try:
            from controller.constraints import read_constraints
            constraints = read_constraints(self.constraints_path)
            flows = [
                {"flow": f, "path": list(state.path)}
                for f, state in self.loop.controller.flows.items()
            ]
            adjacency = {node: list(neigh) for node, neigh in self.loop.graph.items()}
            payload = {
                "ts": time.time(),
                "mode": self.config.mode,
                "effective_mode": getattr(tick_report, "effective_mode", None),
                "graph": adjacency,
                "links": [{"a": a, "b": b} for a, b in self._discovered_links()],
                "n_links": len(self._discovered_links()),
                "flows": flows,
                "constraints": {
                    "block_links": constraints.get("block_links") or [],
                    "valid_until_epoch": constraints.get("valid_until_epoch") or 0,
                    "reason": constraints.get("reason") or "",
                },
            }
            env_path = os.environ.get(STATE_ENV_VAR) or CONTROLLER_STATE_PATH
            state_path = os.path.abspath(env_path)
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            tmp = state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, state_path)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("could not publish controller state snapshot: %s", exc)

    def report(self) -> Dict[str, Any]:
        """Current loop status for the API and the acceptance tests.

        Always carries ``mode``, whether or not the loop started, so a caller
        never has to branch on ``started`` just to learn which mode is
        configured. ``mode_report`` also spells it ``configured_mode``, because
        it is reporting intent rather than the effective mode; both appear so
        neither a caller nor a reader has to guess which one they hold.
        """
        if self.loop is None:
            return {"started": False, "mode": self.config.mode,
                    "configured_mode": self.config.mode, "n_ticks": 0}
        return {"started": True, "mode": self.config.mode,
                **self.loop.mode_report()}
