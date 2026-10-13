"""Tests for P9 path cost and reroute decisions.

The behaviours pinned here are the ones that decide whether the controller
oscillates. A cost function that works on paper and a reroute policy that is
merely "pick the cheapest" both produce a network that flaps under load, so the
improvement ratio, cooldown and dwell rules each get direct tests.
"""
from __future__ import annotations

from typing import Dict, List

import pytest

from routing.cost import (
    CostWeights,
    LinkKey,
    LinkMetrics,
    Path,
    RerouteGuard,
    RoutingError,
    choose_path,
    link_cost,
    normalize,
    path_cost,
)

L1: LinkKey = ("s1", "s2", "forward")
L2: LinkKey = ("s2", "s4", "forward")
L3: LinkKey = ("s1", "s3", "forward")
L4: LinkKey = ("s3", "s4", "forward")

PATH_A: Path = (L1, L2)
PATH_B: Path = (L3, L4)

#: The config's weights (sum to 1.0).
W = CostWeights()


def m(util: float = 0.0, lat: float = 0.0, loss: float = 0.0,
      p: float = 0.0) -> LinkMetrics:
    """Shorthand for a link's measurements."""
    return LinkMetrics(utilization=util, latency_ms=lat, loss_pct=loss,
                       p_congestion=p)


def both(m1: LinkMetrics, m2: LinkMetrics) -> Dict[LinkKey, LinkMetrics]:
    """Metrics for the two two-hop candidate paths."""
    return {L1: m1, L2: m1, L3: m2, L4: m2}


# ------------------------------------------------------------ normalise --
def test_normalize_maps_into_the_unit_interval() -> None:
    assert normalize(0.0, 100.0) == 0.0
    assert normalize(50.0, 100.0) == 0.5
    assert normalize(100.0, 100.0) == 1.0


def test_normalize_saturates_rather_than_exceeding_one() -> None:
    """One outlier must not dominate a path's cost."""
    assert normalize(500.0, 100.0) == 1.0
    assert normalize(-5.0, 100.0) == 0.0


def test_normalize_rejects_a_non_positive_reference() -> None:
    for bad in (0.0, -1.0):
        with pytest.raises(RoutingError, match="must be positive"):
            normalize(1.0, bad)


def test_normalize_honours_a_nonzero_floor() -> None:
    assert normalize(10.0, 100.0, lo=10.0) == 0.0
    assert normalize(55.0, 100.0, lo=10.0) == 0.5


# ---------------------------------------------------------------- weights --
def test_default_weights_sum_to_one() -> None:
    total = (W.a_utilization + W.b_latency + W.c_loss + W.d_congestion)
    assert total == pytest.approx(1.0)


def test_weights_that_do_not_sum_to_one_are_rejected() -> None:
    """Unnormalised weights make costs incomparable between configurations."""
    with pytest.raises(RoutingError, match="must sum to 1.0"):
        CostWeights(a_utilization=0.5, b_latency=0.5, c_loss=0.5,
                    d_congestion=0.5)


def test_negative_weights_are_rejected() -> None:
    with pytest.raises(RoutingError, match="non-negative"):
        CostWeights(a_utilization=-0.1, b_latency=0.4, c_loss=0.3,
                    d_congestion=0.4)


def test_non_positive_normalisation_references_are_rejected() -> None:
    with pytest.raises(RoutingError, match="latency_ms reference"):
        CostWeights(latency_ms=0.0)


def test_weights_from_config_are_used() -> None:
    cfg = __import__("common.config", fromlist=["load_config"]).load_config()
    w = CostWeights.from_config(cfg)
    assert w.as_dict() == {
        "a_utilization": float(cfg.get("routing.weights.a_utilization")),
        "b_latency": float(cfg.get("routing.weights.b_latency")),
        "c_loss": float(cfg.get("routing.weights.c_loss")),
        "d_congestion": float(cfg.get("routing.weights.d_congestion")),
    }


# ------------------------------------------------------------ link cost --
def test_an_unmeasured_link_costs_nothing() -> None:
    """A fresh link is not congested; penalising it biases paths oddly."""
    assert link_cost(m(), W) == 0.0


def test_cost_rises_monotonically_with_each_signal() -> None:
    base = link_cost(m(), W)
    assert link_cost(m(util=1.0), W) > base
    assert link_cost(m(lat=50.0), W) > base
    assert link_cost(m(loss=5.0), W) > base
    assert link_cost(m(p=1.0), W) > base


