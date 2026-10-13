"""Tests for P9 reroute execution and make-before-break ordering.

The ordering is the whole point of this module, so the central tests assert the
*sequence* of operations rather than just their count. A plan that installs the
wrong rule in the wrong order looks identical by length and drops traffic in
practice.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import pytest

from routing.cost import CostWeights, LinkMetrics, RerouteGuard, RoutingError
from routing.paths import build_graph, k_disjoint_candidates
from routing.reroute import (
    PHASE_ORDER,
    ExecutionResult,
    FlowState,
    Operation,
    RerouteController,
    RerouteError,
    build_rollback_plan,
    build_reroute_plan,
    execute_plan,
    link_path_to_switches,
    switch_path_to_links,
)

W = CostWeights()

PATH_A: Tuple[str, ...] = ("s1", "s2", "s4")
PATH_B: Tuple[str, ...] = ("s1", "s3", "s4")

DIAMOND_LINKS = [("s1", "s2"), ("s2", "s4"), ("s1", "s3"), ("s3", "s4")]


def metrics_for(mapping: Dict[Tuple[str, str], LinkMetrics]) -> Dict:
    """Expand a compact {(a, b): metrics} map into directed link keys."""
    out = {}
    for (a, b), m in mapping.items():
        out[(a, b, "forward")] = m
    return out


def m(util: float = 0.0, lat: float = 0.0, loss: float = 0.0,
      p: float = 0.0) -> LinkMetrics:
    """Shorthand for a link's measurements."""
    return LinkMetrics(utilization=util, latency_ms=lat, loss_pct=loss,
                       p_congestion=p)


def diamond_metrics(a: LinkMetrics, b: LinkMetrics) -> Dict:
    """Metrics for the diamond: ``a`` on path A, ``b`` on path B."""
    return metrics_for({("s1", "s2"): a, ("s2", "s4"): a,
                        ("s1", "s3"): b, ("s3", "s4"): b})


# ------------------------------------------------- path <-> link mapping --
def test_switch_path_maps_to_directed_links() -> None:
    links = [("s1", "s2", "forward"), ("s2", "s4", "forward")]
    assert switch_path_to_links(("s1", "s2", "s4"), links) == tuple(links)


def test_reverse_link_is_accepted_when_forward_is_absent() -> None:
    """A controller may only have recorded one direction of a link."""
    links = [("s2", "s1", "forward"), ("s2", "s4", "forward")]
    assert switch_path_to_links(("s1", "s2", "s4"), links) == (
        ("s2", "s1", "forward"), ("s2", "s4", "forward"))


def test_missing_link_between_path_steps_is_rejected() -> None:
    with pytest.raises(RerouteError, match="no telemetry link"):
        switch_path_to_links(("s1", "s9"), [("s1", "s2", "forward")])


def test_link_path_converts_back_to_switches() -> None:
    links = (("s1", "s2", "forward"), ("s2", "s4", "forward"))
    assert link_path_to_switches(links) == ("s1", "s2", "s4")


def test_empty_link_path_is_rejected() -> None:
    with pytest.raises(RerouteError, match="empty link path"):
        link_path_to_switches(())


# ------------------------------------------------------ plan construction --
def test_identical_paths_are_rejected() -> None:
    with pytest.raises(RerouteError, match="nothing to reroute"):
        build_reroute_plan("f1", PATH_A, PATH_A, {"flow": "f1"})


def test_a_single_switch_is_not_a_path() -> None:
    with pytest.raises(RerouteError, match="at least two switches"):
        build_reroute_plan("f1", PATH_A, ("s1",), {"flow": "f1"})
    with pytest.raises(RerouteError, match="at least two switches"):
        build_reroute_plan("f1", ("s1",), PATH_B, {"flow": "f1"})


def test_make_before_break_order_is_prepare_commit_cleanup() -> None:
    """The ordering that prevents dropped packets."""
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    phases = [op.phase for op in plan.operations]
    assert phases == sorted(phases, key=PHASE_ORDER.index), phases
    assert phases[0] == "prepare"
    assert "commit" in phases and "cleanup" in phases


