"""Tests for P10 metrics, checked against hand-computed values.

These numbers end up in the write-up's conclusion, so each is verified against
an independently derived answer rather than a previous run of the same code.
"""
from __future__ import annotations

import math
from typing import List

import pytest

from experiments.stats import (
    CongestionEpisode,
    InsufficientData,
    RunMetrics,
    aggregate,
    best_mode_per_scenario,
    congestion_duration,
    find_episodes,
    mean,
    percentile,
    recovery_time,
    safe_stdev,
    stdev,
    summarise_run,
    utilisation_balance,
)

numpy = pytest.importorskip("numpy", reason="cross-check needs numpy")


# ------------------------------------------------------- elementary stats --
def test_mean_is_hand_checked() -> None:
    assert mean([2.0, 4.0, 6.0]) == 4.0
    assert mean([5.0]) == 5.0


def test_mean_of_nothing_raises_rather_than_returning_nan() -> None:
    """A NaN propagating into a results table reads as a blank cell."""
    with pytest.raises(InsufficientData, match="empty sequence"):
        mean([])


def test_percentile_is_hand_checked() -> None:
    # 1..10, p95 sits at index 8.55 -> between 9 and 10.
    assert percentile(list(range(1, 11)), 95.0) == pytest.approx(9.55)
    assert percentile(list(range(1, 11)), 50.0) == pytest.approx(5.5)
    assert percentile([7.0], 95.0) == 7.0
    assert percentile([3.0, 1.0, 2.0], 0.0) == 1.0
    assert percentile([3.0, 1.0, 2.0], 100.0) == 3.0


def test_percentile_matches_numpy() -> None:
    rng = numpy.random.default_rng(7)
    data = rng.normal(size=257).tolist()
    for q in (0.0, 25.0, 50.0, 90.0, 95.0, 99.0, 100.0):
        assert percentile(data, q) == pytest.approx(
            float(numpy.percentile(data, q)))


def test_percentile_rejects_an_out_of_range_q() -> None:
    for bad in (-1.0, 101.0):
        with pytest.raises(ValueError, match="must be in"):
            percentile([1.0, 2.0], bad)


def test_stdev_is_hand_checked() -> None:
    # Population stdev of [2,4,4,4,5,5,7,9] is exactly 2.
    assert stdev([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]) == pytest.approx(2.0)


def test_stdev_matches_numpy() -> None:
    rng = numpy.random.default_rng(11)
    data = rng.normal(size=100).tolist()
    assert stdev(data) == pytest.approx(float(numpy.std(data)))


def test_stdev_needs_two_values() -> None:
    with pytest.raises(InsufficientData, match="at least two"):
        stdev([1.0])


def test_safe_stdev_returns_zero_instead_of_raising() -> None:
    assert safe_stdev([1.0]) == 0.0


# ------------------------------------------------------------- balance --
def test_uniform_load_is_perfectly_balanced() -> None:
    assert utilisation_balance({"a": 0.5, "b": 0.5, "c": 0.5}) == 0.0


def test_balance_is_the_spread_across_links() -> None:
    assert utilisation_balance({"a": 0.5, "b": 0.7}) == pytest.approx(
        stdev([0.5, 0.7]))


def test_balance_of_one_link_is_zero() -> None:
    assert utilisation_balance({"only": 0.9}) == 0.0


def test_balance_with_no_links_raises() -> None:
    with pytest.raises(InsufficientData, match="no link utilisation"):
        utilisation_balance({})


# ------------------------------------------------------------- episodes --
def test_one_episode_is_measured_by_duration_not_sample_count() -> None:
    """Five 2 s samples are one 8 s episode, not five episodes."""
    ts = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]
    congested = [False, True, True, True, True, False]
    episodes = find_episodes(ts, congested)
    assert len(episodes) == 1
    assert episodes[0].start_s == 2.0
    assert episodes[0].end_s == 8.0
    assert episodes[0].duration_s == 6.0


def test_separate_episodes_are_not_merged() -> None:
    ts = [0.0, 1.0, 2.0, 3.0, 4.0]
    episodes = find_episodes(ts, [True, False, True, False, True])
    assert len(episodes) == 3
    assert [e.duration_s for e in episodes] == [0.0, 0.0, 0.0]