def test_forecast_outweighs_present_load_when_weighted_so() -> None:
    """The ML signal is the point of P9: it must be able to dominate.

    d_congestion (0.40) exceeds a_utilization (0.25), so a link with a high
    forecast is costlier than one that is merely busy.
    """
    busy = m(util=0.9, p=0.05)
    risky = m(util=0.4, p=0.95)
    assert link_cost(risky, W) > link_cost(busy, W)


def test_terms_are_individually_bounded() -> None:
    """Each normalised term is a weight fraction, so the total cannot exceed 1."""
    terms = m(util=1.0, lat=1e6, loss=1e6, p=1.0).terms(W)
    assert all(0.0 <= v <= getattr(W, name) for v, name in zip(
        terms.values(),
        ("a_utilization", "b_latency", "c_loss", "d_congestion"),
    ))
    assert link_cost(m(util=1.0, lat=1e6, loss=1e6, p=1.0), W) <= 1.0 + 1e-9


def test_telemetry_row_conversion() -> None:
    lm = LinkMetrics.from_telemetry(
        {"utilization": 0.5, "latency_ms": 12.0, "loss_pct": 1.0,
         "capacity_mbps": 100.0}, p_congestion=0.8,
    )
    assert lm.utilization == 0.5
    assert lm.p_congestion == 0.8
    assert lm.bandwidth_mbps == 100.0


def test_utilisation_falls_back_to_rate_over_capacity() -> None:
    """Some telemetry rows carry no utilisation; rate/capacity is the fallback."""
    lm = LinkMetrics.from_telemetry(
        {"tx_rate_mbps": 25.0, "capacity_mbps": 100.0, "latency_ms": 1.0}
    )
    assert lm.utilization == pytest.approx(0.25)


def test_utilisation_fallback_with_no_capacity_is_zero() -> None:
    assert LinkMetrics.from_telemetry({"tx_rate_mbps": 25.0}).utilization == 0.0


# ----------------------------------------------------------- path cost --
def test_path_cost_is_the_worst_link_not_the_sum() -> None:
    """A max is what makes a path only as good as its weakest link.

    Summing would make cost scale with path length, quietly preferring a long
    path of cheap links over a short congested one.
    """
    metrics = {L1: m(util=0.1), L2: m(util=0.1),
               L3: m(util=0.1), L4: m(util=0.9)}
    assert path_cost(PATH_A, metrics, W) == pytest.approx(
        link_cost(m(util=0.1), W))
    assert path_cost(PATH_B, metrics, W) == pytest.approx(
        link_cost(m(util=0.9), W))
    assert path_cost(PATH_A, metrics, W) < path_cost(PATH_B, metrics, W)


def test_a_single_bad_link_poisons_the_path() -> None:
    metrics = {L1: m(util=0.05), L2: m(util=0.05, p=0.99)}
    assert path_cost((L1, L2), metrics, W) == pytest.approx(
        link_cost(m(util=0.05, p=0.99), W)
    )
    assert path_cost((L1, L2), metrics, W) > link_cost(m(util=0.05), W)


def test_empty_path_is_rejected() -> None:
    with pytest.raises(RoutingError, match="empty path"):
        path_cost((), both(m(), m()), W)


def test_path_with_unmeasured_link_is_rejected() -> None:
    with pytest.raises(RoutingError, match="no metrics for link"):
        path_cost((("s9", "s9", "forward"),), both(m(), m()), W)


# ------------------------------------------------------- choose_path --
def test_cheapest_path_wins_when_there_is_no_current_path() -> None:
    metrics = both(m(util=0.1), m(util=0.9))
    choice = choose_path([PATH_A, PATH_B], metrics, W)
    assert choice.best == PATH_A
    assert choice.reroute is False
    assert choice.reason == "no current path"


def test_no_candidates_is_rejected() -> None:
    with pytest.raises(RoutingError, match="no candidate paths"):
        choose_path([], both(m(), m()), W)


def test_an_impossible_ratio_is_rejected() -> None:
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(RoutingError, match="improvement_ratio"):
            choose_path([PATH_A, PATH_B], both(m(), m()), W,
                        improvement_ratio=bad)