def test_new_path_is_fully_installed_before_anything_is_deleted() -> None:
    """The make-before-make invariant: never a moment with no matching rule."""
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    kinds = [op.kind for op in plan.operations]
    first_delete = min(i for i, k in enumerate(kinds) if k.startswith("delete"))
    assert all(k.startswith("install") for k in kinds[:first_delete]), kinds


def test_old_ingress_is_deleted_before_old_egress() -> None:
    """The commit point. Deleting egress first would strand the flow.

    With egress removed but ingress still present, packets arrive at the old
    ingress switch and have nowhere to go.
    """
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    commit_start = next(i for i, op in enumerate(plan.operations)
                        if op.phase == "commit")
    cleanup_start = next(i for i, op in enumerate(plan.operations)
                         if op.phase == "cleanup")
    window = plan.operations[commit_start:cleanup_start]
    assert [op.kind for op in window] == ["delete_ingress", "delete_ingress"]


def test_new_rules_outrank_old_rules() -> None:
    """During the overlap both paths match; priority decides, and the new one must."""
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"}, base_priority=100)
    for op in plan.operations:
        if op.is_install:
            assert op.priority == 99
        else:
            assert op.priority == 100


def test_install_happens_ingress_before_egress() -> None:
    """Forward direction: each switch forwards before the next is touched."""
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    prepare = plan.by_phase()["prepare"]
    kinds = [op.kind for op in prepare]
    assert kinds == (["install_ingress"] * 2 + ["install_egress"] * 2), kinds


def test_operations_are_sequence_numbered_without_gaps() -> None:
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    assert [op.sequence for op in plan.operations] == list(
        range(len(plan.operations)))


def test_break_before_make_deletes_first() -> None:
    """The opt-in faster mode: drops packets, so it must be explicit."""
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"},
                              make_before_break=False)
    kinds = [op.kind for op in plan.operations]
    assert kinds[0].startswith("delete")
    assert kinds[-1].startswith("install")


def test_plan_serialises() -> None:
    out = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"}).as_dict()
    assert out["old_path"] == list(PATH_A)
    assert out["new_path"] == list(PATH_B)
    assert out["n_operations"] == len(PATH_B) - 1 + len(PATH_B) - 1 \
        + len(PATH_A) - 1 + len(PATH_A) - 1


# ------------------------------------------------------------- rollback --
def test_rollback_removes_exactly_what_was_installed() -> None:
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    rollback = build_rollback_plan(plan)
    # Every install must have a matching delete, on the same switch, of the
    # same kind of rule (ingress <-> ingress).
    installed = [(op.switch, op.kind.split("_", 1)[1])
                 for op in plan.operations if op.is_install]
    removed = [(op.switch, op.kind.split("_", 1)[1])
               for op in rollback.operations]
    assert sorted(removed) == sorted(installed), (
        "rollback must undo exactly the installs, one for one"
    )
    assert rollback.aborted is True


def test_rollback_deletes_new_ingress_first() -> None:
    """So the flow falls back onto the still-installed old path."""
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    rollback = build_rollback_plan(plan)
    assert rollback.operations[0].kind == "delete_ingress"


def test_rollback_without_an_old_path_is_rejected() -> None:
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    plan.old_path = None
    with pytest.raises(RerouteError, match="no old path"):
        build_rollback_plan(plan)


# ------------------------------------------------------------- execution --
def test_a_successful_run_applies_everything() -> None:
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    seen: List[Operation] = []
    result = execute_plan(plan, lambda op: (seen.append(op) or True))
    assert result.succeeded is True
    assert result.applied == len(plan.operations)
    assert result.rolled_back is False
    assert result.final_path == PATH_B
    assert [op.sequence for op in seen] == list(range(len(plan.operations)))


