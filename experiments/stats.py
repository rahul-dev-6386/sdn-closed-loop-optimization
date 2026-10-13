"""Metrics for the experiment harness (P10).

The measurements here decide what the write-up concludes, so each one is defined
precisely and tested against hand-computed values. Four deserve comment:

**p95, not max.** Congestion is intermittent; a single worst sample from one run
of ten is usually measurement noise. The 95th percentile survives outliers while
still tracking the tail that a user would feel.

**Utilisation balance = standard deviation across links.** Congestion avoidance
that helps everyone at once shows up as load spreading, which is exactly a
reduction in this spread. A mode that merely lowers mean utilisation by starving
everything equally would look worse here, which is the intended bias.

**Congestion duration** counts *contiguous time above threshold*, not the number
of samples. One 10 s episode is one episode, not five 2 s samples.

**Recovery time** is measured from the end of a congestion episode to the first
sample back under threshold. It is what distinguishes a controller that avoided
the congestion from one that merely suffered it briefly, and it is the metric
that should separate the four routing modes most clearly.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("experiments.stats")


class InsufficientData(ValueError):
    """Raised when a metric cannot be computed from the data supplied."""


# --------------------------------------------------------------------------- #
# Elementary statistics
# --------------------------------------------------------------------------- #
def mean(values: Sequence[float]) -> float:
    """Arithmetic mean.

    Raises
    ------
    InsufficientData
        On an empty sequence, rather than returning NaN and letting it propagate
        into a results table as a blank cell.
    """
    if not values:
        raise InsufficientData("mean of an empty sequence")
    return sum(values) / len(values)


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, ``q`` in ``[0, 100]``.

    Uses the same convention as ``numpy.percentile`` with linear interpolation,
    so a reader comparing the two sees no discrepancy.

    Raises
    ------
    InsufficientData
        On an empty sequence.
    ValueError
        If ``q`` is outside ``[0, 100]``.
    """
    if not values:
        raise InsufficientData("percentile of an empty sequence")
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"percentile must be in [0, 100], got {q}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * (q / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[int(position)])
    weight = position - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def stdev(values: Sequence[float]) -> float:
    """Population standard deviation.

    The population form is correct here: the collected runs *are* the whole
    population being described, not a sample from a larger one.

    Raises
    ------
    InsufficientData
        With fewer than two values, where the spread is undefined.
    """
    if len(values) < 2:
        raise InsufficientData(
            "standard deviation needs at least two values, got "
            f"{len(values)}"
        )
    avg = mean(values)
    return math.sqrt(sum((v - avg) ** 2 for v in values) / len(values))


def safe_stdev(values: Sequence[float]) -> float:
    """Standard deviation, or 0.0 when undefined.

    For aggregating across runs where a single link may have been observed once,
    a zero spread is a more honest table entry than an exception.
    """
    try:
        return stdev(values)
    except InsufficientData:
        return 0.0


# --------------------------------------------------------------------------- #
# Utilisation balance
# --------------------------------------------------------------------------- #
def utilisation_balance(per_link_utilisation: Dict[str, float]) -> float:
    """Spread of utilisation across links; lower means better balanced.

    Returns 0.0 for a single link (no spread exists to measure) and for a set
    where every link carries identical load, which is the perfect case.

    Raises
    ------
    InsufficientData
        If no links were measured at all.
    """
    if not per_link_utilisation:
        raise InsufficientData("no link utilisation to compare")
    return safe_stdev(list(per_link_utilisation.values()))


# --------------------------------------------------------------------------- #
# Congestion episodes
# --------------------------------------------------------------------------- #
@dataclass
class CongestionEpisode:
    """One contiguous period above the congestion threshold."""

    start_s: float
    end_s: float
    duration_s: float
    peak_utilization: float

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        return {
            "start_s": round(self.start_s, 3),
            "end_s": round(self.end_s, 3),
            "duration_s": round(self.duration_s, 3),
            "peak_utilization": round(self.peak_utilization, 4),
        }


