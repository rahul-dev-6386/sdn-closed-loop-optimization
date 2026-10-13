"""Tests for the traffic-shape validator.

The central test is a regression: the *actual* rate series collected before the
generator was fixed must be rejected. A validator that only passes synthetic
input it was written alongside is not evidence of anything.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ml.validate import (  # noqa: E402
    DEFAULT_MAX_JUMP_FRACTION,
    link_rate_series,
    validate_runs,
    validate_shape,
)

# The measured profile from the defective `ramp` implementation: discrete levels
# separated by idle gaps, never above 10 Mbps.
DEFECTIVE_RAMP: List[float] = [
    0.0, 1.0, 2.1, 2.1, 2.1, 2.1, 1.5, 0.0, 0.0, 0.0, 0.0,
    1.8, 6.2, 6.2, 6.2, 6.2, 4.4, 0.0, 0.0, 0.0, 0.0,
    2.9, 10.0, 10.0, 10.0, 10.0, 7.6, 7.9, 10.0, 10.0, 10.0, 10.0,
    2.6, 0.0, 0.0, 0.0, 0.0, 2.5, 8.2, 8.2, 8.2, 8.2, 5.7,
    0.0, 0.0, 0.0, 0.0, 0.0,
]

# What the fixed implementation should produce: a continuous rise and fall.
GOOD_RAMP: List[float] = [
    2.0, 2.1, 2.0, 2.2, 4.0, 4.1, 4.0, 3.9, 6.0, 6.2, 6.0, 6.1,
    8.0, 8.1, 8.0, 7.9, 10.0, 10.2, 10.0, 10.1, 12.0, 11.9, 12.1, 12.0,
    10.0, 10.1, 9.9, 10.0, 8.0, 8.1, 7.9, 8.0, 6.0, 6.1, 5.9, 6.0,
    4.0, 4.1, 3.9, 4.0, 2.0, 2.1, 2.0, 1.9,
]


# ----------------------------------------------------------- the defect --
def test_the_actual_defective_profile_is_rejected() -> None:
    """The regression that matters: real collected data must fail validation.

    Without this the validator could be written to pass anything, and the 25 runs
    of square waves would have been accepted again.
    """
    verdict = validate_shape("run_ramp_s42", "ramp", DEFECTIVE_RAMP, peak_mbps=12.0)
    assert verdict.ok is False
    assert verdict.monotonic_rise is False
    assert verdict.idle_fraction > 0.2
    assert any("returned to idle" in r for r in verdict.reasons)


def test_a_continuous_ramp_is_accepted() -> None:
    verdict = validate_shape("run_ramp_s43", "ramp", GOOD_RAMP, peak_mbps=12.0)
    assert verdict.ok is True, verdict.reasons
    assert verdict.monotonic_rise is True
    assert verdict.reaches_peak is True
    assert verdict.smooth is True


def test_the_two_profiles_are_really_different() -> None:
    """Guards the fixture itself: if these were the same, the test proves nothing."""
    assert DEFECTIVE_RAMP != GOOD_RAMP
    assert min(DEFECTIVE_RAMP) < 0.5, "the defect includes idle gaps"
    assert min(GOOD_RAMP) > 0.5, "a continuous ramp never reaches idle"


# ------------------------------------------------------------- each check --
def test_a_load_that_never_ramps_is_rejected() -> None:
    flat = [5.0] * 30
    verdict = validate_shape("run_x", "ramp", flat, peak_mbps=12.0)
    assert verdict.ok is False
    assert verdict.reaches_peak is False
    assert any("below" in r for r in verdict.reasons)


def test_a_single_sample_step_is_rejected_as_unsmooth() -> None:
    """One sample from idle to full: the case with nothing to predict."""
    step = [0.0] * 15 + [12.0] * 15
    verdict = validate_shape("run_x", "ramp", step, peak_mbps=12.0)
    assert verdict.smooth is False
    assert any("step" in r for r in verdict.reasons)


def test_an_empty_series_is_rejected_rather_than_passing() -> None:
    verdict = validate_shape("run_x", "ramp", [], peak_mbps=12.0)
    assert verdict.ok is False
    assert verdict.reasons == ["no samples"]


def test_small_jitter_does_not_fail_smoothness() -> None:
    """Real telemetry is noisy. A validator that rejects noise rejects reality."""
    noisy = [v + (0.05 if i % 2 else -0.05) for i, v in enumerate(GOOD_RAMP)]
    verdict = validate_shape("run_x", "ramp", noisy, peak_mbps=12.0)
    assert verdict.smooth is True, verdict.reasons


def test_the_jump_threshold_is_configurable() -> None:
    """A 7 Mbps single-sample step, tested either side of a 6 Mbps threshold.

    Using an outright 0->12 step would only ever fail, so it could not show the
    threshold doing anything. This one genuinely straddles.
    """
    bumpy = list(GOOD_RAMP)
    bumpy[20] = bumpy[19] + 7.0
    peak = max(bumpy)
    strict = validate_shape("run_x", "ramp", bumpy, peak_mbps=peak,
                            max_jump_fraction=0.30)
    lenient = validate_shape("run_x", "ramp", bumpy, peak_mbps=peak,
                             max_jump_fraction=0.60)
    assert strict.smooth is False, "7 Mbps jump vs a 3.6 Mbps threshold"
    assert lenient.smooth is True, "7 Mbps jump vs a 7.2 Mbps threshold"


def test_a_vacuous_threshold_is_rejected() -> None:
    """Above 1.0 the smoothness check could never fail, so it checks nothing."""
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError, match="max_jump_fraction"):
            validate_shape("run_x", "ramp", GOOD_RAMP, peak_mbps=12.0,
                           max_jump_fraction=bad)


def test_an_invalid_peak_fraction_is_rejected() -> None:
    for bad in (0.0, -0.5, 2.0):
        with pytest.raises(ValueError, match="min_peak_fraction"):
            validate_shape("run_x", "ramp", GOOD_RAMP, peak_mbps=12.0,
                           min_peak_fraction=bad)


def test_the_default_jump_threshold_is_documented_as_a_fraction() -> None:
    assert 0 < DEFAULT_MAX_JUMP_FRACTION < 1


def test_peak_and_idle_statistics_are_reported() -> None:
    verdict = validate_shape("run_x", "ramp", GOOD_RAMP, peak_mbps=12.0)
    assert verdict.peak_mbps == pytest.approx(12.1)
    assert verdict.n_samples == len(GOOD_RAMP)
    assert verdict.idle_fraction == 0.0
    assert verdict.as_dict()["ok"] is True


# --------------------------------------------------------- series extract --
def test_series_is_taken_from_the_busiest_link() -> None:
    """Averaging across links would dilute a step into a smooth curve."""
    rows = []
    for i in range(10):
        rows.append({"src_switch": "s1", "dst_switch": "s2",
                     "timestamp": float(i), "tx_rate_mbps": "1.0"})
        rows.append({"src_switch": "s1", "dst_switch": "s3",
                     "timestamp": float(i), "tx_rate_mbps": "9.0"})
    rates = link_rate_series(rows)
    assert all(r == 9.0 for r in rates)


def test_series_is_time_ordered_even_if_rows_are_not() -> None:
    rows = [
        {"src_switch": "s1", "dst_switch": "s2", "timestamp": "20",
         "tx_rate_mbps": "2.0"},
        {"src_switch": "s1", "dst_switch": "s2", "timestamp": "10",
         "tx_rate_mbps": "1.0"},
        {"src_switch": "s1", "dst_switch": "s2", "timestamp": "30",
         "tx_rate_mbps": "3.0"},
    ]
    assert link_rate_series(rows) == [1.0, 2.0, 3.0]


def test_series_of_an_empty_run_is_empty() -> None:
    assert link_rate_series([]) == []


# ---------------------------------------------------------------- summary --
def test_validate_runs_summarises_and_gates() -> None:
    rows_a = [{"timestamp": float(i), "tx_rate_mbps": str(v)}
              for i, v in enumerate(GOOD_RAMP)]
    rows_b = [{"timestamp": float(i), "tx_rate_mbps": str(v)}
              for i, v in enumerate(DEFECTIVE_RAMP)]
    rows_by_run = {"run_good": rows_a, "run_bad": rows_b}
    patterns = {"run_good": "ramp", "run_bad": "ramp"}
    peaks = {"ramp": 12.0}

    strict = validate_runs(rows_by_run, patterns, peaks, strict=True)
    assert strict["usable"] is False
    assert strict["n_failed"] == 1
    assert strict["by_pattern"]["ramp"] == {"ok": 1, "failed": 1}

    lenient = validate_runs(rows_by_run, patterns, peaks, strict=False)
    assert lenient["usable"] is True, "non-strict reports but does not gate"


def test_patterns_without_a_declared_peak_are_not_checked() -> None:
    """Only patterns with an intended shape are asserted."""
    rows = [{"timestamp": float(i), "tx_rate_mbps": "0.0"} for i in range(10)]
    summary = validate_runs({"run_x": rows}, {"run_x": "elephant_mice"}, {})
    assert summary["n_runs_checked"] == 0
    assert summary["usable"] is True


def test_a_clean_sweep_is_usable() -> None:
    rows = [{"timestamp": float(i), "tx_rate_mbps": str(v)}
            for i, v in enumerate(GOOD_RAMP)]
    summary = validate_runs({"run_a": rows, "run_b": rows},
                            {"run_a": "ramp", "run_b": "ramp"}, {"ramp": 12.0})
    assert summary["usable"] is True
    assert summary["n_failed"] == 0
