"""Metrics and diagnostics for the congestion predictor (P7).

Kept free of I/O so every function here is directly unit-testable with
hand-built arrays. :mod:`ml.train` handles loading, splitting and persistence;
this module answers "how good is it, and why".

Reported metrics
----------------
* **PR-AUC** (average precision) is the headline. The classes are imbalanced --
  most 2 s windows are not followed by congestion -- so ROC-AUC flatters a
  model that merely avoids false positives. PR-AUC does not.
* **Precision / recall / F1** at the configured decision threshold.
* **Lead time**: for each genuine congestion episode, how many seconds *before*
  the link actually crossed the threshold did the model's probability first
  exceed the alarm threshold. This is the number that makes the predictor
  useful for proactive rerouting; a model that only reacts after congestion
  scores a high F1 and a useless lead time.
* **Inference latency**: wall time to score every link once. P8 falls back to
  shortest-path if this exceeds ``ml.max_inference_ms``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

LOGGER = logging.getLogger("ml.evaluate")


@dataclass
class BinaryMetrics:
    """Standard binary-classification metrics for one evaluation slice."""

    n_samples: int
    n_positive: int
    positive_rate: float
    pr_auc: float
    roc_auc: float
    precision: float
    recall: float
    f1: float
    accuracy: float
    threshold: float

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        return asdict(self)


def binary_metrics(
    y_true: Sequence[int],
    y_prob: Sequence[float],
    threshold: float = 0.5,
) -> BinaryMetrics:
    """Compute PR-AUC, ROC-AUC, precision, recall, F1 and accuracy.

    Implemented with numpy only so behaviour is explicit and dependency-light.

    Notes
    -----
    Precision and recall are defined at zero predicted positives as 0.0 rather
    than NaN, so a degenerate slice cannot poison an aggregate.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    if y_true.shape != y_prob.shape:
        raise ValueError(
            f"y_true and y_prob must align: {y_true.shape} vs {y_prob.shape}"
        )
    n = int(y_true.size)
    positives = int(y_true.sum())
    rate = positives / n if n else 0.0

    if n == 0 or positives == 0 or positives == n:
        # PR-AUC and ROC-AUC are undefined for a single-class slice.
        pr_auc = roc_auc = float("nan")
    else:
        pr_auc = _average_precision(y_true, y_prob)
        roc_auc = _roc_auc(y_true, y_prob)

    y_hat = (y_prob >= threshold).astype(float)
    tp = float(((y_hat == 1) & (y_true == 1)).sum())
    fp = float(((y_hat == 1) & (y_true == 0)).sum())
    fn = float(((y_hat == 0) & (y_true == 1)).sum())

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    accuracy = float((y_hat == y_true).mean()) if n else 0.0

    return BinaryMetrics(
        n_samples=n, n_positive=positives, positive_rate=rate,
        pr_auc=pr_auc, roc_auc=roc_auc, precision=precision, recall=recall,
        f1=f1, accuracy=accuracy, threshold=threshold,
    )