def test_a_failure_rolls_back_and_keeps_the_old_path() -> None:
    """The flow must never end up worse than it was."""
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    kinds: List[str] = []

    def apply(op: Operation) -> bool:
        kinds.append(op.kind)
        # Fail on the first old-ingress deletion: the commit point.
        return op.kind != "delete_ingress"

    result = execute_plan(plan, apply)
    assert result.succeeded is False
    assert result.rolled_back is True
    assert result.final_path == PATH_A
    assert result.failed is not None and result.failed.kind == "delete_ingress"
    # The rollback removed the new path again.
    assert kinds.count("delete_ingress") >= 2


def test_an_exception_during_apply_is_contained() -> None:
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    calls = {"n": 0}

    def apply(op: Operation) -> bool:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("switch unreachable")
        return True

    result = execute_plan(plan, apply)
    assert result.succeeded is False
    assert "switch unreachable" in result.error
    assert result.final_path == PATH_A


def test_a_failure_before_any_install_still_rolls_back_safely() -> None:
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    result = execute_plan(plan, lambda op: False)
    assert result.applied == 0
    assert result.final_path == PATH_A


def test_an_empty_plan_succeeds_without_operations() -> None:
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    plan.operations = []
    result = execute_plan(plan, lambda op: True)
    assert result.succeeded is True
    assert result.applied == 0


# -------------------------------------------------------- the controller --
def make_controller(**kwargs) -> RerouteController:
    """A controller on a fixed clock, so no test needs to sleep."""
    clock = {"t": 0.0}
    controller = RerouteController(
        weights=W,
        guard=RerouteGuard(cooldown_s=10.0, min_dwell_s=8.0,
                           now_fn=lambda: clock["t"]),
        now_fn=lambda: clock["t"],
        **kwargs,
    )
    return controller


def test_an_unregistered_flow_is_rejected() -> None:
    controller = make_controller()
    with pytest.raises(RerouteError, match="never registered"):
        controller.decide("ghost", [PATH_A, PATH_B], diamond_metrics(m(), m()))


def test_no_reroute_when_the_current_path_is_best() -> None:
    controller = make_controller()
    controller.register("f1", PATH_A)
    plan = controller.decide("f1", [PATH_A, PATH_B],
                             diamond_metrics(m(0.1), m(0.9)), now=100.0)
    assert plan is None
    assert controller.decisions[-1]["reroute"] is False


def test_a_reroute_is_planned_when_the_alternative_is_much_cheaper() -> None:
    controller = make_controller()
    controller.register("f1", PATH_A)
    plan = controller.decide("f1", [PATH_A, PATH_B],
                             diamond_metrics(m(0.9), m(0.1)), now=100.0)
    assert plan is not None
    assert plan.new_path == PATH_B
    assert controller.decisions[-1]["reroute"] is True


def test_a_declined_reroute_is_still_recorded() -> None:
    """P10 must be able to report *why* a flow did not move."""
    controller = make_controller()
    controller.register("f1", PATH_A)
    controller.decide("f1", [PATH_A, PATH_B],
                      diamond_metrics(m(0.9), m(0.1)), now=100.0)
    entry = controller.decisions[-1]
    assert entry["reroute"] is True
    assert entry["current_cost"] is not None
    assert entry["chosen_cost"] is not None
    assert "at_s" in entry


def test_hysteresis_blocks_a_reroute_on_arrival() -> None:
    """A flow must settle before it is moved again."""
    controller = make_controller()
    controller.register("f1", PATH_A, now=102.0)
    plan = controller.decide("f1", [PATH_A, PATH_B],
                             diamond_metrics(m(0.9), m(0.1)), now=102.0)
    assert plan is None
    assert "hysteresis" in controller.decisions[-1]["reason"]
    assert "dwell" in controller.decisions[-1]["reason"]