def find_episodes(
    timestamps: Sequence[float],
    congested: Sequence[bool],
    peak_per_sample: Optional[Sequence[float]] = None,
) -> List[CongestionEpisode]:
    """Extract maximal runs of congestion.

    Parameters
    ----------
    timestamps, congested:
        Parallel time-ordered series.
    peak_per_sample:
        Optional utilisation per sample, used to record each episode's peak.

    Raises
    ------
    ValueError
        If the two series differ in length.
    """
    if len(timestamps) != len(congested):
        raise ValueError(
            f"timestamps and congested must align: "
            f"{len(timestamps)} vs {len(congested)}"
        )
    episodes: List[CongestionEpisode] = []
    index = 0
    n = len(timestamps)
    while index < n:
        if not congested[index]:
            index += 1
            continue
        start = index
        while index < n and congested[index]:
            index += 1
        stop = index - 1
        span = timestamps[start:index]
        peak = 0.0
        if peak_per_sample is not None:
            peak = max(peak_per_sample[start:index], default=0.0)
        episodes.append(CongestionEpisode(
            start_s=float(timestamps[start]),
            end_s=float(timestamps[stop]),
            duration_s=float(timestamps[stop] - timestamps[start]),
            peak_utilization=float(peak),
        ))
    return episodes


def congestion_duration(episodes: Iterable[CongestionEpisode]) -> float:
    """Total seconds spent congested, summing disjoint episodes.

    Overlapping episodes cannot arise from :func:`find_episodes`, which returns
    maximal disjoint runs, so a simple sum is correct here. An explicit max with
    0.0 keeps the function total for callers passing an empty iterable.
    """
    return float(sum(episode.duration_s for episode in episodes))


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #
def recovery_time(
    timestamps: Sequence[float],
    congested: Sequence[bool],
    *,
    until_s: Optional[float] = None,
) -> Optional[float]:
    """Seconds from each congestion episode's end until utilisation recovers.

    "Recovered" means the run stays uncongested until ``until_s`` (or the end of
    the data). A controller that decongests and immediately re-congests has not
    recovered, and counting that as recovery would flatter it.

    Returns
    -------
    float or None
        Mean recovery time over the episodes that did recover, or ``None`` when
        no episode recovered. ``None`` is distinct from ``0.0``: it means the
        congestion never ended, which is a different finding from instant
        recovery.
    """
    episodes = find_episodes(timestamps, congested)
    if not episodes:
        return None
    limit = timestamps[-1] if until_s is None else until_s

    recoveries: List[float] = []
    for episode in episodes:
        # Strictly after the episode's last congested sample. Including that
        # sample would re-trigger the congestion check and make recovery
        # undetectable.
        tail = [i for i, t in enumerate(timestamps)
                if episode.end_s < t <= limit]
        if not tail:
            continue
        clean = True
        recovered_at: Optional[float] = None
        for i in tail:
            if congested[i]:
                clean = False
                break
            if recovered_at is None:
                recovered_at = float(timestamps[i])
        if clean and recovered_at is not None:
            recoveries.append(recovered_at - episode.end_s)
    if not recoveries:
        return None
    return float(sum(recoveries) / len(recoveries))


# --------------------------------------------------------------------------- #
# Per-run summary
# --------------------------------------------------------------------------- #
@dataclass
class RunMetrics:
    """Everything measured for one (mode, scenario, repetition) run."""

    mode: str
    scenario: str
    repetition: int
    mean_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    throughput_mbps: float = 0.0
    loss_pct: float = 0.0
    utilization_balance: float = 0.0
    congestion_duration_s: float = 0.0
    recovery_time_s: Optional[float] = None
    reroute_count: int = 0
    n_congestion_episodes: int = 0
    peak_utilization: float = 0.0
    samples: int = 0

    #: Column order for results/experiments.csv.
    COLUMNS: Tuple[str, ...] = (
        "mode", "scenario", "repetition", "mean_latency_ms", "p95_latency_ms",
        "throughput_mbps", "loss_pct", "utilization_balance",
        "congestion_duration_s", "recovery_time_s", "reroute_count",
        "n_congestion_episodes", "peak_utilization", "samples",
    )

    def as_row(self) -> Dict[str, Any]:
        """The run as a flat CSV-ready dict in :attr:`COLUMNS` order."""
        return {name: getattr(self, name) for name in self.COLUMNS}

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        return self.as_row()

    @property
    def recovered(self) -> bool:
        """Whether every congestion episode ended in a recovery."""
        return self.recovery_time_s is not None


