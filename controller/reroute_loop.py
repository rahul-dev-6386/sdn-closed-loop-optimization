"""The reroute decision loop (P9).

The controller cannot be tested by launching a fabric for every change, so the
decision logic lives here as an ordinary object driven by two callables and
plain data:

* ``snapshot()`` supplies the latest telemetry rows,
* ``apply(operation)`` performs a flow-table change.

Everything in between -- history, prediction, costing, candidate enumeration,
hysteresis -- is deterministic given those two, which is what makes the four
routing modes comparable in P10. If the controller's event loop instead owned
this logic inline, a claim about mode differences would rest on a fabric being
present every time it was checked.

Four modes, one code path
-------------------------
The modes differ only in which terms the cost function may use:

``static_shortest``
    Ignores telemetry entirely; hop count decides.
``ecmp``
    Uses only present load, spread by hash so no feedback loop exists.
``reactive_threshold``
    Uses present load, but switches to a backup path once a link is already
    above the congestion threshold -- it reacts *after* congestion begins.
``ml_aware``
    Uses present load *and* the P8 forecast, so it can act before congestion.

Implementing them as four separate controllers would make any difference in
outcome attributable to the implementations rather than to the policy. Sharing
one path keeps the comparison honest.

Degradation
-----------
Three conditions must each cause a documented fallback rather than a wrong
decision:

* telemetry stalled -> ``static_shortest`` (rule 7: fail safe to shortest path),
* no model available -> ``reactive_threshold``,
* prediction degraded or feature drift -> ``reactive_threshold``.

Each fallback is recorded in the tick's report, so P10 can show how often the
ML mode was actually in charge. Reporting a mode's numbers while it silently ran
on a fallback would be the most misleading outcome this project could produce.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from routing.cost import (
    CostWeights,
    LinkMetrics,
    RerouteGuard,
    RoutingError,
)
from routing.paths import (
    build_graph,
    k_disjoint_candidates,
    Path as SwitchPath,
)
from routing.reroute import (
    Operation,
    RerouteController,
    RerouteError,
    link_path_to_switches,
)

LOGGER = logging.getLogger("controller.reroute_loop")

LinkKey = Tuple[str, str, str]

#: The four modes P10 compares, in increasing sophistication.
ROUTING_MODES: Tuple[str, ...] = (
    "static_shortest", "ecmp", "reactive_threshold", "ml_aware",
)

#: Cost weights used per mode. ``static_shortest`` cannot express these, so it is
#: handled separately; the others share a weighting and differ in which
#: *signals* they are allowed to use, not in how those signals are priced.
MODE_SIGNALS: Dict[str, Dict[str, bool]] = {
    # Which signal each mode is allowed to consult.
    "static_shortest": {"load": False, "forecast": False},
    "ecmp": {"load": True, "forecast": False},
    "reactive_threshold": {"load": True, "forecast": False},
    "ml_aware": {"load": True, "forecast": True},
}


class RerouteLoopError(RuntimeError):
    """Raised when the loop cannot be constructed or driven."""


@dataclass
class TickReport:
    """What one decision tick decided, and under what conditions."""

    mode: str
    effective_mode: str
    timestamp: float
    n_links: int = 0
    n_scored: int = 0
    n_unscored: int = 0
    predicted_congested: List[str] = field(default_factory=list)
    plans: List[Dict[str, Any]] = field(default_factory=list)
    reroutes_applied: int = 0
    fallback_reason: str = ""
    latency_ms: float = 0.0

    @property
    def degraded(self) -> bool:
        """Whether the tick ran under a fallback mode."""
        return self.effective_mode != self.mode

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        return {
            "mode": self.mode,
            "effective_mode": self.effective_mode,
            "timestamp": round(self.timestamp, 3),
            "n_links": self.n_links,
            "n_scored": self.n_scored,
            "n_unscored": self.n_unscored,
            "predicted_congested": self.predicted_congested,
            "n_plans": len(self.plans),
            "reroutes_applied": self.reroutes_applied,
            "fallback_reason": self.fallback_reason,
            "degraded": self.degraded,
            "latency_ms": round(self.latency_ms, 3),
            "plans": self.plans,
        }


@dataclass
class LoopConfig:
    """Tunables the loop needs, resolved from ``config.yaml``."""

    mode: str = "ml_aware"
    weights: CostWeights = field(default_factory=CostWeights)
    improvement_ratio: float = 0.85
    cooldown_s: float = 10.0
    min_dwell_s: float = 8.0
    decision_interval_s: float = 2.0
    congestion_threshold: float = 0.80
    k_paths: int = 2
    history_w: int = 12
    #: Flows to consider. Empty means "every source/destination pair the
    #: controller has seen traffic between".
    flows: List[Tuple[str, str]] = field(default_factory=list)

    #: Environment variable that overrides ``routing.mode``.
    #:
    #: P10 compares all four modes. Rewriting config.yaml between runs would make
    #: the mode under test a function of what the previous run happened to leave
    #: behind, so the experiment harness selects it through the environment
    #: instead and config.yaml keeps declaring the default.
    MODE_ENV_VAR = "SDN_ROUTING_MODE"

    @classmethod
    def from_config(cls, config: Any, *, mode_override: Optional[str] = None) -> "LoopConfig":
        """Build from the ``routing``/``telemetry``/``ml`` config sections.

        Parameters
        ----------
        mode_override:
            Forces a routing mode, taking precedence over both
            ``$SDN_ROUTING_MODE`` and ``routing.mode``.
        """
        mode = (mode_override or os.environ.get(cls.MODE_ENV_VAR)
                or str(config.get("routing.mode", "ml_aware")))
        if mode not in ROUTING_MODES:
            raise RerouteLoopError(
                f"routing.mode {mode!r} is not one of {ROUTING_MODES}"
            )
        return cls(
            mode=mode,
            weights=CostWeights.from_config(config),
            improvement_ratio=float(config.get("routing.improvement_ratio", 0.85)),
            cooldown_s=float(config.get("routing.cooldown_s", 10.0)),
            min_dwell_s=float(config.get("routing.min_dwell_s", 8.0)),
            decision_interval_s=float(config.get("routing.decision_interval_s", 2.0)),
            congestion_threshold=float(
                config.get("telemetry.congestion_utilization_threshold", 0.80)),
            k_paths=2,
            history_w=int(config.get("ml.window_w", 12)),
        )


class LinkHistory:
    """A bounded per-link window of telemetry rows.

    The P8 predictor needs ``window_w`` consecutive samples per link, but the
    telemetry collector only retains the latest record per link. This keeps the
    trailing window so the predictor has something to score, and bounds it so a
    long-running controller cannot grow without limit.
    """

    def __init__(self, window_w: int = 12) -> None:
        if window_w < 1:
            raise ValueError(f"window_w must be >= 1, got {window_w}")
        self.window_w = window_w
        self._rows: Dict[LinkKey, Deque[Dict[str, Any]]] = {}

    def add(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Append one tick's rows, trimming each link's window."""
        for row in rows:
            key = (str(row.get("src_switch", "")), str(row.get("dst_switch", "")),
                   str(row.get("direction", "forward")))
            if not key[0] or not key[1]:
                continue
            bucket = self._rows.setdefault(key, deque(maxlen=self.window_w))
            bucket.append(dict(row))

    def rows(self) -> List[Dict[str, Any]]:
        """Every retained row, oldest first within each link."""
        out: List[Dict[str, Any]] = []
        for bucket in self._rows.values():
            out.extend(bucket)
        return out

    def window_sizes(self) -> Dict[LinkKey, int]:
        """How many samples each link currently holds."""
        return {key: len(bucket) for key, bucket in self._rows.items()}

    def clear(self) -> None:
        """Drop all history (between experiment runs)."""
        self._rows.clear()


