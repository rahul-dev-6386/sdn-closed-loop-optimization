"""Feature construction for the future-congestion predictor (P7).

Design rules this module exists to enforce
------------------------------------------
**Predict the future, never re-derive the present.** Every feature is a
function of the *trailing* window ``t-W .. t``. The label is computed
independently, by :mod:`ml.features` only reading rows whose timestamp lies at
or after ``t + label_gap_s`` and within ``t + label_gap_s + horizon_n_s``.
Nothing here looks forward, so leakage is structurally impossible rather than
merically avoided.

**No identity features.** Link identifiers (``src_switch``, ``dst_switch``,
ports) are explicitly excluded. They would let the model memorise "link s1-s2 is
busy" instead of learning the traffic signal, and would not transfer to P13's
second topology. The allowlist is enforced in code, not by convention.

**No NaNs.** A row that cannot supply a full window is dropped, never
imputed -- imputing a rate invents a measurement that was never taken.

The feature set per link is, for each of the seven allowlisted telemetry
fields: the last value, the rolling mean, the rolling slope (per second), and
the rolling standard deviation. Slope is what lets the model distinguish "has
been high for a while" from "just spiked", which is the difference between a
link that is about to congest and one that is merely busy.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("ml.features")

#: Fields permitted as feature inputs. Identity fields are deliberately absent.
BASE_FIELDS: Tuple[str, ...] = (
    "tx_rate_mbps",
    "rx_rate_mbps",
    "tx_packet_rate_pps",
    "rx_packet_rate_pps",
    "utilization",
    "latency_ms",
    "loss_pct",
)

#: Statistics computed over the trailing window for every base field.
WINDOW_STATS: Tuple[str, ...] = ("last", "mean", "slope", "std")

#: Explicitly forbidden inputs, rejected if present.
FORBIDDEN_FIELDS: Tuple[str, ...] = (
    "src_switch", "dst_switch", "src_port", "dst_port",
    "src_dpid", "dst_dpid", "link", "direction", "run_id", "pattern",
    "label_future_congested", "lead_time_s", "is_congested", "horizon_valid",
)


def feature_names(fields: Sequence[str] = BASE_FIELDS,
                 stats: Sequence[str] = WINDOW_STATS) -> List[str]:
    """Deterministic feature column order.

    The order is stable so a saved model's ``feature_names_`` can be compared
    against a freshly built matrix at inference time (P8).
    """
    return [f"{field}__{stat}" for field in fields for stat in stats]


def assert_no_identity_features(columns: Iterable[str]) -> None:
    """Raise if any feature column is an identity or target field.

    Parameters
    ----------
    columns:
        Names of the columns about to enter the feature matrix -- either the
        feature names or the base fields they are derived from.

    Raises
    ------
    ValueError
        If a forbidden field is present. Failing loudly beats silently shipping
        a model that memorised link names.

    Notes
    -----
    This guards the *feature matrix*, not the raw records. Raw rows legitimately
    carry ``src_switch``/``dst_switch``/``direction`` because they are needed to
    group rows per link; what must never happen is one of those becoming a
    feature column.
    """
    present = [c for c in columns if c in FORBIDDEN_FIELDS]
    if present:
        raise ValueError(
            "identity/target fields must not reach the feature matrix: "
            f"{sorted(present)}. The model must learn from signal, not from names."
        )


def _rolling_slope(values: Sequence[float], dt: float) -> float:
    """Least-squares slope of ``values`` against time, in units per second.

    Parameters
    ----------
    values:
        Oldest-to-newest samples.
    dt:
        Nominal spacing between samples, in seconds.

    Returns
    -------
    float
        Slope, or 0.0 when the window is too short or ``dt`` is invalid.
    """
    n = len(values)
    if n < 2 or dt <= 0:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return (num / den) / dt


def _rolling_std(values: Sequence[float]) -> float:
    """Population standard deviation of the window."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(var)


@dataclass
class WindowFeatureRow:
    """One training example: feature vector plus its provenance."""

    #: (src_switch, dst_switch, direction) identifying the link.
    link_key: Tuple[str, str, str]
    #: Unix timestamp of the window's newest sample (the "now" of this example).
    timestamp: float
    #: Feature values, ordered per :func:`feature_names`.
    values: List[float]
    #: The training label. ``-1`` marks a row excluded from training because
    #: the label horizon was not fully observed (not a negative example).
    label: int
    #: True when the full horizon was available.
    horizon_valid: bool
    #: Collection run this window came from, so leave-one-run/pattern-out
    #: validation can group rows without re-deriving provenance from timestamps.
    run_id: str = ""
    #: Traffic pattern of the originating run, when the collector recorded one.
    pattern: str = ""


