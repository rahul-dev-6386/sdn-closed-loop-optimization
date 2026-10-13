"""Analysis of experiment results (P11).

Turns ``results/experiments.csv`` into the artefacts a reader needs to judge the
result: per-mode comparison tables, distributions, and -- most importantly -- a
statement of how much of the evidence actually supports the conclusion.

Statistical honesty here is not decoration. The experiment grid is 4 modes x 3
scenarios x N repetitions, and N is small because each run is a full Mininet
fabric held for 90 s. With N that small, a 5% difference in mean latency is
almost certainly noise. So every comparison carries an effect size and a
separation verdict, and the report says plainly when the data does not
distinguish two modes rather than ranking them anyway.

What is reported
----------------
* **Per-group summary**: mean and spread of every metric, plus n and recovery rate.
* **Effect size**: standardised mean difference (Cohen's d) with a pooled
  standard deviation, which is comparable across metrics on different scales.
* **Separation verdict**: ``separate`` when the 95% confidence intervals of the
  group means do not overlap, ``overlapping`` otherwise. Deliberately
  conservative -- it is an interval-overlap test, not a hypothesis test, so it
  under-claims rather than over-claims, which is the right direction for a
  claim that a new method is better.
* **Degradation audit**: how often each mode actually ran under its own label.
  A mode whose runs mostly fell back is reported separately and excluded from
  head-to-head claims, because comparing a real mode against a fallback
  measures the fallback.
"""

from __future__ import annotations

import csv
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from experiments.stats import (
    InsufficientData,
    RunMetrics,
    aggregate,
    mean,
    percentile,
    safe_stdev,
)

LOGGER = logging.getLogger("experiments.analyze")

#: Metrics compared across modes, with the direction that counts as better.
#: ``None`` means "not a quality metric" and is excluded from verdicts.
HEADLINE_METRICS: Dict[str, Optional[bool]] = {
    "mean_latency_ms": True,      # True == lower is better
    "p95_latency_ms": True,
    "throughput_mbps": False,     # False == higher is better
    "loss_pct": True,
    "utilization_balance": True,
    "congestion_duration_s": True,
    "recovery_time_s": True,
    "reroute_count": None,        # a cost, not a quality
}


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_results(csv_path: str | Path) -> List[RunMetrics]:
    """Read ``results/experiments.csv`` into :class:`RunMetrics` objects.

    Rows carrying an ``error`` are skipped: a run that failed is not a run with
    zero congestion, and averaging it in as zeros would bias every mode toward
    looking better than it is.

    Raises
    ------
    FileNotFoundError
        If the CSV does not exist.
    ValueError
        If no usable rows remain, so an empty or wholly-failed sweep is reported
        as such rather than analysed into an empty table.
    """
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"no experiment results at {path}. Run `make experiments` first."
        )

    runs: List[RunMetrics] = []
    skipped = 0
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if (row.get("error") or "").strip():
                skipped += 1
                continue

            def num(key: str) -> Optional[float]:
                raw = (row.get(key) or "").strip()
                if raw == "":
                    return None
                try:
                    value = float(raw)
                except ValueError:
                    return None
                return None if math.isnan(value) else value

            try:
                runs.append(RunMetrics(
                    mode=row["mode"], scenario=row["scenario"],
                    repetition=int(row["repetition"]),
                    mean_latency_ms=num("mean_latency_ms") or 0.0,
                    p95_latency_ms=num("p95_latency_ms") or 0.0,
                    throughput_mbps=num("throughput_mbps") or 0.0,
                    loss_pct=num("loss_pct") or 0.0,
                    utilization_balance=num("utilization_balance") or 0.0,
                    congestion_duration_s=num("congestion_duration_s") or 0.0,
                    recovery_time_s=num("recovery_time_s"),
                    reroute_count=int(num("reroute_count") or 0),
                    n_congestion_episodes=int(num("n_congestion_episodes") or 0),
                    peak_utilization=num("peak_utilization") or 0.0,
                    samples=int(num("samples") or 0),
                ))
            except (KeyError, ValueError):
                skipped += 1

    if not runs:
        raise ValueError(
            f"{path} contains no usable experiment rows "
            f"({skipped} skipped as failed). The sweep produced nothing to analyse."
        )
    LOGGER.info("loaded %d runs from %s (%d failed rows skipped)",
                len(runs), path.name, skipped)
    return runs


