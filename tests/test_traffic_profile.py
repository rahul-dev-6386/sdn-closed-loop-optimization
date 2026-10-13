"""Tests for the continuous-load traffic generator.

The defect this replaces produced a square wave where a ramp was intended, and
25 runs of it reached the dataset unnoticed. These tests pin the property that
was missing: **the aggregate of the scheduled flows must match the requested
profile at every point in time.**

That is a pure-arithmetic property of the schedule, so it is testable without a
fabric. The live run is still the final check -- unit tests cannot detect a flow
that dies on the wire -- but they catch a schedule that is wrong in the first
place, and they would have caught the original defect.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Sequence

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traffic.generate import ScheduledFlow, ramp_plan  # noqa: E402

RAMP = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 10.0, 8.0, 6.0, 4.0, 2.0]
STEP = 10.0


def aggregate_at(flows: Sequence[ScheduledFlow], t: float) -> float:
    """Total Mbps offered at instant ``t`` by every live flow."""
    return sum(f.rate_mbps for f in flows
               if f.start_s <= t < f.start_s + f.life_s)


def expected_profile(profile: Sequence[float], step_s: float,
                     duration_s: float) -> List[float]:
    """The rate each step of a run should offer."""
    n_steps = max(1, int(round(duration_s / step_s)))
    return [profile[k % len(profile)] for k in range(n_steps)]


# ------------------------------------------------------------ the property --
def test_aggregate_matches_the_profile_at_every_step() -> None:
    """The assertion that was missing. Every step, not just the endpoints."""
    duration = 110.0
    flows = ramp_plan(RAMP, STEP, duration, unit_mbps=2.0)
    for step, want in enumerate(expected_profile(RAMP, STEP, duration)):
        # Sample inside the step, away from the boundary where a flow has just
        # started or just expired.
        t = step * STEP + STEP / 2
        got = aggregate_at(flows, t)
        assert got == pytest.approx(want), (
            f"step {step} (t={t:.0f}s): aggregate {got:.1f} Mbps, profile "
            f"says {want:.1f} Mbps"
        )


def test_the_ramp_rises_and_falls_and_never_touches_zero() -> None:
    """The specific property the old generator violated."""
    duration = 110.0
    flows = ramp_plan(RAMP, STEP, duration, unit_mbps=2.0)
    samples = [aggregate_at(flows, float(t))
               for t in range(0, int(duration), 2)]
    assert min(samples) > 0, "a continuous ramp must never return to idle"
    assert max(samples) == pytest.approx(12.0)
    rising = samples[: len(samples) // 2]
    assert rising == sorted(rising), "the first half must rise monotonically"


def test_the_profile_cycles_when_shorter_than_the_run() -> None:
    """A run longer than one cycle of the profile must repeat it.

    Checked step by step rather than at one hand-picked instant: picking a
    single point is how an off-by-one in the cycle boundary slips through.
    """
    duration = 220.0
    flows = ramp_plan(RAMP, STEP, duration, unit_mbps=2.0)
    for step, want in enumerate(expected_profile(RAMP, STEP, duration)):
        t = step * STEP + STEP / 2
        assert aggregate_at(flows, t) == pytest.approx(want), f"step {step}"


# --------------------------------------------------------------- edge cases --
def test_a_genuine_square_wave_is_still_representable() -> None:
    """bursty_onoff is meant to be an on/off wave and is left alone.

    The fix must not have made a legitimate square wave impossible -- asserting a
    continuous rise-and-fall everywhere would have "fixed" a pattern that was
    already correct.
    """
    bursty = [10.0, 10.0, 10.0, 10.0, 10.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    flows = ramp_plan(bursty, STEP, 100.0, unit_mbps=2.0)
    assert aggregate_at(flows, 20.0) == pytest.approx(10.0)
    assert aggregate_at(flows, 70.0) == pytest.approx(0.0)


def test_a_stochastic_profile_is_followed_not_smoothed() -> None:
    """poisson fluctuates; the schedule must follow it, not average it away."""
    rng_profile = [10.3, 2.4, 9.6, 2.2, 9.2, 8.3, 5.3, 5.0, 11.3, 7.8, 3.6]
    duration = 110.0
    flows = ramp_plan(rng_profile, STEP, duration, unit_mbps=1.0)
    for step, want in enumerate(expected_profile(rng_profile, STEP, duration)):
        t = step * STEP + STEP / 2
        assert aggregate_at(flows, t) == pytest.approx(want, abs=1.0)


def test_each_flow_gets_its_own_port() -> None:
    """Two flows sharing a port would silently sum into whatever else uses it."""
    flows = ramp_plan(RAMP, STEP, 110.0, unit_mbps=2.0)
    ports = [f.port for f in flows]
    assert len(ports) == len(set(ports)), f"port reuse: {ports}"


def test_flows_live_inside_the_run() -> None:
    flows = ramp_plan(RAMP, STEP, 110.0, unit_mbps=2.0)
    for flow in flows:
        assert 0 <= flow.start_s < 110.0
        assert flow.life_s > 0


def test_a_run_shorter_than_one_step_still_offers_load() -> None:
    flows = ramp_plan(RAMP, STEP, 5.0, unit_mbps=2.0)
    assert flows, "a short run must still offer its base load"
    assert aggregate_at(flows, 2.0) > 0


def test_finer_units_track_the_profile_more_closely() -> None:
    """Unit size is the schedule's approximation error."""
    coarse = ramp_plan(RAMP, STEP, 110.0, unit_mbps=4.0)
    fine = ramp_plan(RAMP, STEP, 110.0, unit_mbps=1.0)
    # 6 Mbps is not a multiple of 4, so the coarse schedule must miss it.
    assert aggregate_at(coarse, 25.0) != pytest.approx(6.0)
    assert aggregate_at(fine, 25.0) == pytest.approx(6.0)