def test_a_clearly_better_path_is_taken() -> None:
    """0.9 -> 0.1 is an 89% improvement, well past the 15% the guard demands."""
    metrics = both(m(util=0.9), m(util=0.1))
    choice = choose_path([PATH_A, PATH_B], metrics, W, current=PATH_A)
    assert choice.best == PATH_B
    assert choice.reroute is True
    assert "cheaper" in choice.reason


def test_a_marginal_improvement_is_refused() -> None:
    """The oscillation guard: 0.90 -> 0.88 is not worth the disruption."""
    metrics = both(m(util=0.9), m(util=0.88))
    choice = choose_path([PATH_A, PATH_B], metrics, W, current=PATH_A)
    assert choice.reroute is False
    assert "not" in choice.reason
    # The best path is still *reported*, so the caller can log it.
    assert choice.best == PATH_B


def test_a_larger_ratio_relaxes_the_guard() -> None:
    """With ratio 0.999 essentially any improvement is taken."""
    metrics = both(m(util=0.9), m(util=0.88))
    strict = choose_path([PATH_A, PATH_B], metrics, W, current=PATH_A,
                         improvement_ratio=0.85)
    loose = choose_path([PATH_A, PATH_B], metrics, W, current=PATH_A,
                        improvement_ratio=0.99)
    assert strict.reroute is False
    assert loose.reroute is True


def test_staying_put_when_already_cheapest() -> None:
    metrics = both(m(util=0.1), m(util=0.9))
    choice = choose_path([PATH_A, PATH_B], metrics, W, current=PATH_A)
    assert choice.reroute is False
    assert choice.reason == "current path is already cheapest"


def test_a_vanished_current_path_is_left_immediately() -> None:
    """No improvement guard: the current path is no longer an option."""
    metrics = {L1: m(util=0.5), L2: m(util=0.5),
               L3: m(util=0.1), L4: m(util=0.1)}
    gone: Path = (("s7", "s7", "forward"),)
    choice = choose_path([PATH_A, PATH_B], metrics, W, current=gone)
    assert choice.best == PATH_B
    assert choice.reroute is True
    assert "no longer available" in choice.reason


def test_ties_are_broken_deterministically() -> None:
    """Otherwise identical paths would swap the flow on every decision tick."""
    metrics = both(m(), m())
    first = choose_path([PATH_A, PATH_B], metrics, W)
    second = choose_path([PATH_B, PATH_A], metrics, W)
    assert first.best == second.best == min(PATH_A, PATH_B)


def test_choice_serialises_to_explainable_json() -> None:
    choice = choose_path([PATH_A, PATH_B], both(m(0.9), m(0.1)), W,
                         current=PATH_A)
    out = choice.as_dict()
    assert out["reroute"] is True
    assert out["best"] == ["s1->s3:forward", "s3->s4:forward"]
    assert "candidates" in out and len(out["candidates"]) == 2


# ------------------------------------------------------------- guard --
def test_fresh_flow_may_be_placed() -> None:
    guard = RerouteGuard(cooldown_s=10.0, min_dwell_s=8.0, now_fn=lambda: 0.0)
    allowed, reason = guard.may_reroute("f1", now=0.0)
    assert allowed is True
    assert "satisfied" in reason


def test_cooldown_blocks_an_immediate_second_reroute() -> None:
    guard = RerouteGuard(cooldown_s=10.0, min_dwell_s=0.0, now_fn=lambda: 0.0)
    guard.record("f1", now=0.0)
    allowed, reason = guard.may_reroute("f1", now=3.0)
    assert allowed is False
    assert "cooldown" in reason
    assert guard.may_reroute("f1", now=10.0)[0] is True


def test_dwell_blocks_a_move_right_after_arrival() -> None:
    """A flow that just arrived must not be moved again immediately."""
    guard = RerouteGuard(cooldown_s=0.0, min_dwell_s=8.0, now_fn=lambda: 0.0)
    guard.record("f1", now=100.0)
    allowed, reason = guard.may_reroute("f1", now=102.0)
    assert allowed is False
    assert "dwell" in reason
    assert guard.may_reroute("f1", now=108.0)[0] is True


def test_dwell_and_cooldown_are_both_enforced() -> None:
    guard = RerouteGuard(cooldown_s=10.0, min_dwell_s=8.0, now_fn=lambda: 0.0)
    guard.record("f1", now=0.0)     # arrived and placed at 0
    guard.record("f1", now=9.0)     # rerouted at 9
    # At t=10: dwell satisfied (9 > 8) but cooldown is not (1 < 10).
    allowed, reason = guard.may_reroute("f1", now=10.0)
    assert allowed is False
    assert "cooldown" in reason
    assert guard.may_reroute("f1", now=19.0)[0] is True