class FeatureBuilder:
    """Builds sliding-window features from per-link telemetry rows.

    Parameters
    ----------
    window_w:
        Number of trailing samples per feature window (``ml.window_w``).
    sample_interval_s:
        Nominal sample spacing, used for the slope unit (``ml.sample_interval_s``).
    horizon_n_s:
        Label horizon in seconds (``ml.horizon_n_s``).
    label_gap_s:
        Dead band between the window end and the horizon start
        (``ml.label_gap_s``). This is the anti-leakage guard: the sample at
        ``t`` itself may not contribute to the label.
    fields:
        Allowlisted base fields.
    """

    def __init__(
        self,
        window_w: int = 12,
        sample_interval_s: float = 2.0,
        horizon_n_s: float = 10.0,
        label_gap_s: float = 2.0,
        fields: Sequence[str] = BASE_FIELDS,
    ) -> None:
        if window_w < 2:
            raise ValueError(
                f"window_w must be >= 2 to compute a slope, got {window_w}"
            )
        if label_gap_s <= 0:
            raise ValueError(
                "label_gap_s must be > 0; without a dead band the sample at t "
                "could leak into its own label"
            )
        if horizon_n_s <= label_gap_s:
            raise ValueError(
                f"horizon_n_s ({horizon_n_s}) must exceed label_gap_s "
                f"({label_gap_s})"
            )
        self.window_w = int(window_w)
        self.sample_interval_s = float(sample_interval_s)
        self.horizon_n_s = float(horizon_n_s)
        self.label_gap_s = float(label_gap_s)
        self.fields = tuple(fields)
        # Structural guarantee: the allowlist itself must be clean. This is
        # checked once at construction so a bad config fails immediately rather
        # than producing a subtly wrong matrix.
        assert_no_identity_features(self.fields)
        self.feature_names = feature_names(self.fields)
        assert_no_identity_features(self.feature_names)

    # ------------------------------------------------------------- grouping --
    @staticmethod
    def link_key(row: Dict[str, Any]) -> Tuple[str, str, str]:
        """Identify the link a row belongs to.

        Direction is kept because utilisation is directional: the s1->s2 link
        and s2->s1 are distinct measurements.
        """
        return (
            str(row.get("src_switch", "")),
            str(row.get("dst_switch", "")),
            str(row.get("direction", "forward")),
        )

    @classmethod
    def series_key(cls, row: Dict[str, Any]) -> Tuple[str, ...]:
        """Identify the *series* a row belongs to: link plus run.

        The dataset contains many runs of the same link, each with its own load
        shape and separated in time by a teardown/setup gap. Grouping by link
        alone would concatenate them into one series, so the first windows of a
        run would be built from the idle tail of the previous run -- windows that
        describe a transition that never happened. ``run_id`` therefore
        partitions the series whenever the collector supplies it.
        """
        return cls.link_key(row) + (str(row.get("run_id", "") or ""),)

    def group_by_link(
        self, rows: Iterable[Dict[str, Any]]
    ) -> Dict[Tuple[str, ...], List[Dict[str, Any]]]:
        """Sort rows into per-link, per-run, time-ordered buckets.

        Raises
        ------
        ValueError
            If two rows in one series share a timestamp, which would make the
            window ambiguous.
        """
        buckets: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
        for row in rows:
            buckets.setdefault(self.series_key(row), []).append(row)
        for key, items in buckets.items():
            items.sort(key=lambda r: float(r["timestamp"]))
            stamps = [float(r["timestamp"]) for r in items]
            if len(set(stamps)) != len(stamps):
                dupes = len(stamps) - len(set(stamps))
                raise ValueError(
                    f"series {key} has {dupes} duplicate timestamp(s); "
                    "the sliding window would be ambiguous"
                )
        return buckets

    # ------------------------------------------------------------- labelling --
    def label_for(
        self,
        timestamps: Sequence[float],
        congested: Sequence[bool],
        index: int,
    ) -> Tuple[int, bool]:
        """Label the example whose window ends at ``timestamps[index]``.

        Parameters
        ----------
        timestamps, congested:
            Time-ordered samples and their congestion flags for one link.
        index:
            Position of the window's final sample.

        Returns
        -------
        (label, horizon_valid)
            ``label`` is 1 only if a sample **actually flagged congested** falls
            inside the horizon ``[t + gap, t + gap + horizon]``, else 0.
            ``horizon_valid`` is False when the data ends before the horizon
            completes, in which case the label is ``-1`` and the row must not be
            trained on.

        Notes
        -----
        An earlier version of this function returned 1 as soon as *any* sample
        existed inside the horizon, which labelled the entire series positive
        and made the training matrix degenerate. The congestion flag must be
        consulted.
        """
        t_now = timestamps[index]
        horizon_start = t_now + self.label_gap_s
        horizon_end = horizon_start + self.horizon_n_s

        # The horizon must be fully observed, otherwise "not congested" is
        # indistinguishable from "we stopped looking".
        if timestamps[-1] < horizon_end:
            return -1, False

        in_horizon = [
            bool(flag)
            for stamp, flag in zip(timestamps, congested)
            if horizon_start <= stamp <= horizon_end
        ]
        if not in_horizon:
            # No sample landed in the horizon window at all (a sampling gap).
            return -1, False
        return (1 if any(in_horizon) else 0), True

    # -------------------------------------------------------------- building --
    def build(self, rows: Iterable[Dict[str, Any]]) -> List[WindowFeatureRow]:
        """Produce every valid feature row from raw telemetry.

        Parameters
        ----------
        rows:
            Raw telemetry records with ``timestamp`` and the allowlisted
            fields, plus ``is_congested`` for labelling.

        Returns
        -------
        list[WindowFeatureRow]
            One entry per (link, time) with a complete window. Rows whose
            window is short are skipped rather than padded.
        """
        buckets = self.group_by_link(rows)
        out: List[WindowFeatureRow] = []

        for key, items in buckets.items():
            timestamps = [float(r["timestamp"]) for r in items]
            congested = [bool(r.get("is_congested", False)) for r in items]

            for index in range(self.window_w - 1, len(items)):
                window = items[index - self.window_w + 1: index + 1]
                values: List[float] = []
                usable = True
                for field in self.fields:
                    series: List[float] = []
                    for row in window:
                        raw = row.get(field)
                        if raw is None or raw == "":
                            usable = False
                            break
                        try:
                            series.append(float(raw))
                        except (TypeError, ValueError):
                            usable = False
                            break
                    if not usable:
                        break
                    values.append(series[-1])
                    values.append(sum(series) / len(series))
                    values.append(_rolling_slope(series, self.sample_interval_s))
                    values.append(_rolling_std(series))
                if not usable:
                    # Incomplete window: drop it rather than invent a value.
                    continue

                label, valid = self.label_for(timestamps, congested, index)
                run_id = str(key[3]) if len(key) > 3 else ""
                pattern = str(items[index].get("pattern", "") or "")
                if not valid:
                    # Horizon not fully observed -- excluded from training but
                    # still reported so callers can quantify the loss.
                    out.append(
                        WindowFeatureRow(key[:3], timestamps[index], values, -1,
                                         False, run_id, pattern)
                    )
                    continue
                out.append(
                    WindowFeatureRow(key[:3], timestamps[index], values, label, True,
                                     run_id, pattern)
                )

        LOGGER.info(
            "built %d feature rows across %d series (link x run)", len(out), len(buckets)
        )
        return out

    # ----------------------------------------------------------------- matrix --
    def to_matrix(
        self, rows: Sequence[WindowFeatureRow], *, include_invalid: bool = False
    ) -> Tuple[List[List[float]], List[int], List[Tuple[str, str, str]]]:
        """Flatten rows into ``(X, y, link_keys)``.

        Parameters
        ----------
        include_invalid:
            When False (default) rows with ``label == -1`` are excluded, which
            is what training must do.
        """
        xs: List[List[float]] = []
        ys: List[int] = []
        keys: List[Tuple[str, str, str]] = []
        for row in rows:
            if not include_invalid and not row.horizon_valid:
                continue
            xs.append(list(row.values))
            ys.append(row.label)
            keys.append(row.link_key)
        return xs, ys, keys

    def describe(self) -> str:
        """Human-readable summary of the configured feature space."""
        return (
            f"FeatureBuilder(window_w={self.window_w}, "
            f"sample_interval_s={self.sample_interval_s}s, "
            f"label_gap_s={self.label_gap_s}s, "
            f"horizon_n_s={self.horizon_n_s}s, "
            f"columns={len(self.feature_names)} over {len(self.fields)} fields)"
        )