def test_one_flow_spans_the_whole_run() -> None:
    """For a ramp, something is live from the first second to the last.

    Scoped to ramp deliberately: a pattern with an idle phase is supposed to
    reach zero, and asserting continuous coverage everywhere would have flagged
    bursty_onoff's correct behaviour as a defect.
    """
    flows = ramp_plan(RAMP, STEP, 110.0, unit_mbps=2.0)
    longest = max(flows, key=lambda f: f.life_s)
    assert longest.start_s == 0.0
    assert longest.life_s == pytest.approx(110.0)


# ------------------------------------------------- regressions from today --
def test_a_repeating_profile_is_not_pinned_at_peak() -> None:
    """Regression: one interval per unit broke the second cycle.

    An earlier version gave each unit a single interval from its first step to
    its last. For a two-cycle ramp that meant a unit needed in steps 1-9 *and*
    12-20 stayed alive across steps 10-11, so the aggregate read
    "climb then sit at maximum" instead of cycling.
    """
    duration = 220.0
    flows = ramp_plan(RAMP, STEP, duration, unit_mbps=2.0)
    for step, want in enumerate(expected_profile(RAMP, STEP, duration)):
        t = step * STEP + STEP / 2
        assert aggregate_at(flows, t) == pytest.approx(want), (
            f"step {step}: {aggregate_at(flows, t):.1f} != {want:.1f}"
        )
    # And the peak is reached twice, not once then held.
    peaks = [t for t in range(0, int(duration), 2)
             if aggregate_at(flows, float(t)) == pytest.approx(12.0)]
    assert len(peaks) >= 4, f"peak held across {len(peaks)} samples"


def test_an_idle_phase_is_representable() -> None:
    """Regression: a forced minimum of one unit made zero impossible.

    ``bursty_onoff`` is supposed to fall to zero. An earlier version clamped
    every step to need at least one unit, so it could not -- and any attempt to
    use this scheduler for that pattern would have silently flattened it.
    """
    bursty = [10.0] * 5 + [0.0] * 4
    flows = ramp_plan(bursty, STEP, 100.0, unit_mbps=2.0)
    assert aggregate_at(flows, 20.0) == pytest.approx(10.0)
    assert aggregate_at(flows, 60.0) == pytest.approx(0.0)
    assert aggregate_at(flows, 95.0) == pytest.approx(10.0), "and it comes back"


def test_a_zero_duration_is_rejected() -> None:
    for bad_step, bad_unit in ((0.0, 2.0), (10.0, 0.0), (10.0, -1.0)):
        with pytest.raises(ValueError):
            ramp_plan(RAMP, bad_step, 100.0, unit_mbps=bad_unit)


def test_a_unit_reuses_its_port_only_across_disjoint_runs() -> None:
    """Safe because the runs are disjoint, but worth pinning.

    If two overlapping runs of the same unit ever shared a port, their rates
    would sum into whatever else used that port and the shape would drift.
    """
    duration = 220.0
    flows = ramp_plan(RAMP, STEP, duration, unit_mbps=2.0)
    by_port: dict = {}
    for flow in flows:
        by_port.setdefault(flow.port, []).append(
            (flow.start_s, flow.start_s + flow.life_s))
    for port, spans in by_port.items():
        spans.sort()
        for (_, end_a), (start_b, _) in zip(spans, spans[1:]):
            assert end_a <= start_b, f"port {port} has overlapping runs"


def test_a_single_step_profile_is_supported() -> None:
    flows = ramp_plan([8.0], STEP, 100.0, unit_mbps=2.0)
    assert aggregate_at(flows, 50.0) == pytest.approx(8.0)
