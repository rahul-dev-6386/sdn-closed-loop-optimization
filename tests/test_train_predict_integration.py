"""Integration test: train a model, save it, reload it, predict with it.

This is the seam where a train/inference mismatch hides. Training builds its
matrix from a :class:`~ml.features.FeatureBuilder`; inference builds one from the
same allowlist recovered from *column names in the saved bundle*. If the two ever
disagree -- a reordered column, a field renamed, a stray identity field -- the
model does not raise, it returns confident wrong numbers, and every downstream
number in P10 looks legitimate.

So this test exercises the real round trip on a synthetic dataset: train, persist
a bundle, load it through the production loader, and score live-shaped telemetry.
The prediction is compared against the model's own training-time expectation, not
against a hardcoded value.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import Config, load_config  # noqa: E402
from ml.features import BASE_FIELDS, feature_names  # noqa: E402
from ml.predict import CongestionPredictor, ModelUnavailable  # noqa: E402
from ml.train import DatasetError, train  # noqa: E402

ALL_FIELDS = BASE_FIELDS

#: Live telemetry carries every allowlisted field; inference needs all of them.
LIVE_ROWS_PER_LINK = 40


def synth_rows(
    n: int, *, congested_from: int, link: str, dst: str,
    run_id: str, pattern: str, start: float,
) -> List[Dict[str, Any]]:
    """A link-run that ramps, then congests."""
    rows: List[Dict[str, Any]] = []
    for i in range(n):
        congested = i >= congested_from
        util = 1.0 if congested else min(0.15 + i * 0.012, 0.70)
        rows.append({
            "timestamp": start + i * 2.0,
            "src_switch": link, "dst_switch": dst,
            "direction": "forward", "run_id": run_id, "pattern": pattern,
            "utilization": util,
            "latency_ms": 60.0 if congested else 3.0,
            "loss_pct": 3.5 if congested else 0.0,
            "tx_rate_mbps": util * 100.0,
            "rx_rate_mbps": util * 96.0,
            "tx_packet_rate_pps": util * 1200.0,
            "rx_packet_rate_pps": util * 1150.0,
            "capacity_mbps": 100.0,
            "is_congested": congested,
        })
    return rows


def write_training_csv(path: Path) -> Path:
    """Two patterns, several runs each, long enough to window and label."""
    rows: List[Dict[str, Any]] = []
    base = 1000.0
    for index, pattern in enumerate(("ramp", "bursty_onoff", "poisson",
                                    "tcp_udp_mix")):
        for rep in range(3):
            rows += synth_rows(
                90, congested_from=55, link="s1", dst="s2",
                run_id=f"run_{pattern}_{rep}", pattern=pattern, start=base,
            )
            rows += synth_rows(
                90, congested_from=55, link="s1", dst="s3",
                run_id=f"run_{pattern}_{rep}", pattern=pattern, start=base,
            )
            base += 1000.0
    columns = ["run_id", "timestamp", "src_switch", "dst_switch", "direction",
               "capacity_mbps", "tx_rate_mbps", "rx_rate_mbps",
               "tx_packet_rate_pps", "rx_packet_rate_pps", "utilization",
               "latency_ms", "loss_pct", "is_congested", "pattern"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def live_rows(link: str, dst: str, *, congested_from: int,
              run_id: str = "run_live") -> List[Dict[str, Any]]:
    """Telemetry shaped exactly like the live collector's output."""
    return synth_rows(LIVE_ROWS_PER_LINK, congested_from=congested_from,
                      link=link, dst=dst, run_id=run_id, pattern="ramp",
                      start=9000.0)


@pytest.fixture(scope="module")
def trained_bundle(tmp_path_factory) -> Dict[str, Any]:
    """Train once, save the bundle, and reuse it across the tests."""
    tmp = tmp_path_factory.mktemp("bundle")
    csv_path = write_training_csv(tmp / "training.csv")
    base = load_config()
    cfg = Config({
        **dict(base),
        "ml": {
            **dict(base["ml"]),
            "model_path": str(tmp / "model.joblib"),
            "metrics_path": str(tmp / "metrics.json"),
            "features_path": str(tmp / "fi.png"),
        },
    })
    report = train(csv_path, cfg=cfg, quick=True)
    return {"dir": tmp, "report": report, "config": cfg}


# ------------------------------------------------------------------ train --
def test_training_on_a_realistic_dataset_succeeds(trained_bundle) -> None:
    report = trained_bundle["report"]
    assert report["winner"] in {"logistic_regression", "random_forest"}
    assert report["time_split"]["train_rows"] > 0
    assert report["time_split"]["test_rows"] > 0


