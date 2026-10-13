"""Tests for P8 online inference.

A trained model is trivially easy to score correctly in a test. The value here is
in the ways an *online* predictor lies: it confidently answers for a link whose
telemetry died, for a link that just booted, and for a feature set that drifted
from the one it was trained on. Each of those is a test below.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pytest

from ml.features import feature_names
from ml.predict import (
    CongestionPredictor,
    ModelUnavailable,
    Prediction,
    PredictionBatch,
    link_key_of,
)

FIELDS = ("utilization", "latency_ms")
DT = 2.0
WINDOW = 6


# --------------------------------------------------------------------------- #
# A stand-in for the trained model, so no real dataset is needed.
# --------------------------------------------------------------------------- #
class ConstantModel:
    """Returns a fixed probability for every row."""

    def __init__(self, value: float = 0.9) -> None:
        self.value = value
        self.calls = 0

    def predict_proba(self, matrix: Any) -> np.ndarray:
        self.calls += 1
        n = len(matrix)
        return np.tile([1.0 - self.value, self.value], (n, 1))


class SlidingModel:
    """Probabilistic in the first feature, so ordering can be observed."""

    def predict_proba(self, matrix: Any) -> np.ndarray:
        x = np.asarray(matrix, dtype=float)
        p = np.clip(x[:, 0], 0.0, 1.0)
        return np.column_stack([1.0 - p, p])


class SlowModel:
    """Takes longer than the latency budget allows."""

    def __init__(self, delay_s: float = 0.05) -> None:
        self.delay_s = delay_s

    def predict_proba(self, matrix: Any) -> np.ndarray:
        import time

        time.sleep(self.delay_s)
        n = len(matrix)
        return np.tile([0.5, 0.5], (n, 1))


def make_rows(
    n: int, *, link: str = "s1", dst: str = "s2", util: float = 0.5,
    start: float = 1000.0, run_id: str = "run_A",
) -> List[Dict[str, Any]]:
    """``n`` samples for one link at constant utilisation."""
    return [{
        "timestamp": start + i * DT,
        "src_switch": link, "dst_switch": dst, "direction": "forward",
        "run_id": run_id, "pattern": "ramp",
        "utilization": util, "latency_ms": 4.0, "loss_pct": 0.0,
        "tx_rate_mbps": util * 100.0, "rx_rate_mbps": util * 95.0,
        "tx_packet_rate_pps": util * 1200.0, "rx_packet_rate_pps": util * 1100.0,
        "is_congested": False,
    } for i in range(n)]


def make_predictor(model: Any = None, **kwargs: Any) -> CongestionPredictor:
    """A predictor over the two-field feature set used by these tests."""
    defaults: Dict[str, Any] = dict(
        decision_threshold=0.5, window_w=WINDOW, sample_interval_s=DT,
        horizon_n_s=10.0, max_inference_ms=50.0,
    )
    defaults.update(kwargs)
    return CongestionPredictor(
        model=model or ConstantModel(),
        feature_names=feature_names(FIELDS), **defaults,
    )


# ------------------------------------------------------------------ basics --
def test_link_key_extraction() -> None:
    assert link_key_of(make_rows(1)[0]) == ("s1", "s2", "forward")


def test_a_model_without_predict_proba_is_refused() -> None:
    class Useless:
        pass

    with pytest.raises(ModelUnavailable, match="predict_proba"):
        CongestionPredictor(Useless(), feature_names(FIELDS))


def test_batch_of_empty_rows_is_empty_not_an_error() -> None:
    batch = make_predictor().predict([])
    assert batch.predictions == []
    assert batch.congested_links() == []
    assert batch.within_budget is True


# ----------------------------------------------------------------- scoring --
def test_a_healthy_link_is_scored() -> None:
    batch = make_predictor(ConstantModel(0.9)).predict(make_rows(WINDOW + 4))
    assert len(batch.predictions) == 1
    pred = batch.predictions[0]
    assert pred.probability == pytest.approx(0.9)
    assert pred.congested is True
    assert pred.reason == ""
    assert pred.n_samples == WINDOW + 4


def test_probability_drives_the_congested_flag() -> None:
    low = make_predictor(ConstantModel(0.2)).predict(make_rows(WINDOW + 2))
    assert low.predictions[0].probability == pytest.approx(0.2)
    assert low.predictions[0].congested is False

    high = make_predictor(ConstantModel(0.8)).predict(make_rows(WINDOW + 2))
    assert high.predictions[0].congested is True


def test_threshold_is_inclusive() -> None:
    """probability == threshold counts as congested."""
    batch = make_predictor(ConstantModel(0.5)).predict(make_rows(WINDOW + 2))
    assert batch.predictions[0].congested is True


def test_multiple_links_are_scored_independently() -> None:
    rows = (make_rows(WINDOW + 3, link="s1")
            + make_rows(WINDOW + 3, link="s3"))
    batch = make_predictor().predict(rows)
    assert {p.link_key[0] for p in batch.predictions} == {"s1", "s3"}
    assert len(batch.by_link()) == 2


def test_congested_links_are_sorted_for_reproducibility() -> None:
    rows = (make_rows(WINDOW + 2, link="s3")
            + make_rows(WINDOW + 2, link="s1")
            + make_rows(WINDOW + 2, link="s2"))
    batch = make_predictor(ConstantModel(0.9)).predict(rows)
    assert [k[0] for k in batch.congested_links()] == ["s1", "s2", "s3"]


# ------------------------------------------------------------- staleness --
def test_a_stale_link_is_unscored_not_guessed() -> None:
    """A dead link must not inherit a confident number from the model.

    This is the failure that would cause a reroute away from a link nobody is
    measuring -- the controller would act on a fiction.
    """
    # s1 reports up to t=1058; s2's last sample is t=1018, i.e. 40 s stale,
    # against a deadline of 3 cadences = 6 s.
    fresh = make_rows(WINDOW + 4, link="s1", start=1040.0)
    dead = make_rows(WINDOW + 4, link="s3", start=1000.0)
    batch = make_predictor(ConstantModel(0.9)).predict(fresh + dead)
    by_link = batch.by_link()
    assert by_link[("s1", "s2", "forward")].probability == pytest.approx(0.9)
    assert by_link[("s3", "s2", "forward")].probability is None
    assert by_link[("s3", "s2", "forward")].reason == "stale"
    assert by_link[("s3", "s2", "forward")].congested is False


def test_stale_links_are_excluded_from_congested() -> None:
    rows = (make_rows(WINDOW + 4, link="s1", start=1040.0)
            + make_rows(WINDOW + 4, link="s3", start=1000.0))
    batch = make_predictor(ConstantModel(0.95)).predict(rows)
    assert batch.congested_links() == [("s1", "s2", "forward")]
    assert len(batch.unscored()) == 1


def test_staleness_deadline_is_three_cadences() -> None:
    """Just inside the deadline scores; just outside is stale."""
    rows = make_rows(WINDOW + 2, start=1000.0)
    newest = rows[-1]["timestamp"]
    # 6 s old == exactly the deadline -> still fresh.
    inside = make_predictor().predict(rows, now=newest + 6.0)
    assert inside.predictions[0].probability is not None
    # Just past it.
    outside = make_predictor().predict(rows, now=newest + 6.5)
    assert outside.predictions[0].reason == "stale"


# ------------------------------------------------------ short history --
def test_a_link_with_insufficient_history_is_unscored() -> None:
    """A partially-filled window is a distribution the model never saw."""
    batch = make_predictor().predict(make_rows(WINDOW - 1))
    assert batch.predictions[0].probability is None
    assert batch.predictions[0].reason == "insufficient_history"
    assert batch.predictions[0].n_samples == WINDOW - 1


def test_exactly_window_w_samples_is_enough() -> None:
    batch = make_predictor().predict(make_rows(WINDOW))
    assert batch.predictions[0].probability is not None


def test_a_single_sample_link_is_reported_not_dropped() -> None:
    """Every known link must appear in the batch, scorable or not."""
    # s2's lone sample is the newest overall, so it is fresh but far too short.
    # Both links end at t=1040: s1 has a full window, s3 has a single sample.
    rows = (make_rows(WINDOW + 2, link="s1", start=1026.0)
            + make_rows(1, link="s3", start=1040.0))
    batch = make_predictor().predict(rows)
    assert {p.link_key[0] for p in batch.predictions} == {"s1", "s3"}
    assert batch.by_link()[("s3", "s2", "forward")].reason == "insufficient_history"
    assert batch.by_link()[("s1", "s2", "forward")].probability is not None


# ------------------------------------------------------- feature drift --
def test_a_different_feature_set_is_refused() -> None:
    predictor = make_predictor()
    with pytest.raises(ModelUnavailable, match="does not match the trained model"):
        predictor.validate_feature_names(feature_names(("utilization",)))


def test_reordered_features_are_refused() -> None:
    """Same names, different order: a silently wrong score, so it must raise."""
    predictor = make_predictor()
    swapped = list(reversed(predictor.feature_names))
    with pytest.raises(ModelUnavailable, match="order_mismatch=True"):
        predictor.validate_feature_names(swapped)


def test_matching_features_validate_cleanly() -> None:
    predictor = make_predictor()
    predictor.validate_feature_names(predictor.feature_names)


def test_field_names_are_recovered_from_feature_names() -> None:
    predictor = make_predictor()
    assert predictor._field_names() == list(FIELDS)


# ----------------------------------------------------------- latency --
def test_a_slow_model_degrades_instead_of_blocking() -> None:
    """The control loop must never stall behind a slow model."""
    batch = make_predictor(SlowModel(0.05), max_inference_ms=1.0).predict(
        make_rows(WINDOW + 2)
    )
    assert batch.degraded is True
    assert batch.within_budget is False
    assert batch.latency_ms > 1.0
    assert batch.predictions[0].probability is None
    assert batch.predictions[0].reason == "degraded_inference"
    assert batch.congested_links() == []


def test_a_fast_model_is_within_budget() -> None:
    batch = make_predictor(max_inference_ms=50.0).predict(make_rows(WINDOW + 2))
    assert batch.degraded is False
    assert batch.within_budget is True
    assert batch.latency_ms < 50.0


def test_latency_is_measured_for_a_real_pass() -> None:
    model = ConstantModel()
    make_predictor(model).predict(make_rows(WINDOW + 2))
    assert model.calls == 1, "the model must actually be invoked"


# ------------------------------------------------------------ persistence --
def test_bundle_round_trip(tmp_path: Path) -> None:
    import joblib

    bundle = {
        "model": ConstantModel(0.75),
        "feature_names": feature_names(FIELDS),
        "decision_threshold": 0.5,
        "window_w": WINDOW,
        "sample_interval_s": DT,
        "horizon_n_s": 10.0,
    }
    path = tmp_path / "m.joblib"
    joblib.dump(bundle, path)

    predictor = CongestionPredictor.from_path(path, max_inference_ms=25.0)
    assert predictor.max_inference_ms == 25.0
    assert predictor.window_w == WINDOW
    batch = predictor.predict(make_rows(WINDOW + 2))
    assert batch.predictions[0].probability == pytest.approx(0.75)


def test_missing_bundle_raises_with_guidance(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="make train"):
        CongestionPredictor.from_path(tmp_path / "absent.joblib")


def test_bundle_without_feature_names_is_refused() -> None:
    with pytest.raises(ModelUnavailable, match="feature_names"):
        CongestionPredictor.from_bundle({"model": ConstantModel()})


def test_bundle_without_a_model_is_refused() -> None:
    with pytest.raises(ModelUnavailable, match="no 'model' key"):
        CongestionPredictor.from_bundle({"feature_names": ["a"]})


# ------------------------------------------------------------ reporting --
def test_as_dict_flattens_the_link_key() -> None:
    batch = make_predictor().predict(make_rows(WINDOW + 2))
    row = batch.as_dict()["predictions"][0]
    assert row["src_switch"] == "s1"
    assert row["dst_switch"] == "s2"
    assert row["direction"] == "forward"
    assert "probability" in row


def test_batch_as_dict_counts() -> None:
    rows = (make_rows(WINDOW + 2, link="s1")
            + make_rows(WINDOW + 2, link="s2"))
    batch = make_predictor(ConstantModel(0.9)).predict(rows)
    summary = batch.as_dict()
    assert summary["n_congested"] == 2
    assert summary["n_unscored"] == 0
    assert "latency_ms" in summary


def test_probability_none_is_distinct_from_zero() -> None:
    """"I do not know" must never be read as "definitely fine"."""
    batch = make_predictor(ConstantModel(0.0)).predict(make_rows(2))
    pred = batch.predictions[0]
    assert pred.probability is None
    assert pred.congested is False
    assert pred.reason == "insufficient_history"