class RerouteLoop:
    """Drives prediction, costing and reroute execution on a timer.

    Parameters
    ----------
    snapshot:
        Returns the latest telemetry rows as a list of dicts. Called once per
        tick.
    apply:
        Executes one :class:`routing.reroute.Operation`. Returns True on
        success.
    predictor:
        A :class:`ml.predict.CongestionPredictor`, or ``None`` -- in which case
        ``ml_aware`` degrades to ``reactive_threshold`` and says so.
    stalled:
        Returns True when telemetry has stalled. Injected rather than read from
        the collector so the degradation path is testable without a fabric.
    now_fn:
        Clock, injectable for tests.
    """

    def __init__(
        self,
        snapshot: Callable[[], Sequence[Mapping[str, Any]]],
        apply: Callable[[Operation], bool],
        predictor: Any = None,
        stalled: Callable[[], bool] = lambda: False,
        config: Optional[LoopConfig] = None,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.snapshot = snapshot
        self.apply_op = apply
        self.predictor = predictor
        self.stalled = stalled
        self.config = config or LoopConfig()
        self.now_fn = now_fn

        self.history = LinkHistory(self.config.history_w)
        self.controller = RerouteController(
            weights=self.config.weights,
            guard=RerouteGuard(cooldown_s=self.config.cooldown_s,
                               min_dwell_s=self.config.min_dwell_s,
                               now_fn=now_fn),
            improvement_ratio=self.config.improvement_ratio,
            now_fn=now_fn,
        )
        self.graph: Dict[str, List[str]] = {}
        self.ticks: List[TickReport] = []
        self.fallbacks: Dict[str, int] = {}

    # ---------------------------------------------------------- topology --
    def observe_links(self, links: Sequence[Tuple[str, str]]) -> None:
        """Update the switch graph from the discovered link set.

        Notes
        -----
        The graph is a *set* of bidirectional adjacencies, not a DAG: a
        loop-free forwarding table does not imply a loop-free topology. The
        controller's drop-unknown-unicast rule (P3) is what prevents a topology
        cycle from becoming a packet loop, and candidate enumeration here must
        still see the true connectivity to be able to avoid a congested link.
        """
        self.graph = build_graph(links)

    def candidates(self, src: str, dst: str) -> List[SwitchPath]:
        """Candidate paths for a flow's endpoints."""
        if src not in self.graph or dst not in self.graph:
            return []
        return k_disjoint_candidates(self.graph, src, dst, k=self.config.k_paths)

    # ------------------------------------------------------------- modes --
    def effective_mode(self, *, stalled: bool, degraded: bool) -> Tuple[str, str]:
        """Resolve the mode actually in force for this tick.

        Returns
        -------
        (effective_mode, reason)
            ``reason`` is empty when the configured mode was honoured.

        Notes
        -----
        Order matters. A stalled fabric cannot trust *any* measurement, so it
        falls all the way back to shortest path (rule 7). Only once telemetry is
        known-fresh is a missing or degraded forecast downgraded to reactive.
        """
        if self.config.mode == "static_shortest":
            return "static_shortest", ""
        if stalled:
            return "static_shortest", "telemetry stalled (rule 7)"
        if self.config.mode == "ml_aware" and degraded:
            return "reactive_threshold", "ML forecast unavailable or degraded"
        return self.config.mode, ""

    # --------------------------------------------------------------- tick --
    def tick(self) -> TickReport:
        """Run one decision tick.

        Returns a :class:`TickReport` recording what was decided, whether the
        configured mode was honoured, and every reroute attempted.
        """
        started = time.perf_counter()
        now = self.now_fn()
        rows = list(self.snapshot())
        self.history.add(rows)

        report = TickReport(mode=self.config.mode, effective_mode=self.config.mode,
                            timestamp=now, n_links=len(self.history._rows))

        probabilities, degraded = self._forecast()
        report.n_scored = sum(1 for p in probabilities.values() if p is not None)
        report.n_unscored = sum(1 for p in probabilities.values() if p is None)
        report.predicted_congested = sorted(
            f"{a}->{b}:{d}" for (a, b, d), p in probabilities.items()
            if p is not None and p >= 0.5
        )

        stalled = bool(self.stalled())
        effective, reason = self.effective_mode(stalled=stalled, degraded=degraded)
        report.effective_mode = effective
        report.fallback_reason = reason
        if reason:
            self.fallbacks[reason] = self.fallbacks.get(reason, 0) + 1

        metrics = self._link_metrics(rows, probabilities, effective)

        for flow, state in list(self.controller.flows.items()):
            # The flow's endpoints are the ends of the path it is currently on,
            # which is what keeps this correct after a reroute moves it onto a
            # route with different first/last hops.
            current = state.path
            if len(current) < 2:
                continue
            src, dst = current[0], current[-1]
            paths = self.candidates(src, dst)
            if not paths:
                continue
            try:
                plan = self.controller.decide(flow, paths, metrics, now=now)
            except RerouteError as exc:
                LOGGER.debug("decision failed for %s: %s", flow, exc)
                continue
            if plan is None:
                continue
            result = self._run_plan(plan)
            report.reroutes_applied += result.applied
            report.plans.append({
                "flow": flow,
                "operations": len(plan.operations),
                "applied": result.applied,
                "succeeded": result.succeeded,
                "old_path": list(plan.old_path) if plan.old_path else None,
                "new_path": list(plan.new_path),
            })
            if result.succeeded:
                self.controller.commit(flow, plan, now=now)

        report.latency_ms = (time.perf_counter() - started) * 1000.0
        self.ticks.append(report)
        return report

    # ------------------------------------------------------------ helpers --
    def _forecast(self) -> Tuple[Dict[LinkKey, Optional[float]], bool]:
        """Ask the predictor for a per-link congestion probability.

        Returns
        -------
        (probabilities, degraded)
            ``degraded`` is True when the mode cannot be honoured, including the
            case of no model at all.
        """
        if self.config.mode != "ml_aware":
            return {}, False
        if self.predictor is None:
            LOGGER.warning("ml_aware selected but no model is loaded; "
                           "degrading to reactive_threshold")
            return {}, True
        try:
            batch = self.predictor.predict(self.history.rows())
        except Exception as exc:  # noqa: BLE001
            # A model that raises must not take the controller down with it;
            # P8's own guards already refuse drift and staleness, and this
            # catches anything they do not cover.
            LOGGER.error("prediction failed (%s); degrading to "
                         "reactive_threshold", exc)
            return {}, True
        return ({p.link_key: p.probability for p in batch.predictions},
                bool(batch.degraded))

    def _link_metrics(
        self,
        rows: Sequence[Mapping[str, Any]],
        probabilities: Mapping[LinkKey, Optional[float]],
        effective_mode: str,
    ) -> Dict[LinkKey, LinkMetrics]:
        """Build the per-link cost inputs for this tick.

        The mode decides which signals survive:

        * ``static_shortest`` zeros every measurement, so the cost is uniform and
          the outcome is decided purely by path length and the improvement guard.
        * ``ecmp`` and ``reactive_threshold`` use present load only; the
          difference between them is *when* they act, not what they can see.
        * ``ml_aware`` adds the forecast.
        """
        signals = MODE_SIGNALS.get(effective_mode, MODE_SIGNALS["reactive_threshold"])
        threshold = self.config.congestion_threshold
        latest: Dict[LinkKey, Dict[str, Any]] = {}
        for row in rows:
            key = (str(row.get("src_switch", "")), str(row.get("dst_switch", "")),
                   str(row.get("direction", "forward")))
            latest[key] = dict(row)

        metrics: Dict[LinkKey, LinkMetrics] = {}
        for key, row in latest.items():
            if signals["load"]:
                load = LinkMetrics(
                    utilization=float(row.get("utilization") or 0.0),
                    latency_ms=float(row.get("latency_ms") or 0.0),
                    loss_pct=float(row.get("loss_pct") or 0.0),
                    p_congestion=0.0,
                )
            else:
                load = LinkMetrics()

            if not signals["forecast"]:
                load.p_congestion = 0.0
            else:
                prob = probabilities.get(key)
                if prob is None:
                    # No forecast for this link: use the measured load, not zero.
                    # Zeroing it would make an unmeasured link look like the
                    # safest one available.
                    load.p_congestion = 0.0
                else:
                    load.p_congestion = float(prob)

            if effective_mode == "reactive_threshold" and not signals["forecast"]:
                # A reactive controller only reacts once the link is *already*
                # over threshold; the forecast term is the mechanism by which
                # ml_aware differs, so the reactive mode sets it to 1.0 exactly
                # at the threshold and 0.0 below.
                load.p_congestion = (
                    1.0 if load.utilization >= threshold else 0.0
                )
            metrics[key] = load
        return metrics

    def _run_plan(self, plan: Any) -> Any:
        """Execute a plan's operations through the injected ``apply``."""
        from routing.reroute import execute_plan

        return execute_plan(plan, self.apply_op)

    # ---------------------------------------------------------- lifecycle --
    def register_flow(self, flow: str, path: SwitchPath) -> None:
        """Tell the loop where a flow currently is."""
        self.controller.register(flow, path)

    def reset(self) -> None:
        """Clear history, flows and tick reports (between experiment runs)."""
        self.history.clear()
        self.controller.reset()
        self.ticks.clear()
        self.fallbacks.clear()

    def mode_report(self) -> Dict[str, Any]:
        """Summary for the experiment harness.

        ``n_ticks`` and ``fallbacks`` together answer the question a results
        table must answer before its numbers mean anything: was the mode under
        test actually the mode that ran?
        """
        effective: Dict[str, int] = {}
        for tick in self.ticks:
            effective[tick.effective_mode] = effective.get(tick.effective_mode, 0) + 1
        return {
            "configured_mode": self.config.mode,
            "n_ticks": len(self.ticks),
            "effective_modes": effective,
            "fallbacks": dict(self.fallbacks),
            "degraded_ticks": sum(1 for t in self.ticks if t.degraded),
            "reroutes_planned": sum(len(t.plans) for t in self.ticks),
            "n_flows": len(self.controller.flows),
        }
