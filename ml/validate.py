"""Validate the shape of traffic a run actually produced (P6).

Why this module exists
----------------------
The ``ramp`` pattern is supposed to offer load that climbs 2 -> 4 -> 6 -> 8 ->
10 -> 12 Mbps and falls back. It delivered a square wave: load switched from
idle to saturated inside a single 2 s sample, then returned to zero. Nothing
flagged it. Twenty-five runs of that data were collected, labelled, trained on
and reported -- every artefact looked perfectly plausible, and the only reason it
was caught was reading the raw link-rate profile by hand.

That is the failure mode this module closes. A dataset whose offered load does
not resemble the pattern that generated it is not usable evidence, and the
cheapest time to say so is immediately after collection, not after training.

What is checked
---------------
For each run and each pattern, against the *measured* link-rate series:

``monotonic_rise``
    During the rising phase, the offered rate must not return to idle. This is
    the exact defect: an earlier implementation restarted one iperf3 per step, so
    the rate fell to zero between steps.
``span``
    The rate must reach at least a configured fraction of the peak.
``smoothness``
    The largest single-sample jump must be small relative to the peak. A load
    that steps from zero to full in one sample carries no predictive signal at
    any horizon.

Each check reports a verdict rather than raising, so a caller can decide whether
to reject the run, and :func:`validate_runs` returns a machine-readable summary
that the collector writes next to the data.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

LOGGER = logging.getLogger("ml.validate")

#: Fraction of the configured peak a ramp must actually reach.
DEFAULT_MIN_PEAK_FRACTION = 0.75

#: Largest acceptable single-sample jump, as a fraction of peak. Above this the
#: load is a step, not a ramp, and there is nothing for a predictor to learn.
DEFAULT_MAX_JUMP_FRACTION = 0.35


@dataclass
class ShapeVerdict:
    """Whether one run's measured traffic matched its intended pattern."""

    run_id: str
    pattern: str
    n_samples: int
    peak_mbps: float
    median_mbps: float
    idle_fraction: float
    max_jump_mbps: float
    monotonic_rise: bool
    reaches_peak: bool
    smooth: bool
    ok: bool
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        data = asdict(self)
        for key in ("peak_mbps", "median_mbps", "max_jump_mbps"):
            data[key] = round(float(data[key]), 3)
        data["idle_fraction"] = round(float(data["idle_fraction"]), 4)
        return data


