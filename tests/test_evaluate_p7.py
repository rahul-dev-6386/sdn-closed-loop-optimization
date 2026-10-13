"""Tests for P7 metrics.

Hand-built arrays with known answers, so a regression in the metric maths is
caught without needing a dataset. Several cases check the implementation
against scikit-learn, which is the reference the project reports alongside it.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from sklearn.metrics import average_precision_score, roc_auc_score

from ml.evaluate import (
    binary_metrics,
    lead_time_stats,
    measure_inference_latency,
    normalised_importance,
)


# ---------------------------------------------------------------- accuracy --
def test_binary_metrics_perfect_prediction() -> None:
    m = binary_metrics([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9])
    assert m.pr_auc == pytest.approx(1.0)
    assert m.roc_auc == pytest.approx(1.0)
    assert m.precision == 1.0 and m.recall == 1.0 and m.f1 == 1.0


def test_pr_auc_ranks_while_thresholded_scores_do_not() -> None:
    """PR-AUC scores *ranking*; precision/recall score a *decision*.

    Here the single positive is ranked last-but-one, so ranking quality is
    perfect (PR-AUC 1.0) even though the 0.5 threshold predicts nothing and
    recall is 0. This is precisely why PR-AUC alone is not a sufficient report
    and must be read together with precision/recall/F1 at the real threshold.
    """
    m = binary_metrics([0, 0, 0, 1], [0.1, 0.2, 0.3, 0.4], threshold=0.5)
    assert m.recall == 0.0
    assert m.precision == 0.0
    assert m.f1 == 0.0
    assert m.pr_auc == 1.0


def test_single_class_slice_is_nan_not_a_crash() -> None:
    """A test slice with one class must not raise or silently score 1.0."""
    m = binary_metrics([0, 0, 0], [0.1, 0.2, 0.3])
    assert math.isnan(m.pr_auc)
    assert math.isnan(m.roc_auc)
    assert m.n_positive == 0


def test_empty_input_is_handled() -> None:
    m = binary_metrics([], [])
    assert m.n_samples == 0
    assert m.positive_rate == 0.0
    assert m.accuracy == 0.0


def test_mismatched_lengths_raise() -> None:
    with pytest.raises(ValueError, match="must align"):
        binary_metrics([0, 1], [0.5])


def test_raising_the_threshold_never_increases_recall() -> None:
    """The invariant that actually holds for any threshold sweep."""
    y = [0, 1, 1, 0]
    p = [0.4, 0.45, 0.55, 0.1]
    recalls = [binary_metrics(y, p, threshold=t).recall
               for t in (0.2, 0.4, 0.5, 0.9, 1.0)]
    assert all(a >= b for a, b in zip(recalls, recalls[1:])), recalls


def test_threshold_moves_precision_and_f1() -> None:
    """A stricter threshold trades recall for precision on separable data."""
    y = [0, 1, 1, 0]
    p = [0.2, 0.8, 0.9, 0.1]
    loose = binary_metrics(y, p, threshold=0.3)
    tight = binary_metrics(y, p, threshold=0.85)
    # 0.3 -> both positives caught; 0.85 -> only the 0.9 one.
    assert loose.recall == 1.0
    assert tight.recall == pytest.approx(0.5)
    # Both are perfectly precise here, so precision must not degrade.
    assert tight.precision == loose.precision == 1.0
    assert loose.f1 > tight.f1


# ----------------------------------------------------- cross-check sklearn --
def test_pr_auc_matches_sklearn() -> None:
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, size=200)
    p = np.clip(y * 0.4 + rng.random(200) * 0.6, 0, 1)
    if y.sum() in (0, len(y)):
        pytest.skip("degenerate draw")
    mine = binary_metrics(y, p)
    assert mine.pr_auc == pytest.approx(average_precision_score(y, p), abs=1e-6)


def test_roc_auc_matches_sklearn() -> None:
    rng = np.random.default_rng(1)
    y = rng.integers(0, 2, size=300)
    p = np.clip(y * 0.3 + rng.random(300) * 0.7, 0, 1)
    if y.sum() in (0, len(y)):
        pytest.skip("degenerate draw")
    mine = binary_metrics(y, p)
    assert mine.roc_auc == pytest.approx(roc_auc_score(y, p), abs=1e-6)


# --------------------------------------------------------------- lead time --
def test_lead_time_counts_each_episode() -> None:
    ts = [0.0, 10.0, 20.0, 30.0, 40.0]
    y = [0, 0, 1, 1, 0]
    p = [0.0, 0.9, 0.9, 0.0, 0.0]
    lead = lead_time_stats(ts, y, p, threshold=0.5)
    assert lead.episodes == 1
    assert lead.detected == 1
    assert lead.per_episode == [10.0]  # alarmed at t=10, congested at t=20
    assert lead.detection_rate == 1.0


def test_lead_time_undetected_episode() -> None:
    ts = [0.0, 10.0, 20.0, 30.0]
    y = [0, 1, 1, 0]
    p = [0.0, 0.0, 0.0, 0.0]
    lead = lead_time_stats(ts, y, p, threshold=0.5)
    assert lead.episodes == 1
    assert lead.detected == 0
    assert lead.per_episode == []
    assert lead.detection_rate == 0.0


def test_lead_time_multiple_episodes() -> None:
    """Each episode is measured against the latest alarm before it began."""
    ts = [0.0, 5.0, 10.0, 20.0, 25.0, 30.0]
    y = [0, 1, 0, 0, 1, 0]
    # One alarm at t=0, ahead of both episodes.
    p = [0.9, 0.0, 0.0, 0.0, 0.0, 0.0]
    lead = lead_time_stats(ts, y, p, threshold=0.5)
    assert lead.episodes == 2
    assert lead.detected == 2
    assert lead.per_episode == [5.0, 25.0]
    assert lead.mean_s == pytest.approx(15.0)


def test_lead_time_no_episodes() -> None:
    lead = lead_time_stats([0.0, 1.0], [0, 0], [0.1, 0.2])
    assert lead.episodes == 0 and lead.detected == 0


def test_lead_time_alarm_after_congestion_is_not_counted() -> None:
    """An alarm that only fires once congestion is underway is not a lead."""
    ts = [0.0, 10.0, 20.0]
    y = [0, 1, 1]
    p = [0.0, 0.0, 0.99]
    lead = lead_time_stats(ts, y, p, threshold=0.5)
    assert lead.detected == 0


# --------------------------------------------------------------- latency --
def test_latency_measured_for_a_real_callable() -> None:
    matrix = [[0.1, 0.2], [0.3, 0.4]]
    stats = measure_inference_latency(lambda m: [[0.5] for _ in m], matrix, repeats=5)
    assert stats.samples == 5
    assert stats.per_link_rows == 2
    assert stats.mean_ms >= 0.0


def test_latency_empty_matrix() -> None:
    stats = measure_inference_latency(lambda m: [], [], repeats=3)
    assert stats.samples == 0 and stats.per_link_rows == 0


# ----------------------------------------------------------- importance --
def test_importance_is_sorted_and_percentage_normalised() -> None:
    rows = normalised_importance([0.1, 0.6, 0.3], ["a", "b", "c"])
    assert [r["feature"] for r in rows] == ["b", "c", "a"]
    assert sum(r["pct"] for r in rows) == pytest.approx(100.0, abs=0.01)


def test_importance_handles_all_zero() -> None:
    rows = normalised_importance([0.0, 0.0], ["a", "b"])
    assert all(r["pct"] == 0.0 for r in rows)


def test_importance_uses_absolute_value_for_ranking() -> None:
    rows = normalised_importance([-0.9, 0.4], ["a", "b"])
    assert rows[0]["feature"] == "a"