def load_context(csv_path: str | Path) -> Dict[str, Dict[str, Any]]:
    """Read the per-run context columns, keyed by run key.

    Used by the degradation audit, which needs to know each run's
    ``effective_mode``.
    """
    path = Path(csv_path)
    context: Dict[str, Dict[str, Any]] = {}
    if not path.is_file():
        return context
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = (f"{row.get('mode')}__{row.get('scenario')}"
                   f"__r{row.get('repetition')}")
            context[key] = {
                "effective_mode": row.get("effective_mode", "") or "",
                "degraded_ticks": row.get("degraded_ticks", "") or "",
                "fallbacks": row.get("fallbacks", "") or "",
                "error": row.get("error", "") or "",
            }
    return context


# --------------------------------------------------------------------------- #
# Effect size and separation
# --------------------------------------------------------------------------- #
def cohens_d(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """Standardised mean difference with a pooled standard deviation.

    Returns
    -------
    float or None
        ``None`` when either group has fewer than two observations, or the
        pooled spread is zero -- in which case any difference is infinitely
        "large" and reporting a number would be misleading.

    Signs
    -----
    Positive means ``a`` has the larger mean. Whether that is *better* depends
    on the metric, which is why the direction lives in :data:`HEADLINE_METRICS`
    rather than being baked into this function.
    """
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = safe_stdev(a), safe_stdev(b)
    pooled = math.sqrt((va * va + vb * vb) / 2.0)
    if pooled == 0.0:
        return None
    return (mean(a) - mean(b)) / pooled


def confidence_interval(values: Sequence[float], confidence: float = 0.95) -> Tuple[float, float]:
    """Normal-approximation confidence interval for a mean.

    Uses a normal approximation rather than Student's t because the repetition
    counts are small and the interval is used only for the overlap heuristic, not
    for a significance claim.
    """
    if not values:
        raise InsufficientData("no values")
    if len(values) < 2:
        v = float(values[0])
        return (v, v)
    se = safe_stdev(values) / math.sqrt(len(values))
    # 1.96 for 95%; the multiplier is the only approximation here.
    margin = 1.96 * se
    avg = mean(values)
    return (avg - margin, avg + margin)


def intervals_overlap(a: Sequence[float], b: Sequence[float]) -> bool:
    """Whether the two groups' mean confidence intervals overlap."""
    try:
        lo_a, hi_a = confidence_interval(a)
        lo_b, hi_b = confidence_interval(b)
    except InsufficientData:
        return True
    return not (hi_a < lo_b or hi_b < lo_a)


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
@dataclass
class Comparison:
    """One metric, one scenario, every mode compared."""

    scenario: str
    metric: str
    lower_is_better: bool
    #: mode -> {"values": [...], "mean": ..., "stdev": ..., "n": ...}
    groups: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: mode -> verdict against the best mode on this metric.
    verdicts: Dict[str, str] = field(default_factory=dict)
    best_mode: Optional[str] = None
    #: True when at least two modes are separated by non-overlapping intervals.
    any_separation: bool = False

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        return {
            "scenario": self.scenario,
            "metric": self.metric,
            "lower_is_better": self.lower_is_better,
            "groups": self.groups,
            "verdicts": self.verdicts,
            "best_mode": self.best_mode,
            "any_separation": self.any_separation,
        }


def compare_modes(
    runs: Sequence[RunMetrics], scenario: str, metric: str,
    lower_is_better: bool = True,
) -> Optional[Comparison]:
    """Compare every mode on one metric within one scenario.

    Returns ``None`` when the metric was never measured -- e.g. recovery time
    when nothing congested -- so an absent measurement is not treated as a tie.
    """
    values: Dict[str, List[float]] = {}
    for run in runs:
        if run.scenario != scenario:
            continue
        raw = getattr(run, metric, None)
        if raw is None:
            continue
        values.setdefault(run.mode, []).append(float(raw))
    if len(values) < 1:
        return None

    comparison = Comparison(
        scenario=scenario, metric=metric, lower_is_better=lower_is_better,
    )
    for mode, series in values.items():
        comparison.groups[mode] = {
            "values": [round(v, 4) for v in series],
            "mean": round(mean(series), 4),
            "stdev": round(safe_stdev(series), 4),
            "n": len(series),
            "ci95": [round(v, 4) for v in confidence_interval(series)]
            if len(series) >= 2 else None,
        }

    ranking = (min if lower_is_better else max)
    comparison.best_mode = ranking(
        comparison.groups, key=lambda m: comparison.groups[m]["mean"]
    )

    for mode, series in values.items():
        if len(series) < 2 or len(comparison.groups[comparison.best_mode]["values"]) < 2:
            comparison.verdicts[mode] = "insufficient repetitions"
            continue
        best_values = [v for v in
                       values[comparison.best_mode]]
        if mode == comparison.best_mode:
            comparison.verdicts[mode] = "best"
        elif intervals_overlap(series, best_values):
            comparison.verdicts[mode] = "overlapping with best"
        else:
            comparison.verdicts[mode] = "separated from best"

    comparison.any_separation = any(
        v == "separated from best" for v in comparison.verdicts.values()
    )
    return comparison


# --------------------------------------------------------------------------- #
# Degradation audit
# --------------------------------------------------------------------------- #
def audit_degradation(
    context: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Check that each run executed under the mode its row claims.

    Returns
    -------
    dict
        Per-mode counts of runs that ran as labelled versus degraded, plus the
        list of offending run keys.
    """
    per_mode: Dict[str, Dict[str, Any]] = {}
    offenders: List[str] = []
    for key, entry in context.items():
        mode = key.split("__", 1)[0]
        bucket = per_mode.setdefault(
            mode, {"runs": 0, "as_labelled": 0, "degraded": 0, "unknown": 0})
        bucket["runs"] += 1
        effective = entry.get("effective_mode", "")
        if not effective:
            # The run did not record a mode at all; that is itself a gap.
            bucket["unknown"] += 1
            offenders.append(key)
        elif effective == mode:
            bucket["as_labelled"] += 1
        else:
            bucket["degraded"] += 1
            offenders.append(key)

    for bucket in per_mode.values():
        labelled = bucket["runs"] - bucket["unknown"]
        bucket["as_labelled_fraction"] = (
            round(bucket["as_labelled"] / labelled, 4) if labelled else None
        )
    return {
        "per_mode": per_mode,
        "degraded_runs": offenders,
        "clean": not offenders,
    }


# --------------------------------------------------------------------------- #
# Top-level analysis
# --------------------------------------------------------------------------- #
@dataclass
class Analysis:
    """The complete analysis of one results CSV."""

    runs: List[RunMetrics]
    summary: Dict[str, Dict[str, Any]]
    comparisons: List[Comparison]
    degradation: Dict[str, Any]
    scenarios: List[str]
    modes: List[str]

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        return {
            "n_runs": len(self.runs),
            "modes": self.modes,
            "scenarios": self.scenarios,
            "summary": self.summary,
            "comparisons": [c.as_dict() for c in self.comparisons],
            "degradation_audit": self.degradation,
        }

    def headline(self, scenario: str, metric: str) -> Optional[Comparison]:
        """One comparison by scenario and metric."""
        for comparison in self.comparisons:
            if comparison.scenario == scenario and comparison.metric == metric:
                return comparison
        return None

    def modes_separated(self, scenario: str, metric: str) -> bool:
        """Whether the data distinguishes the best mode on this metric."""
        comparison = self.headline(scenario, metric)
        return bool(comparison and comparison.any_separation)


def analyse(runs: Sequence[RunMetrics],
            context: Optional[Dict[str, Dict[str, Any]]] = None) -> Analysis:
    """Run the full analysis.

    Raises
    ------
    ValueError
        If ``runs`` is empty.
    """
    runs = list(runs)
    if not runs:
        raise ValueError("cannot analyse an empty result set")

    scenarios = sorted({r.scenario for r in runs})
    modes = sorted({r.mode for r in runs})
    summary = aggregate(runs)

    comparisons: List[Comparison] = []
    for scenario in scenarios:
        for metric, lower_is_better in HEADLINE_METRICS.items():
            if lower_is_better is None:
                continue
            comparison = compare_modes(runs, scenario, metric, lower_is_better)
            if comparison is not None:
                comparisons.append(comparison)

    return Analysis(
        runs=runs, summary=summary, comparisons=comparisons,
        degradation=audit_degradation(context or {}),
        scenarios=scenarios, modes=modes,
    )


def analyse_csv(csv_path: str | Path) -> Analysis:
    """Load and analyse one results CSV in a single call."""
    return analyse(load_results(csv_path), load_context(csv_path))