def test_guards_are_per_flow() -> None:
    """One flow's cooldown must not hold up an unrelated flow."""
    guard = RerouteGuard(cooldown_s=10.0, min_dwell_s=0.0, now_fn=lambda: 0.0)
    guard.record("f1", now=0.0)
    assert guard.may_reroute("f1", now=1.0)[0] is False
    assert guard.may_reroute("f2", now=1.0)[0] is True


def test_dwell_remaining_never_goes_negative() -> None:
    guard = RerouteGuard(min_dwell_s=8.0, now_fn=lambda: 0.0)
    guard.record("f1", now=0.0)
    assert guard.dwell_remaining("f1", now=2.0) == pytest.approx(6.0)
    assert guard.dwell_remaining("f1", now=100.0) == 0.0
    assert guard.dwell_remaining("unknown") == 0.0


def test_register_does_not_consume_the_cooldown() -> None:
    guard = RerouteGuard(cooldown_s=10.0, min_dwell_s=0.0, now_fn=lambda: 0.0)
    guard.register("f1", now=0.0)
    assert guard.may_reroute("f1", now=11.0)[0] is True


def test_reset_clears_all_state() -> None:
    guard = RerouteGuard(cooldown_s=100.0, min_dwell_s=0.0, now_fn=lambda: 0.0)
    guard.record("f1", now=0.0)
    assert guard.may_reroute("f1", now=1.0)[0] is False
    guard.reset()
    assert guard.may_reroute("f1", now=1.0)[0] is True


def test_negative_windows_are_rejected() -> None:
    with pytest.raises(RoutingError, match="non-negative"):
        RerouteGuard(cooldown_s=-1.0)
    with pytest.raises(RoutingError, match="non-negative"):
        RerouteGuard(min_dwell_s=-1.0)


def test_a_flow_cannot_be_flapped_by_the_guards_alone() -> None:
    """End-to-end: cost + improvement ratio + timers stop oscillation.

    Two paths of near-equal cost, and a decision tick every 2 s. Without the
    three guards the flow would alternate on every tick; with them it moves at
    most once per cooldown and never straight after arrival.
    """
    guard = RerouteGuard(cooldown_s=10.0, min_dwell_s=8.0, now_fn=lambda: 0.0)
    metrics = both(m(util=0.90, p=0.0), m(util=0.88, p=0.05))
    current = PATH_A
    guard.register("f1", now=0.0)

    moves: List[float] = []
    for tick in range(0, 61, 2):
        now = float(tick)
        choice = choose_path([PATH_A, PATH_B], metrics, W, current=current,
                             improvement_ratio=0.85)
        if not choice.reroute:
            continue
        allowed, _ = guard.may_reroute("f1", now=now)
        if not allowed:
            continue
        current = choice.best
        guard.record("f1", now=now)
        moves.append(now)

    # 0.90 vs 0.88 is a ~2% gain, far below the 15% the ratio demands, so the
    # cost guard alone should have prevented every move.
    assert moves == []


def test_a_freshly_registered_flow_is_not_cooldown_blocked() -> None:
    """Regression: ``register`` must not start the cooldown.

    It seeded ``_last`` as well as ``_arrived``, so a flow placed at t=0 could
    not be rerouted for ``cooldown_s`` -- regardless of how congested its path
    was. That is a property of ``record`` (a reroute happened), not of
    ``register`` (a flow was merely placed).
    """
    guard = RerouteGuard(cooldown_s=10.0, min_dwell_s=0.0, now_fn=lambda: 0.0)
    guard.register("f1", now=0.0)
    allowed, reason = guard.may_reroute("f1", now=0.0)
    assert allowed is True, f"blocked by: {reason}"


def test_record_still_starts_the_cooldown() -> None:
    """The complement of the test above: a real reroute does start it."""
    guard = RerouteGuard(cooldown_s=10.0, min_dwell_s=0.0, now_fn=lambda: 0.0)
    guard.register("f1", now=0.0)
    guard.record("f1", now=0.0)
    assert guard.may_reroute("f1", now=1.0)[0] is False
