"""Tests for P11 analysis.

The point of these tests is not that the code produces a table, but that it
refuses to over-claim. With 3-10 repetitions of a 90 s fabric run, most
differences between modes are noise, and an analysis that silently ranks them
anyway would be the most consequential bug in the project.
"""
from __future__ import annotations

import csv
import math
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.analyze import (  # noqa: E402
    HEADLINE_METRICS,
    Analysis,
    analyse,
    analyse_csv,
    audit_degradation,
    cohens_d,
    compare_modes,
    confidence_interval,
    intervals_overlap,
    load_context,
    load_results,
)
from experiments.stats import RunMetrics  # noqa: E402


def make_run(mode: str, scenario: str, rep: int, *, latency: float = 10.0,
             **kwargs: Any) -> RunMetrics:
    """A run with sensible defaults for every metric."""
    defaults: Dict[str, Any] = dict(
        p95_latency_ms=latency + 4.0, throughput_mbps=10.0, loss_pct=0.0,
        utilization_balance=0.2, congestion_duration_s=5.0,
        recovery_time_s=4.0, reroute_count=1, n_congestion_episodes=1,
        peak_utilization=0.9, samples=45,
    )
    defaults.update(kwargs)
    return RunMetrics(mode=mode, scenario=scenario, repetition=rep,
                      mean_latency_ms=latency, **defaults)


def write_csv(path: Path, rows: List[Dict[str, Any]],
              extra: Dict[str, Any] = None) -> Path:
    """Write a results CSV from row dicts."""
    from scripts.run_experiments import RUN_CONTEXT_COLUMNS, csv_columns

    columns = csv_columns()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            full = {name: "" for name in columns}
            full.update(row)
            full.update(extra or {})
            writer.writerow(full)
    return path


# ------------------------------------------------------------- effect size --
def test_cohens_d_sign_and_magnitude() -> None:
    # Equal spread (population sd 1.4142), means 12 and 4, so d = 8 / 1.4142.
    a = [10.0, 11.0, 12.0, 13.0, 14.0]
    b = [2.0, 3.0, 4.0, 5.0, 6.0]
    assert cohens_d(a, b) == pytest.approx(8.0 / math.sqrt(2.0))
    assert cohens_d(b, a) == pytest.approx(-8.0 / math.sqrt(2.0))