def summarise_run(
    mode: str,
    scenario: str,
    repetition: int,
    timestamps: Sequence[float],
    congested: Sequence[bool],
    latency_ms: Sequence[float],
    throughput_mbps: Sequence[float],
    loss_pct: Sequence[float],
    per_link_utilisation: Dict[str, float],
    reroute_count: int = 0,
    utilization_threshold: float = 0.80,
) -> RunMetrics:
    """Reduce one run's raw series to :class:`RunMetrics`.

    Raises
    ------
    ValueError
        If the series are not the same length, since every metric is a per-sample
        aggregate and mismatched series would silently misalign.
    """
    lengths = {len(timestamps), len(congested), len(latency_ms),
               len(throughput_mbps), len(loss_pct)}
    if len(lengths) != 1:
        raise ValueError(
            "all series must be the same length, got "
            f"{len(timestamps)}, {len(congested)}, {len(latency_ms)}, "
            f"{len(throughput_mbps)}, {len(loss_pct)}"
        )
    if not timestamps:
        raise InsufficientData("cannot summarise an empty run")

    episodes = find_episodes(
        timestamps, congested,
        list(per_link_utilisation.values()) if per_link_utilisation else None,
    )
    peaks = [episode.peak_utilization for episode in episodes]

    return RunMetrics(
        mode=mode,
        scenario=scenario,
        repetition=repetition,
        mean_latency_ms=round(mean(latency_ms), 4),
        p95_latency_ms=round(percentile(latency_ms, 95.0), 4),
        throughput_mbps=round(mean(throughput_mbps), 4),
        loss_pct=round(mean(loss_pct), 4),
        utilization_balance=round(utilisation_balance(per_link_utilisation), 6),
        congestion_duration_s=round(congestion_duration(episodes), 3),
        recovery_time_s=(None if recovery_time(timestamps, congested) is None
                         else round(recovery_time(timestamps, congested), 3)),
        reroute_count=int(reroute_count),
        n_congestion_episodes=len(episodes),
        peak_utilization=round(max(peaks, default=0.0), 4),
        samples=len(timestamps),
    )


# --------------------------------------------------------------------------- #
# Aggregation across repetitions
# --------------------------------------------------------------------------- #
def aggregate(runs: Sequence[RunMetrics]) -> Dict[str, Dict[str, Any]]:
    """Group runs by ``(mode, scenario)`` and summarise each group.

    Reports mean and spread for every numeric metric, because a mean across ten
    repetitions presented without a spread invites over-reading a difference
    that the run-to-run noise does not support.
    """
    grouped: Dict[Tuple[str, str], List[RunMetrics]] = {}
    for run in runs:
        grouped.setdefault((run.mode, run.scenario), []).append(run)

    summary: Dict[str, Dict[str, Any]] = {}
    for (mode, scenario), group in sorted(grouped.items()):
        entry: Dict[str, Any] = {
            "mode": mode,
            "scenario": scenario,
            "n_runs": len(group),
            "recovery_rate": round(
                sum(1 for r in group if r.recovered) / len(group), 4),
        }
        for column in RunMetrics.COLUMNS:
            if column in {"mode", "scenario", "repetition"}:
                continue
            values = [getattr(r, column) for r in group
                      if getattr(r, column) is not None]
            if not values:
                # Never observed (e.g. no episode, so no recovery time). Report
                # absence explicitly rather than 0.0, which would read as
                # "instant recovery" rather than "not measured".
                entry[f"{column}_mean"] = None
                entry[f"{column}_stdev"] = None
                continue
            entry[f"{column}_mean"] = round(mean(values), 4)
            entry[f"{column}_stdev"] = round(safe_stdev(values), 4)
        summary[f"{mode}/{scenario}"] = entry
    return summary


def best_mode_per_scenario(
    summary: Dict[str, Dict[str, Any]],
    metric: str = "mean_latency_ms",
    lower_is_better: bool = True,
) -> Dict[str, Optional[str]]:
    """Which mode wins each scenario on ``metric``.

    Returns ``None`` for a scenario where the metric was never measured, so a
    missing measurement cannot be silently treated as the best or the worst.
    """
    winners: Dict[str, Optional[str]] = {}
    scenarios = sorted({entry["scenario"] for entry in summary.values()})
    for scenario in scenarios:
        scored = [
            (entry[f"{metric}_mean"], entry["mode"])
            for entry in summary.values()
            if entry["scenario"] == scenario
            and entry.get(f"{metric}_mean") is not None
        ]
        if not scored:
            winners[scenario] = None
            continue
        pick = min if lower_is_better else max
        winners[scenario] = pick(scored, key=lambda pair: pair[0])[1]
    return winners