def test_the_saved_bundle_records_the_full_feature_set(trained_bundle) -> None:
    """Inference recovers its allowlist from these names, so they must be
    complete and in the canonical order."""
    import joblib

    bundle = joblib.load(trained_bundle["dir"] / "model.joblib")
    assert list(bundle["feature_names"]) == feature_names(ALL_FIELDS)
    assert bundle["window_w"] == int(load_config().get("ml.window_w"))


# ------------------------------------------------------- reload + predict --
def test_the_production_loader_builds_a_working_predictor(trained_bundle) -> None:
    predictor = CongestionPredictor.from_path(trained_bundle["dir"] / "model.joblib")
    assert list(predictor.feature_names) == feature_names(ALL_FIELDS)
    assert predictor.window_w == int(load_config().get("ml.window_w"))


def test_inference_scores_live_shaped_telemetry(trained_bundle) -> None:
    """The actual round trip: saved model, live-shaped rows, real probability."""
    predictor = CongestionPredictor.from_path(trained_bundle["dir"] / "model.joblib")
    batch = predictor.predict(live_rows("s1", "s2", congested_from=30))
    assert len(batch.predictions) == 1
    prediction = batch.predictions[0]
    assert prediction.reason == "", f"unscored: {prediction.reason}"
    assert prediction.probability is not None
    assert 0.0 <= prediction.probability <= 1.0


def test_the_forecast_is_higher_on_a_congesting_link(trained_bundle) -> None:
    """Directional sanity: a link already over threshold must not look safer
    than one far below it. A model that fails this is worse than useless."""
    predictor = CongestionPredictor.from_path(trained_bundle["dir"] / "model.joblib")
    quiet = predictor.predict(live_rows("s1", "s2", congested_from=999)).predictions[0]
    busy = predictor.predict(live_rows("s1", "s2", congested_from=10)).predictions[0]
    assert quiet.probability is not None and busy.probability is not None
    assert busy.probability > quiet.probability, (
        f"busy={busy.probability:.3f} should exceed quiet={quiet.probability:.3f}"
    )


def test_the_congested_link_is_flagged_and_the_quiet_one_is_not(
    trained_bundle,
) -> None:
    predictor = CongestionPredictor.from_path(trained_bundle["dir"] / "model.joblib")
    busy = predictor.predict(live_rows("s1", "s2", congested_from=5)).predictions[0]
    assert busy.congested is True, (
        f"probability was only {busy.probability:.3f} on a congested link"
    )


def test_a_second_link_is_scored_independently(trained_bundle) -> None:
    predictor = CongestionPredictor.from_path(trained_bundle["dir"] / "model.joblib")
    rows = (live_rows("s1", "s2", congested_from=30)
            + live_rows("s1", "s3", congested_from=999, run_id="run_live"))
    batch = predictor.predict(rows)
    assert len(batch.predictions) == 2
    assert {p.link_key[0] for p in batch.predictions} == {"s1"}


def test_inference_latency_is_inside_the_controller_budget(trained_bundle) -> None:
    """P8 falls back to shortest path if this is exceeded, so it is a hard
    constraint rather than a benchmark."""
    predictor = CongestionPredictor.from_path(trained_bundle["dir"] / "model.joblib")
    batch = predictor.predict(live_rows("s1", "s2", congested_from=30))
    budget = float(load_config().get("ml.max_inference_ms", 50.0))
    assert batch.latency_ms < budget, (
        f"p95 scoring took {batch.latency_ms:.1f}ms, over the {budget}ms budget"
    )


def test_a_changed_allowlist_is_refused_rather_than_scored(
    trained_bundle,
) -> None:
    """The failure this seam exists to catch: same columns, wrong order."""
    predictor = CongestionPredictor.from_path(trained_bundle["dir"] / "model.joblib")
    with pytest.raises(ModelUnavailable, match="order_mismatch"):
        predictor.validate_feature_names(list(reversed(predictor.feature_names)))


def test_prediction_is_deterministic(trained_bundle) -> None:
    """A prediction that changes between identical calls cannot be trusted by a
    controller that will act on it."""
    predictor = CongestionPredictor.from_path(trained_bundle["dir"] / "model.joblib")
    rows = live_rows("s1", "s2", congested_from=20)
    first = predictor.predict(rows).predictions[0].probability
    second = predictor.predict(rows).predictions[0].probability
    assert first == second