def test_no_congestion_yields_no_episodes() -> None:
    assert find_episodes([0.0, 1.0], [False, False]) == []


def test_episode_peak_is_recorded() -> None:
    ts = [0.0, 1.0, 2.0]
    episodes = find_episodes(ts, [True, True, False], [0.7, 0.95, 0.2])
    assert episodes[0].peak_utilization == pytest.approx(0.95)


def test_mismatched_series_raise() -> None:
    with pytest.raises(ValueError, match="must align"):
        find_episodes([0.0, 1.0], [True])


def test_congestion_duration_sums_episodes() -> None:
    episodes = [CongestionEpisode(0, 6, 6.0, 0.9),
                CongestionEpisode(10, 14, 4.0, 0.85)]
    assert congestion_duration(episodes) == pytest.approx(10.0)


def test_congestion_duration_of_nothing_is_zero() -> None:
    assert congestion_duration([]) == 0.0


# ------------------------------------------------------------- recovery --
def test_recovery_is_time_from_episode_end_to_clean() -> None:
    ts = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]
    congested = [True, True, False, False, False, False]
    assert recovery_time(ts, congested) == pytest.approx(2.0)


def test_recovery_is_none_when_congestion_never_ends() -> None:
    """Different from 0.0: "never recovered" is not "recovered instantly"."""
    assert recovery_time([0.0, 1.0, 2.0], [True, True, True]) is None


def test_decongesting_then_recongesting_is_not_recovery() -> None:
    """A brief clean sample does not count: the run must stay clean to the end.

    Series [T, F, T, F, F] has two episodes. The first decongests for one sample
    and then recongests, so it did *not* recover and is excluded from the mean.
    The second recovers at t=3, one second after it ended at t=2. The reported
    value is therefore 1.0 -- episode two only.
    """
    ts = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert recovery_time(ts, [True, False, True, False, False]) == pytest.approx(
        1.0)


def test_only_the_final_episode_failing_yields_no_recovery() -> None:
    """If nothing ever settles, there is no recovery time to report."""
    ts = [0.0, 1.0, 2.0, 3.0]
    # Second episode runs to the final sample, so it never recovers.
    assert recovery_time(ts, [True, False, True, True]) is None


def test_no_episode_means_none_not_zero() -> None:
    assert recovery_time([0.0, 1.0], [False, False]) is None


def test_recovery_is_averaged_over_episodes() -> None:
    ts = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    congested = [True, False, False, False, True, False, False]
    # Episode 1 ends at t=0, recovers at t=1 -> 1 s. Episode 2 ends at t=4,
    # recovers at t=5 -> 1 s. Mean 1 s.
    assert recovery_time(ts, congested) == pytest.approx(1.0)


# --------------------------------------------------------- summarise_run --
def test_summarise_run_produces_every_metric() -> None:
    n = 40
    ts = [i * 2.0 for i in range(n)]
    congested = [i >= 20 for i in range(n)]
    metrics = summarise_run(
        "ml_aware", "ramp_bursty", 1, ts, congested,
        latency_ms=[5.0 + i * 0.5 for i in range(n)],
        throughput_mbps=[10.0] * n,
        loss_pct=[0.0] * (n - 5) + [1.0] * 5,
        per_link_utilisation={"s1-s2": 0.9, "s1-s3": 0.4},
        reroute_count=3,
    )
    assert metrics.mode == "ml_aware"
    assert metrics.n_congestion_episodes == 1
    assert metrics.congestion_duration_s == pytest.approx(38.0)
    assert metrics.reroute_count == 3
    assert metrics.samples == n
    assert metrics.mean_latency_ms > metrics.p95_latency_ms * 0.5


def test_summarise_run_rejects_mismatched_series() -> None:
    with pytest.raises(ValueError, match="same length"):
        summarise_run("m", "s", 1, [0.0, 1.0], [False], [1.0], [1.0], [0.0], {})


def test_summarise_run_rejects_an_empty_run() -> None:
    with pytest.raises(InsufficientData, match="empty run"):
        summarise_run("m", "s", 1, [], [], [], [], [], {})