def _average_precision(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    """Average precision via the step-wise sum (sklearn's definition).

    Equivalent to ``sklearn.metrics.average_precision_score`` but implemented
    directly so the metric's behaviour is explicit and auditable.
    """
    order = np.argsort(-y_prob)
    y_sorted = y_true[order]
    tp = np.cumsum(y_sorted)
    fp = np.cumsum(1.0 - y_sorted)
    precision = tp / np.maximum(tp + fp, 1e-12)
    total_pos = y_sorted.sum()
    if total_pos == 0:
        return float("nan")
    return float((precision * y_sorted).sum() / total_pos)


def _roc_auc(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    """ROC-AUC via the rank-sum (Mann-Whitney U) identity."""
    order = np.argsort(y_prob)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(y_prob) + 1, dtype=float)
    n_pos = float(y_true.sum())
    n_neg = float(len(y_true) - n_pos)
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return float((ranks[y_true == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


# --------------------------------------------------------------------------- #
# Lead time
# --------------------------------------------------------------------------- #
@dataclass
class LeadTimeStats:
    """How far ahead of real congestion the model alarms."""

    episodes: int = 0
    detected: int = 0
    mean_s: float = 0.0
    median_s: float = 0.0
    min_s: float = 0.0
    max_s: float = 0.0
    per_episode: List[float] = field(default_factory=list)

    @property
    def detection_rate(self) -> float:
        """Fraction of episodes alarmed before they began."""
        return self.detected / self.episodes if self.episodes else 0.0

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view (episodes listed rounded)."""
        return {
            "episodes": self.episodes,
            "detected": self.detected,
            "detection_rate": round(self.detection_rate, 4),
            "mean_s": round(self.mean_s, 3),
            "median_s": round(self.median_s, 3),
            "min_s": round(self.min_s, 3),
            "max_s": round(self.max_s, 3),
            "per_episode_s": [round(v, 2) for v in self.per_episode],
        }


def lead_time_stats(
    timestamps: Sequence[float],
    y_true: Sequence[int],
    y_prob: Sequence[float],
    threshold: float = 0.5,
) -> LeadTimeStats:
    """Compute per-episode lead time.

    A *congestion episode* is a maximal run of ``y_true == 1``. For each episode
    starting at ``t_start``, lead time is ``t_start - t_alarm`` where ``t_alarm``
    is the earliest timestamp at or before ``t_start`` whose probability meets
    ``threshold``. Episodes with no earlier alarm count as undetected.

    Parameters
    ----------
    timestamps, y_true, y_prob:
        Chronologically ordered per-link series.
    threshold:
        Probability at which a link is considered alarmed.
    """
    timestamps = np.asarray(timestamps, dtype=float)
    y_true = np.asarray(y_true, dtype=int)
    y_prob = np.asarray(y_prob, dtype=float)

    stats = LeadTimeStats()
    n = len(timestamps)
    i = 0
    while i < n:
        if y_true[i] == 0:
            i += 1
            continue
        start = i
        while i < n and y_true[i] == 1:
            i += 1
        stats.episodes += 1
        t_start = float(timestamps[start])
        prior = np.arange(0, start)
        alarmed = prior[y_prob[prior] >= threshold]
        if alarmed.size:
            t_alarm = float(timestamps[int(alarmed[-1])])
            stats.per_episode.append(max(t_start - t_alarm, 0.0))
            stats.detected += 1

    if stats.per_episode:
        values = np.array(stats.per_episode, dtype=float)
        stats.mean_s = float(values.mean())
        stats.median_s = float(np.median(values))
        stats.min_s = float(values.min())
        stats.max_s = float(values.max())
    return stats


# --------------------------------------------------------------------------- #
# Inference latency
# --------------------------------------------------------------------------- #
@dataclass
class LatencyStats:
    """Wall-clock cost of scoring, in milliseconds."""

    mean_ms: float
    p95_ms: float
    max_ms: float
    samples: int
    per_link_rows: int

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        return {
            "mean_ms": round(self.mean_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "samples": self.samples,
            "per_link_rows": self.per_link_rows,
        }


def measure_inference_latency(
    predict_proba: Any,
    matrix: Sequence[Sequence[float]],
    repeats: int = 20,
) -> LatencyStats:
    """Time ``predict_proba`` over the whole matrix, ``repeats`` times.

    Mirrors P8's real workload -- one scoring pass for every link at once --
    so the reported figure is the number the controller must beat against
    ``ml.max_inference_ms``.
    """
    if len(matrix) == 0:
        return LatencyStats(0.0, 0.0, 0.0, 0, 0)
    timings: List[float] = []
    for _ in range(max(1, repeats)):
        start = time.perf_counter()
        predict_proba(matrix)
        timings.append((time.perf_counter() - start) * 1000.0)
    arr = np.array(timings, dtype=float)
    return LatencyStats(
        mean_ms=float(arr.mean()),
        p95_ms=float(np.percentile(arr, 95)),
        max_ms=float(arr.max()),
        samples=len(timings),
        per_link_rows=len(matrix),
    )


# --------------------------------------------------------------------------- #
# Feature importance
# --------------------------------------------------------------------------- #
def normalised_importance(
    importances: Sequence[float], names: Sequence[str]
) -> List[Dict[str, Any]]:
    """Return importances as a sorted, percentage-normalised list."""
    arr = np.asarray(importances, dtype=float)
    total = float(np.abs(arr).sum())
    rows = [
        {
            "feature": name,
            "importance": round(float(value), 6),
            "pct": round(float(abs(value) / total * 100.0), 3) if total else 0.0,
        }
        for name, value in zip(names, arr)
    ]
    rows.sort(key=lambda r: abs(r["importance"]), reverse=True)
    return rows


def summarise(
    name: str, metrics: BinaryMetrics, lead: Optional[LeadTimeStats] = None,
    latency: Optional[LatencyStats] = None,
) -> Dict[str, Any]:
    """Assemble one model's evaluation into a single JSON-friendly dict."""
    out: Dict[str, Any] = {"model": name, **metrics.as_dict()}
    if lead is not None:
        out["lead_time"] = lead.as_dict()
    if latency is not None:
        out["inference_latency"] = latency.as_dict()
    return out