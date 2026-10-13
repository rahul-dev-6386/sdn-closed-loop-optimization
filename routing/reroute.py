"""Reroute execution with make-before-break ordering (P9).

Deciding *that* a flow should move is only half the problem. The other half is
moving it without dropping packets, and moving it back again if anything goes
wrong. This module owns that ordering.

Make-before-break
-----------------
A reroute installs the new path *around* the existing one:

1. Install the new ingress->egress rules for the flow at every switch on the new
   path, at a **higher priority** than the current rules. Both paths now match
   the flow; the controller chooses which one carries it.
2. Once every new rule is confirmed present, delete the **ingress** rule on the
   old path. This is the commit point: the flow now takes the new path.
3. Delete the remaining old egress rules.

The ordering matters at each step. Installing before deleting means there is
always a complete, matching rule set for the flow -- a break-before-make
sequence leaves a window in which the flow matches nothing and is dropped. Doing
the ingress deletion last-but-one means the old egress rules are still in place
while the new ones take effect, so a failure partway through is recoverable by
deleting the new rules and leaving the old path intact.

Failure handling
----------------
If any step fails, the sequence stops and rolls back: the rules installed by
this attempt are deleted and the previous path is left in place. The flow is
never left worse than it was, and the rollback itself is ordered (new ingress
first, so the flow falls back onto the still-installed old path).

This module does not talk to OpenFlow itself. It emits an ordered
:class:`ReroutePlan` of operations for a caller to execute, which keeps the
ordering logic testable without a fabric and without root.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from routing.cost import (
    CostWeights,
    LinkMetrics,
    Path as LinkPath,
    PathChoice,
    RerouteGuard,
    RoutingError,
    choose_path,
    path_cost,
)
from routing.paths import Node, Path as SwitchPath

LOGGER = logging.getLogger("routing.reroute")

#: A directed link as the cost function names them.
LinkKey = Tuple[str, str, str]


class RerouteError(RoutingError):
    """Raised when a reroute plan cannot be constructed."""


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Operation:
    """One step of a reroute plan.

    Attributes
    ----------
    kind:
        ``install_ingress`` / ``install_egress`` / ``delete_ingress`` /
        ``delete_egress``.
    switch:
        The switch the operation applies to.
    match:
        The flow selector, verbatim from the caller.
    priority:
        Rule priority. New-path rules outrank old-path ones so both paths match
        during the overlap window and the controller's choice is unambiguous.
    phase:
        ``prepare``, ``commit`` or ``cleanup``, which is the make-before-break
        stage. Ordering is guaranteed by the phase *and* the sequence within it.
    sequence:
        Position in the whole plan, assigned by :func:`build_reroute_plan`.
    """

    kind: str
    switch: str
    match: Dict[str, Any] = field(default_factory=dict)
    priority: int = 100
    phase: str = "prepare"
    sequence: int = 0

    @property
    def is_install(self) -> bool:
        """Whether this operation adds a rule."""
        return self.kind.startswith("install")

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        return {
            "kind": self.kind, "switch": self.switch, "match": self.match,
            "priority": self.priority, "phase": self.phase,
            "sequence": self.sequence,
        }


#: Phase order. Prepare fully precedes commit, which fully precedes cleanup.
PHASE_ORDER: Tuple[str, ...] = ("prepare", "commit", "cleanup")


def switch_path_to_links(
    path: SwitchPath,
    link_keys: Sequence[LinkKey],
    default_direction: str = "forward",
) -> LinkPath:
    """Translate a switch path into the directed links the cost function uses.

    Prefers a link whose ``src_switch``/``dst_switch`` match the path step in
    order; falls back to the same pair in the opposite direction, since a
    controller may only have recorded one direction.

    Raises
    ------
    RerouteError
        If a consecutive pair on the path has no matching link, which means the
        topology and the flow table disagree.
    """
    result: List[LinkKey] = []
    for a, b in zip(path, path[1:]):
        forward = (a, b, default_direction)
        reverse = (b, a, default_direction)
        if forward in link_keys:
            result.append(forward)
        elif reverse in link_keys:
            result.append(reverse)
        else:
            raise RerouteError(
                f"no telemetry link between {a} and {b}; the topology and the "
                "flow table disagree"
            )
    return tuple(result)


def link_path_to_switches(path: LinkPath) -> SwitchPath:
    """Recover the switch sequence from a directed-link path."""
    if not path:
        raise RerouteError("cannot convert an empty link path to switches")
    return (path[0][0],) + tuple(dst for _, dst, _ in path)


# --------------------------------------------------------------------------- #
# Plan construction
# --------------------------------------------------------------------------- #
@dataclass
class ReroutePlan:
    """An ordered set of operations implementing one path change."""

    flow: str
    old_path: Optional[SwitchPath]
    new_path: SwitchPath
    operations: List[Operation] = field(default_factory=list)
    aborted: bool = False
    reason: str = ""

    @property
    def is_empty(self) -> bool:
        """Whether the plan would change nothing."""
        return not self.operations

    def by_phase(self) -> Dict[str, List[Operation]]:
        """Operations grouped by make-before-break phase."""
        grouped: Dict[str, List[Operation]] = {p: [] for p in PHASE_ORDER}
        for op in self.operations:
            grouped[op.phase].append(op)
        return grouped

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        return {
            "flow": self.flow,
            "old_path": list(self.old_path) if self.old_path else None,
            "new_path": list(self.new_path),
            "aborted": self.aborted,
            "reason": self.reason,
            "n_operations": len(self.operations),
            "operations": [op.as_dict() for op in self.operations],
        }


def build_reroute_plan(
    flow: str,
    old_path: SwitchPath,
    new_path: SwitchPath,
    match: Dict[str, Any],
    base_priority: int = 100,
    make_before_break: bool = True,
) -> ReroutePlan:
    """Build the ordered operations to move ``flow`` from ``old_path`` to ``new_path``.

    Parameters
    ----------
    base_priority:
        Priority of the existing rules. New rules are installed at
        ``base_priority - 1`` so they take precedence while both paths match.

    Raises
    ------
    RerouteError
        If the paths are identical (nothing to do) or a path is too short to
        carry a flow (a single switch is not a path through the network).
    """
    if old_path == new_path:
        raise RerouteError("old and new paths are identical; nothing to reroute")
    for label, path in (("old_path", old_path), ("new_path", new_path)):
        if len(path) < 2:
            raise RerouteError(
                f"{label} must contain at least two switches, got {path}"
            )

    new_priority = base_priority - 1
    # (kind, switch, priority, phase) in make-before-break order. The sequence
    # number is assigned when the Operation is constructed -- Operation is frozen,
    # so assigning it afterwards would raise.
    steps: List[Tuple[str, str, int, str]] = []

    # Phase 1 -- prepare: install the whole new path, ingress and egress.
    # Ingress first so each switch is forwarding before the next is touched.
    steps += [("install_ingress", sw, new_priority, "prepare")
              for sw in new_path[:-1]]
    steps += [("install_egress", sw, new_priority, "prepare")
              for sw in new_path[1:]]

    # Phase 2 -- commit: remove the old ingress, one switch at a time. This is
    # the moment the flow actually changes path.
    steps += [("delete_ingress", sw, base_priority, "commit")
              for sw in old_path[:-1]]

    # Phase 3 -- cleanup: remove the old egress, now unreachable.
    steps += [("delete_egress", sw, base_priority, "cleanup")
              for sw in old_path[1:]]

    if not make_before_break:
        # Break-before-make: delete the old path entirely before installing the
        # new one. Faster (no overlap) but drops packets for the duration, so it
        # is opt-in rather than the default.
        steps.sort(key=lambda st: {"cleanup": 0, "commit": 1, "prepare": 2}[st[3]])

    operations = [
        Operation(kind, switch, dict(match), priority, phase, index)
        for index, (kind, switch, priority, phase) in enumerate(steps)
    ]

    return ReroutePlan(flow=flow, old_path=old_path, new_path=new_path,
                       operations=operations)


def build_rollback_plan(plan: ReroutePlan, base_priority: int = 100) -> ReroutePlan:
    """Undo a partially applied :class:`ReroutePlan`.

    The rollback is itself ordered make-before-break: the new path's ingress is
    deleted first so the flow falls back onto the old path -- which is still
    installed, because the commit phase deletes old ingress *before* old egress.
    """
    if plan.old_path is None:
        raise RerouteError("cannot roll back a plan that had no old path")
    steps: List[Tuple[str, str, str]] = []
    steps += [("delete_ingress", sw, "commit") for sw in plan.new_path[:-1]]
    steps += [("delete_egress", sw, "cleanup") for sw in plan.new_path[1:]]
    operations = [
        Operation(kind, switch, {}, base_priority - 1, phase, index)
        for index, (kind, switch, phase) in enumerate(steps)
    ]
    return ReroutePlan(
        flow=plan.flow, old_path=plan.old_path, new_path=plan.old_path,
        operations=operations, aborted=True,
        reason="rollback: new path never became fully committed",
    )


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
@dataclass
class ExecutionResult:
    """What happened when a plan was executed."""

    flow: str
    applied: int = 0
    failed: Optional[Operation] = None
    rolled_back: bool = False
    rollback_applied: int = 0
    final_path: Optional[SwitchPath] = None
    error: str = ""

    @property
    def succeeded(self) -> bool:
        """Whether the flow ended up on the new path."""
        return self.failed is None and not self.rolled_back

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        return {
            "flow": self.flow, "applied": self.applied,
            "succeeded": self.succeeded, "rolled_back": self.rolled_back,
            "rollback_applied": self.rollback_applied,
            "final_path": (list(self.final_path)
                           if self.final_path else None),
            "failed_at": self.failed.as_dict() if self.failed else None,
            "error": self.error,
        }


def execute_plan(
    plan: ReroutePlan,
    apply: Callable[[Operation], bool],
    base_priority: int = 100,
) -> ExecutionResult:
    """Run a plan, rolling back on the first failure.

    Parameters
    ----------
    apply:
        Executes one operation and returns True on success. Injecting this keeps
        the ordering logic testable without a fabric.

    Notes
    -----
    On failure the rollback *re-adds nothing*: it only removes what this attempt
    installed. The old path was never fully removed (commit precedes cleanup),
    so the flow falls back to it. This is why the commit phase deletes old
    ingress and only then the old egress.
    """
    result = ExecutionResult(flow=plan.flow, final_path=plan.old_path)
    if plan.is_empty:
        result.final_path = plan.new_path
        return result

    installed: List[Operation] = []
    for op in plan.operations:
        try:
            ok = apply(op)
        except Exception as exc:  # noqa: BLE001
            result.failed = op
            result.error = f"{type(exc).__name__}: {exc}"
            break
        if not ok:
            result.failed = op
            result.error = f"{op.kind} rejected on {op.switch}"
            break
        result.applied += 1
        if op.is_install:
            installed.append(op)

    if result.failed is not None:
        rollback = build_rollback_plan(plan, base_priority)
        for op in rollback.operations:
            try:
                if apply(op):
                    result.rollback_applied += 1
            except Exception:  # noqa: BLE001
                LOGGER.error("rollback operation %s on %s failed",
                             op.kind, op.switch)
        result.rolled_back = True
        result.final_path = plan.old_path
        LOGGER.warning("reroute of %s failed at %s; rolled back %d operation(s)",
                       plan.flow, result.failed.kind, result.rollback_applied)
        return result

    result.final_path = plan.new_path
    return result


# --------------------------------------------------------------------------- #
# The decision loop
# --------------------------------------------------------------------------- #
@dataclass
class FlowState:
    """Where a flow currently is, and when it moved."""

    path: SwitchPath
    last_change_s: float = 0.0


class RerouteController:
    """Decides and performs reroutes for a set of flows.

    This is the piece P9's experiments drive. It owns the cost weights, the
    candidate enumeration and the per-flow hysteresis, and it records every
    decision -- including the ones it declines -- so P10 can report why a flow did
    not move rather than only that it did not move.

    Parameters
    ----------
    weights:
        Cost coefficients.
    guard:
        Per-flow cooldown and dwell timer.
    improvement_ratio, base_priority, make_before_break:
        Passed through to :func:`choose_path` and :func:`build_reroute_plan`.
    now_fn:
        Clock, injectable so tests need not sleep.
    """

    def __init__(
        self,
        weights: CostWeights,
        guard: Optional[RerouteGuard] = None,
        improvement_ratio: float = 0.85,
        base_priority: int = 100,
        make_before_break: bool = True,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.weights = weights
        self.guard = guard or RerouteGuard()
        self.improvement_ratio = improvement_ratio
        self.base_priority = base_priority
        self.make_before_break = make_before_break
        self.now_fn = now_fn
        self.flows: Dict[str, FlowState] = {}
        self.decisions: List[Dict[str, Any]] = []

    def register(self, flow: str, path: SwitchPath, now: Optional[float] = None) -> None:
        """Record a flow's initial placement and start its dwell timer."""
        self.flows[flow] = FlowState(path=path)
        self.guard.register(flow, now=now)

    def decide(
        self,
        flow: str,
        candidates: Sequence[SwitchPath],
        metrics: Mapping[LinkKey, LinkMetrics],
        now: Optional[float] = None,
    ) -> Optional[ReroutePlan]:
        """Decide whether to move ``flow``, without executing anything.

        Returns the plan to execute, or ``None`` when no reroute is warranted.
        Every outcome is appended to :attr:`decisions`, so a declined reroute is
        as visible as an accepted one.
        """
        if flow not in self.flows:
            raise RerouteError(f"flow {flow!r} was never registered")
        t = self.now_fn() if now is None else now
        current = self.flows[flow].path

        link_paths: List[LinkPath] = []
        usable: List[SwitchPath] = []
        for path in candidates:
            try:
                link_paths.append(switch_path_to_links(path, list(metrics)))
                usable.append(path)
            except RerouteError as exc:
                LOGGER.debug("skipping candidate %s: %s", path, exc)
        if not usable:
            self._record(flow, current, None, "no usable candidate", t)
            return None

        current_links = switch_path_to_links(current, list(metrics))
        try:
            choice = choose_path(
                link_paths, metrics, self.weights,
                current=current_links, improvement_ratio=self.improvement_ratio,
            )
        except RoutingError as exc:
            self._record(flow, current, None, f"cost error: {exc}", t)
            return None

        best_switch_path = usable[link_paths.index(choice.best)]
        if not choice.reroute:
            self._record(flow, current, choice, choice.reason, t)
            return None

        allowed, why = self.guard.may_reroute(flow, t)
        if not allowed:
            self._record(flow, current, choice, f"hysteresis: {why}", t)
            return None

        plan = build_reroute_plan(
            flow, current, best_switch_path, {"flow": flow},
            base_priority=self.base_priority,
            make_before_break=self.make_before_break,
        )
        self._record(flow, current, choice, why, t, plan)
        return plan

    def commit(self, flow: str, plan: ReroutePlan,
               now: Optional[float] = None) -> ExecutionResult:
        """Record a plan as executed, updating the flow's path and timers.

        The caller runs the plan's operations; this updates the controller's own
        bookkeeping so a failed plan leaves the flow where it was.
        """
        t = self.now_fn() if now is None else now
        result = ExecutionResult(flow=flow, final_path=plan.old_path)
        if plan.old_path is not None:
            self.flows[flow] = FlowState(path=plan.old_path)
            self.guard.record(flow, t)
        if not plan.is_empty:
            self.flows[flow] = FlowState(path=plan.new_path)
            result.final_path = plan.new_path
        return result

    def _record(
        self, flow: str, current: SwitchPath,
        choice: Optional[PathChoice], reason: str, now: float,
        plan: Optional[ReroutePlan] = None,
    ) -> None:
        """Append one decision, accepted or declined, to the log."""
        self.decisions.append({
            "flow": flow,
            "current_path": list(current),
            "chosen_path": list(choice.best) if choice else None,
            "current_cost": (round(choice.current_cost, 6)
                             if choice and choice.current_cost is not None else None),
            "chosen_cost": (round(choice.best_cost, 6)
                            if choice else None),
            "reroute": plan is not None,
            "reason": reason,
            "planned_operations": len(plan.operations) if plan else 0,
            "at_s": round(now, 3),
        })

    def reset(self) -> None:
        """Clear all flows, timers and decision history (between experiments)."""
        self.flows.clear()
        self.guard.reset()
        self.decisions.clear()