def link_rate_series(
    rows: Iterable[Mapping[str, Any]],
    *,
    src: Optional[str] = None,
    dst: Optional[str] = None,
) -> List[float]:
    """Extract the offered-rate series for one link from raw telemetry rows.

    Parameters
    ----------
    src, dst:
        Restrict to a single link. Defaults to the busiest link in the run,
        since traffic is directed along one path and averaging across links
        would dilute a step into a smooth curve and hide the defect.
    """
    per_link: Dict[tuple, List[tuple]] = {}
    for row in rows:
        key = (row.get("src_switch", ""), row.get("dst_switch", ""))
        if src and dst and key != (src, dst):
            continue
        try:
            rate = float(row.get("tx_rate_mbps") or 0.0)
            stamp = float(row.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            continue
        per_link.setdefault(key, []).append((stamp, rate))

    if not per_link:
        return []
    if src and dst:
        chosen = per_link.get((src, dst), [])
    else:
        # Sum the *rates*, not the (timestamp, rate) pairs -- `key=sum` would
        # add an int to a tuple and raise.
        chosen = max(per_link.values(),
                     key=lambda pairs: sum(rate for _, rate in pairs))
    chosen.sort(key=lambda pair: pair[0])
    return [rate for _, rate in chosen]


def validate_shape(
    run_id: str,
    pattern: str,
    rates: Sequence[float],
    *,
    peak_mbps: float,
    min_peak_fraction: float = DEFAULT_MIN_PEAK_FRACTION,
    max_jump_fraction: float = DEFAULT_MAX_JUMP_FRACTION,
    idle_mbps: float = 0.5,
) -> ShapeVerdict:
    """Check one run's rate series against the shape its pattern promises.

    Parameters
    ----------
    rates:
        Offered rate per sample, in time order.
    peak_mbps:
        The peak the pattern was configured to offer.
    idle_mbps:
        At or below this the link is treated as idle.

    Notes
    -----
    ``monotonic_rise`` is deliberately only about *not returning to idle*. A
    legitimate ramp need not be perfectly monotonic -- sampling noise and TCP
    behaviour both perturb it -- but a load that drops back to idle partway
    through a rise is not a rise, it is a series of bursts.
    """
    if not 0.0 < max_jump_fraction <= 1.0:
        raise ValueError(
            f"max_jump_fraction must be in (0, 1], got {max_jump_fraction}; "
            "a value above 1 would make the smoothness check vacuous"
        )
    if not 0.0 < min_peak_fraction <= 1.0:
        raise ValueError(
            f"min_peak_fraction must be in (0, 1], got {min_peak_fraction}"
        )

    values = [float(v) for v in rates]
    reasons: List[str] = []
    if not values:
        return ShapeVerdict(
            run_id=run_id, pattern=pattern, n_samples=0, peak_mbps=0.0,
            median_mbps=0.0, idle_fraction=1.0, max_jump_mbps=0.0,
            monotonic_rise=False, reaches_peak=False, smooth=False, ok=False,
            reasons=["no samples"],
        )

    peak = max(values)
    median = statistics.median(values)
    idle_fraction = sum(1 for v in values if v <= idle_mbps) / len(values)
    jumps = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    max_jump = max(jumps) if jumps else 0.0

    reaches_peak = peak >= peak_mbps * min_peak_fraction
    smooth = max_jump <= peak_mbps * max_jump_fraction

    # A rise is monotonic while the rate stays above idle. Find the first index
    # that reaches 80% of peak, then check nothing falls back to idle after it.
    rising = peak > 0
    threshold = peak * 0.8
    try:
        crest = next(i for i, v in enumerate(values) if v >= threshold)
        after_crest = values[crest:]
        monotonic_rise = all(v > idle_mbps for v in after_crest)
        if not monotonic_rise:
            idle_after = sum(1 for v in after_crest if v <= idle_mbps)
            reasons.append(
                f"offered load returned to idle {idle_after} time(s) after "
                f"reaching {peak:.1f} Mbps at t={crest}; a ramp must not "
                "restart from zero"
            )
    except StopIteration:
        monotonic_rise = False

    if not reaches_peak:
        reasons.append(
            f"peak {peak:.1f} Mbps is below "
            f"{min_peak_fraction:.0%} of the configured {peak_mbps:.1f} Mbps")
    if not smooth:
        reasons.append(
            f"largest single-sample jump {max_jump:.1f} Mbps exceeds "
            f"{max_jump_fraction:.0%} of peak; the load is a step, so there "
            "is nothing to predict")

    return ShapeVerdict(
        run_id=run_id, pattern=pattern, n_samples=len(values), peak_mbps=peak,
        median_mbps=median, idle_fraction=idle_fraction, max_jump_mbps=max_jump,
        monotonic_rise=monotonic_rise, reaches_peak=reaches_peak, smooth=smooth,
        ok=monotonic_rise and reaches_peak and smooth,
        reasons=reasons,
    )


def validate_runs(
    rows_by_run: Mapping[str, Sequence[Mapping[str, Any]]],
    patterns: Mapping[str, str],
    peaks: Mapping[str, float],
    *,
    strict: bool = True,
) -> Dict[str, Any]:
    """Validate every run and summarise.

    Parameters
    ----------
    rows_by_run:
        ``run_id -> raw telemetry rows``.
    patterns:
        ``run_id -> traffic pattern``.
    peaks:
        ``pattern -> configured peak in Mbps``.
    strict:
        When True, a failed run makes the summary report ``usable: False``.

    Returns
    -------
    dict
        A JSON-serialisable summary with one verdict per run.
    """
    verdicts: List[Dict[str, Any]] = []
    for run_id in sorted(rows_by_run):
        pattern = patterns.get(run_id, "unknown")
        peak = peaks.get(pattern)
        if peak is None:
            # No configured peak means no shape can be asserted. Only patterns
            # with an intended shape are checked, so this is not a failure.
            continue
        rates = link_rate_series(rows_by_run[run_id])
        verdict = validate_shape(run_id, pattern, rates, peak_mbps=float(peak))
        if not verdict.ok:
            LOGGER.warning("run %s (%s) did not produce the intended shape: %s",
                           run_id, pattern, "; ".join(verdict.reasons))
        verdicts.append(verdict.as_dict())

    failed = [v for v in verdicts if not v["ok"]]
    by_pattern: Dict[str, Dict[str, int]] = {}
    for verdict in verdicts:
        bucket = by_pattern.setdefault(verdict["pattern"], {"ok": 0, "failed": 0})
        bucket["ok" if verdict["ok"] else "failed"] += 1

    return {
        "usable": not failed or not strict,
        "n_runs_checked": len(verdicts),
        "n_failed": len(failed),
        "by_pattern": by_pattern,
        "verdicts": verdicts,
    }