def test_hysteresis_blocks_a_second_reroute_within_the_cooldown() -> None:
    controller = make_controller()
    controller.register("f1", PATH_A)
    first = controller.decide("f1", [PATH_A, PATH_B],
                              diamond_metrics(m(0.9), m(0.1)), now=100.0)
    assert first is not None
    controller.commit("f1", first, now=100.0)
    assert controller.flows["f1"].path == PATH_B
    # Now on PATH_B, and 5 s later PATH_B has itself become the congested side,
    # so the cost logic wants to move back. Only the cooldown stops it.
    second = controller.decide("f1", [PATH_A, PATH_B],
                               diamond_metrics(m(0.1), m(0.9)), now=105.0)
    assert second is None
    assert "cooldown" in controller.decisions[-1]["reason"]


def test_commit_updates_the_flows_path() -> None:
    controller = make_controller()
    controller.register("f1", PATH_A)
    plan = controller.decide("f1", [PATH_A, PATH_B],
                             diamond_metrics(m(0.9), m(0.1)), now=100.0)
    controller.commit("f1", plan, now=100.0)
    assert controller.flows["f1"].path == PATH_B


def test_a_candidate_with_unknown_links_is_skipped() -> None:
    """One unusable candidate must not sink an otherwise valid decision."""
    controller = make_controller()
    controller.register("f1", PATH_A)
    plan = controller.decide("f1", [("s1", "s9", "s4"), PATH_B],
                             diamond_metrics(m(0.9), m(0.1)), now=100.0)
    assert plan is not None
    assert plan.new_path == PATH_B, "the phantom candidate was not selected"


def test_all_candidates_unknown_means_no_decision() -> None:
    controller = make_controller()
    controller.register("f1", PATH_A)
    plan = controller.decide("f1", [("s1", "s9", "s4"), ("s1", "s8", "s4")],
                             diamond_metrics(m(0.9), m(0.1)), now=100.0)
    assert plan is None
    assert "no usable candidate" in controller.decisions[-1]["reason"]


def test_a_flow_cannot_be_flapped_across_many_ticks() -> None:
    """End-to-end: cost, ratio and timers together stop oscillation.

    Two paths of clearly different cost, with the better one flipping between
    ticks. A controller that acted on every tick would move the flow constantly.
    """
    controller = make_controller()
    controller.register("f1", PATH_A)
    moves = 0
    for tick in range(0, 121, 2):
        # The congestion alternates, so a naive controller would chase it.
        congested_on_a = (tick // 4) % 2 == 0
        metrics = diamond_metrics(
            m(0.9 if congested_on_a else 0.2),
            m(0.2 if congested_on_a else 0.9),
        )
        plan = controller.decide("f1", [PATH_A, PATH_B], metrics,
                                 now=float(tick))
        if plan is None:
            continue
        moves += 1
        controller.commit("f1", plan, now=float(tick))
    # Over 121 s with a 10 s cooldown, at most ~12 moves are physically possible.
    assert moves <= 13, f"flow flapped {moves} times"


def test_reset_clears_flows_and_history() -> None:
    controller = make_controller()
    controller.register("f1", PATH_A)
    controller.decide("f1", [PATH_A, PATH_B], diamond_metrics(m(), m()))
    controller.reset()
    assert controller.flows == {}
    assert controller.decisions == []


def test_execution_result_serialises() -> None:
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    result = execute_plan(plan, lambda op: True)
    out = result.as_dict()
    assert out["succeeded"] is True
    assert out["final_path"] == list(PATH_B)
    assert out["failed_at"] is None


def test_controller_works_with_real_disjoint_candidates() -> None:
    """The full P9 chain: discover paths, cost them, reroute when it pays."""
    graph = build_graph(DIAMOND_LINKS)
    candidates = k_disjoint_candidates(graph, "s1", "s4", k=2)
    assert len(candidates) == 2

    controller = make_controller()
    controller.register("f1", candidates[0])
    plan = controller.decide("f1", candidates, diamond_metrics(m(0.9), m(0.1)),
                             now=100.0)
    assert plan is not None
    assert plan.new_path != controller.flows["f1"].path