def test_run_metrics_columns_match_the_row() -> None:
    metrics = RunMetrics(mode="ecmp", scenario="baseline", repetition=3)
    assert set(metrics.as_row()) == set(RunMetrics.COLUMNS)
    assert list(metrics.as_row()) == list(RunMetrics.COLUMNS)


def test_a_run_with_no_congestion_has_no_recovery_time() -> None:
    n = 20
    metrics = summarise_run(
        "static", "baseline", 1, [i * 2.0 for i in range(n)],
        [False] * n, [5.0] * n, [10.0] * n, [0.0] * n, {"a": 0.2},
    )
    assert metrics.n_congestion_episodes == 0
    assert metrics.recovery_time_s is None
    assert metrics.recovered is False
    assert metrics.congestion_duration_s == 0.0


# ------------------------------------------------------------ aggregate --
def _run(mode: str, scenario: str, rep: int, latency: float,
         recovery: float | None = 5.0,
         throughput: float = 10.0) -> RunMetrics:
    return RunMetrics(mode=mode, scenario=scenario, repetition=rep,
                      mean_latency_ms=latency, p95_latency_ms=latency + 2,
                      throughput_mbps=throughput, loss_pct=0.1,
                      utilization_balance=0.2, congestion_duration_s=4.0,
                      recovery_time_s=recovery, reroute_count=2,
                      n_congestion_episodes=1, peak_utilization=0.9, samples=40)


def test_aggregate_groups_by_mode_and_scenario() -> None:
    summary = aggregate([
        _run("ml_aware", "ramp_bursty", 1, 5.0),
        _run("ml_aware", "ramp_bursty", 2, 7.0),
        _run("ecmp", "ramp_bursty", 1, 9.0),
    ])
    assert set(summary) == {"ml_aware/ramp_bursty", "ecmp/ramp_bursty"}
    assert summary["ml_aware/ramp_bursty"]["n_runs"] == 2
    assert summary["ml_aware/ramp_bursty"]["mean_latency_ms_mean"] == 6.0


def test_aggregate_reports_spread_alongside_the_mean() -> None:
    """A mean without a spread invites over-reading noise."""
    summary = aggregate([_run("m", "s", 1, 5.0), _run("m", "s", 2, 9.0)])
    assert summary["m/s"]["mean_latency_ms_mean"] == 7.0
    assert summary["m/s"]["mean_latency_ms_stdev"] == pytest.approx(2.0)


def test_aggregate_reports_absent_metrics_as_none_not_zero() -> None:
    """Zero would read as "instant recovery", which is a different claim."""
    summary = aggregate([_run("m", "s", 1, 5.0, recovery=None)])
    assert summary["m/s"]["recovery_time_s_mean"] is None


def test_aggregate_reports_the_recovery_rate() -> None:
    summary = aggregate([
        _run("m", "s", 1, 5.0, recovery=4.0),
        _run("m", "s", 2, 5.0, recovery=None),
    ])
    assert summary["m/s"]["recovery_rate"] == pytest.approx(0.5)


# --------------------------------------------------------- best per mode --
def test_best_mode_picks_the_lowest_mean() -> None:
    summary = aggregate([
        _run("ml_aware", "ramp", 1, 5.0),
        _run("ecmp", "ramp", 1, 9.0),
        _run("static", "ramp", 1, 20.0),
    ])
    assert best_mode_per_scenario(summary)["ramp"] == "ml_aware"


def test_best_mode_can_prefer_higher() -> None:
    """Same runs, opposite metric: b has the better throughput despite worse latency."""
    summary = aggregate([_run("a", "s", 1, 5.0, throughput=8.0),
                         _run("b", "s", 1, 9.0, throughput=14.0)])
    assert best_mode_per_scenario(summary, metric="mean_latency_ms")["s"] == "a"
    assert best_mode_per_scenario(summary, metric="throughput_mbps",
                                  lower_is_better=False)["s"] == "b"


def test_best_mode_returns_none_when_unmeasured() -> None:
    """A missing measurement must not be scored as best or worst."""
    summary = aggregate([_run("a", "s", 1, 5.0, recovery=None),
                         _run("b", "s", 1, 5.0, recovery=None)])
    assert best_mode_per_scenario(summary, metric="recovery_time_s")["s"] is None