def test_cohens_d_of_identical_groups_is_zero() -> None:
    assert cohens_d([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(0.0)


def test_cohens_d_needs_two_observations() -> None:
    assert cohens_d([1.0], [2.0, 3.0]) is None


def test_cohens_d_is_none_for_zero_spread() -> None:
    """Constant groups have infinite separation; a number would be a fiction."""
    assert cohens_d([1.0, 1.0, 1.0], [2.0, 2.0, 2.0]) is None


# ---------------------------------------------------------------- intervals --
def test_confidence_interval_brackets_the_mean() -> None:
    lo, hi = confidence_interval([10.0, 12.0, 11.0, 13.0, 9.0])
    assert lo < 11.0 < hi


def test_confidence_interval_of_one_value_is_a_point() -> None:
    assert confidence_interval([4.0]) == (4.0, 4.0)


def test_tighter_spread_gives_a_narrower_interval() -> None:
    tight = confidence_interval([10.0, 10.1, 9.9, 10.0, 10.05])
    loose = confidence_interval([1.0, 30.0, 2.0, 29.0, 15.0])
    assert (tight[1] - tight[0]) < (loose[1] - loose[0])


def test_overlap_detection() -> None:
    assert intervals_overlap([10.0, 10.5, 10.2], [10.4, 10.6, 10.5]) is True
    assert intervals_overlap([1.0, 1.1, 0.9], [50.0, 51.0, 49.0]) is False


def test_identical_groups_overlap() -> None:
    series = [5.0, 5.2, 4.8, 5.1]
    assert intervals_overlap(series, series) is True


# --------------------------------------------------------------- comparison --
def test_compare_identifies_the_best_mode() -> None:
    runs = [make_run("ml_aware", "ramp", i, latency=5.0) for i in range(5)]
    runs += [make_run("ecmp", "ramp", i, latency=20.0) for i in range(5)]
    comparison = compare_modes(runs, "ramp", "mean_latency_ms", True)
    assert comparison is not None
    assert comparison.best_mode == "ml_aware"
    assert comparison.verdicts["ml_aware"] == "best"


def test_compare_honours_higher_is_better() -> None:
    runs = [make_run("a", "s", i, throughput_mbps=20.0) for i in range(5)]
    runs += [make_run("b", "s", i, throughput_mbps=5.0) for i in range(5)]
    comparison = compare_modes(runs, "s", "throughput_mbps", False)
    assert comparison.best_mode == "a"


def test_noise_is_reported_as_overlapping_not_as_a_win() -> None:
    """The core honesty requirement.

    Two modes differing by 0.1 ms with normal run-to-run spread are not
    distinguishable at this sample size, and the analysis must say so.
    """
    # A 0.5 ms difference against a run-to-run spread of ~3 ms. At six
    # repetitions this is nowhere near distinguishable.
    jitter = [0.0, 3.0, -2.5, 1.5, -1.0, 2.0]
    runs = [make_run("ml_aware", "ramp", i, latency=10.0 + jitter[i])
            for i in range(6)]
    runs += [make_run("ecmp", "ramp", i, latency=10.5 + jitter[i])
             for i in range(6)]
    comparison = compare_modes(runs, "ramp", "mean_latency_ms", True)
    assert comparison.best_mode == "ml_aware"
    assert comparison.verdicts["ecmp"] == "overlapping with best"
    assert comparison.any_separation is False


def test_a_real_difference_is_reported_as_separated() -> None:
    runs = [make_run("ml_aware", "ramp", i, latency=5.0 + 0.1 * i)
            for i in range(6)]
    runs += [make_run("ecmp", "ramp", i, latency=90.0 + 0.1 * i)
             for i in range(6)]
    comparison = compare_modes(runs, "ramp", "mean_latency_ms", True)
    assert comparison.verdicts["ecmp"] == "separated from best"
    assert comparison.any_separation is True


def test_single_repetition_is_refused_a_verdict() -> None:
    runs = [make_run("a", "s", 0, latency=5.0), make_run("b", "s", 0, latency=50.0)]
    comparison = compare_modes(runs, "s", "mean_latency_ms", True)
    assert comparison.verdicts["a"] == "insufficient repetitions"
    assert comparison.verdicts["b"] == "insufficient repetitions"


def test_an_unmeasured_metric_yields_no_comparison() -> None:
    """No episode, so no recovery time -- not a tie, just absent."""
    runs = [make_run("a", "s", i, recovery_time_s=None) for i in range(4)]
    assert compare_modes(runs, "s", "recovery_time_s", True) is None


def test_reroute_count_is_not_treated_as_a_quality_metric() -> None:
    assert HEADLINE_METRICS["reroute_count"] is None


# -------------------------------------------------------------- degradation --
def test_audit_flags_runs_that_did_not_run_as_labelled() -> None:
    context = {
        "ml_aware__ramp__r0": {"effective_mode": "ml_aware", "degraded_ticks": "0"},
        "ml_aware__ramp__r1": {"effective_mode": "reactive_threshold",
                               "degraded_ticks": "9"},
        "ecmp__ramp__r0": {"effective_mode": "ecmp", "degraded_ticks": "0"},
    }
    audit = audit_degradation(context)
    assert audit["clean"] is False
    assert "ml_aware__ramp__r1" in audit["degraded_runs"]
    assert audit["per_mode"]["ecmp"]["as_labelled_fraction"] == pytest.approx(1.0)
    assert audit["per_mode"]["ml_aware"]["as_labelled_fraction"] == pytest.approx(0.5)


def test_audit_flags_runs_that_recorded_no_mode() -> None:
    audit = audit_degradation({"a__s__r0": {"effective_mode": ""}})
    assert audit["per_mode"]["a"]["unknown"] == 1
    assert audit["clean"] is False


def test_a_clean_sweep_audits_clean() -> None:
    context = {f"ml_aware__s__r{i}": {"effective_mode": "ml_aware"}
               for i in range(3)}
    assert audit_degradation(context)["clean"] is True


# ------------------------------------------------------------------ loading --
def test_loading_a_missing_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="make experiments"):
        load_results(tmp_path / "absent.csv")


def test_loading_skips_failed_runs(tmp_path: Path) -> None:
    """A failed run is not a run with zero congestion.

    Averaging it in as zeros would bias every mode toward looking better than
    it is, so the errored row must be dropped rather than coerced.
    """
    from scripts.run_experiments import csv_columns

    path = tmp_path / "e.csv"
    columns = csv_columns()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        good = {n: "" for n in columns}
        good.update(make_run("ml_aware", "ramp", 0).as_row())
        writer.writerow(good)
        bad = {n: "" for n in columns}
        bad.update(make_run("ml_aware", "ramp", 1).as_row())
        bad["error"] = "fabric refused to start"
        writer.writerow(bad)

    runs = load_results(path)
    assert len(runs) == 1
    assert runs[0].repetition == 0


def test_loading_a_wholly_failed_sweep_is_an_error(tmp_path: Path) -> None:
    from scripts.run_experiments import csv_columns

    columns = csv_columns()
    path = tmp_path / "e.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        row = {name: "" for name in columns}
        row.update(make_run("ml_aware", "ramp", 0).as_row())
        row["error"] = "fabric refused to start"
        writer.writerow(row)
    with pytest.raises(ValueError, match="nothing to analyse"):
        load_results(path)


def test_blank_recovery_time_loads_as_none(tmp_path: Path) -> None:
    """A run that never congested records an empty cell, not a zero."""
    row = make_run("ml_aware", "ramp", 0).as_row()
    row["recovery_time_s"] = ""
    runs = load_results(write_csv(tmp_path / "e.csv", [row]))
    assert len(runs) == 1
    assert runs[0].recovery_time_s is None


def test_context_is_read_per_run(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "e.csv",
                     [make_run("ml_aware", "ramp", 0).as_row()],
                     {"effective_mode": "reactive_threshold"})
    context = load_context(path)
    assert context["ml_aware__ramp__r0"]["effective_mode"] == "reactive_threshold"


# ---------------------------------------------------------------- analysis --
def test_analysis_covers_every_scenario_and_mode() -> None:
    runs = []
    for mode in ("ml_aware", "ecmp"):
        for scenario in ("baseline", "ramp_bursty"):
            runs += [make_run(mode, scenario, i, latency=5.0) for i in range(4)]
    analysis = analyse(runs)
    assert set(analysis.modes) == {"ml_aware", "ecmp"}
    assert set(analysis.scenarios) == {"baseline", "ramp_bursty"}
    assert analysis.comparisons


def test_analysis_of_an_empty_set_is_refused() -> None:
    with pytest.raises(ValueError, match="empty result set"):
        analyse([])


def test_analysis_serialises() -> None:
    runs = [make_run("ml_aware", "ramp", i) for i in range(4)]
    payload = analyse(runs).as_dict()
    assert payload["n_runs"] == 4
    assert "summary" in payload and "comparisons" in payload


def test_modes_separated_helper_answers_the_headline_question() -> None:
    runs = [make_run("ml_aware", "ramp", i, latency=5.0 + 0.01 * i)
            for i in range(6)]
    runs += [make_run("ecmp", "ramp", i, latency=80.0 + 0.01 * i)
             for i in range(6)]
    analysis = analyse(runs)
    assert analysis.modes_separated("ramp", "mean_latency_ms") is True


def test_analyse_csv_end_to_end(tmp_path: Path) -> None:
    rows = []
    for mode, latency in (("ml_aware", 5.0), ("ecmp", 50.0)):
        for i in range(5):
            row = make_run(mode, "ramp_bursty", i, latency=latency).as_row()
            rows.append(row)
    path = write_csv(tmp_path / "e.csv", rows, {"effective_mode": ""})
    analysis = analyse_csv(path)
    assert len(analysis.runs) == 10
    # No effective_mode recorded, so the audit must flag every run.
    assert analysis.degradation["clean"] is False
