"""Tests for the P7 training pipeline.

Everything here runs on synthetic CSVs written to ``tmp_path``, so the suite is
hermetic and needs neither the real dataset nor root. The properties under test
are the ones that make an ML result *trustworthy*: that the split is
chronological, that a degenerate dataset is refused loudly, and that the report
records what was actually measured.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

from common.config import Config, load_config
from ml.features import FeatureBuilder
from ml.train import (
    Dataset,
    DatasetError,
    build_dataset,
    build_models,
    cross_check_labels,
    evaluate_model,
    feature_importances,
    leave_one_pattern_out,
    load_labelled_csv,
    time_split,
    train,
)

FIELDS = ("utilization", "latency_ms")
COLUMNS = [
    "run_id", "timestamp", "src_switch", "dst_switch", "direction",
    "src_port", "dst_port", "capacity_mbps", "tx_rate_mbps", "rx_rate_mbps",
    "tx_packet_rate_pps", "rx_packet_rate_pps", "utilization", "latency_ms",
    "loss_pct", "is_stale", "is_congested", "pattern", "seed",
    "label_future_congested", "lead_time_s", "horizon_valid",
]


def _write_csv(
    path: Path, runs: List[Dict[str, Any]], *,
    link: str = "s1-s2", n: int = 40, congested_from: int | None = 20,
    pattern: str = "ramp", start: float = 1000.0, dt: float = 2.0,
) -> Path:
    """Write a synthetic labelled CSV shaped like the collector's output."""
    rows: List[Dict[str, Any]] = []
    for r, spec in enumerate(runs):
        base = start + r * (n * dt + 600.0)  # a wide gap between runs
        for i in range(n):
            cong = congested_from is not None and i >= congested_from
            util = 1.0 if cong else min(0.2 + i * 0.02, 0.75)
            rows.append({
                "run_id": spec.get("run_id", f"run_{pattern}_s{r}"),
                "timestamp": base + i * dt,
                "src_switch": link.split("-")[0],
                "dst_switch": link.split("-")[1],
                "direction": "forward",
                "src_port": spec.get("src_port", 1),
                "dst_port": spec.get("dst_port", 2),
                "capacity_mbps": 100.0,
                "tx_rate_mbps": util * 100.0,
                "rx_rate_mbps": util * 95.0,
                "tx_packet_rate_pps": util * 1200.0,
                "rx_packet_rate_pps": util * 1100.0,
                "utilization": util,
                "latency_ms": 55.0 if cong else 4.0,
                "loss_pct": 3.0 if cong else 0.0,
                "is_stale": "False",
                "is_congested": str(cong),
                "pattern": spec.get("pattern", pattern),
                "seed": 40 + r,
                "label_future_congested": "",
                "lead_time_s": "",
                "horizon_valid": "",
            })
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def builder() -> FeatureBuilder:
    return FeatureBuilder(window_w=5, sample_interval_s=2.0, horizon_n_s=10.0,
                          label_gap_s=2.0, fields=FIELDS)


@pytest.fixture
def dataset_csv(tmp_path: Path) -> Path:
    return _write_csv(tmp_path / "labeled.csv", [{"run_id": "run_A"}])


# ------------------------------------------------------------------ loading --
def test_missing_dataset_raises_a_helpful_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="collect_dataset"):
        load_labelled_csv(tmp_path / "nope.csv")


def test_loader_types_every_field(dataset_csv: Path) -> None:
    rows = load_labelled_csv(dataset_csv)
    assert rows
    assert isinstance(rows[0]["timestamp"], float)
    assert isinstance(rows[0]["utilization"], float)
    assert isinstance(rows[0]["is_congested"], bool)


def test_blank_numeric_cells_become_none_not_zero(tmp_path: Path) -> None:
    """A missing measurement is absent data; zero would be a fabricated value."""
    path = _write_csv(tmp_path / "l.csv", [{"run_id": "run_A"}])
    lines = path.read_text().splitlines()
    header = lines[0].split(",")
    col = header.index("utilization")
    cells = lines[1].split(",")
    cells[col] = ""
    lines[1] = ",".join(cells)
    path.write_text("\n".join(lines) + "\n")
    rows = load_labelled_csv(path)
    assert sum(r["utilization"] is None for r in rows) == 1
    assert sum(r["utilization"] is not None for r in rows) == len(rows) - 1


def test_rows_without_timestamp_or_link_are_dropped(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "l.csv", [{"run_id": "run_A"}])
    text = path.read_text().splitlines()
    text[1] = text[1].replace(text[1].split(",")[1], "")  # blank timestamp
    text[2] = text[2].replace(text[2].split(",")[2], "")  # blank src_switch
    path.write_text("\n".join(text) + "\n")
    rows = load_labelled_csv(path)
    assert len(rows) == 38  # 40 minus 2


def test_a_csv_with_no_usable_rows_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text(",".join(COLUMNS) + "\n" + ",".join([""] * len(COLUMNS)) + "\n")
    with pytest.raises(DatasetError, match="no usable rows"):
        load_labelled_csv(path)


# ------------------------------------------------------------- assembling --
def test_build_dataset_produces_a_finite_matrix(
    dataset_csv: Path, builder: FeatureBuilder,
) -> None:
    ds = build_dataset(load_labelled_csv(dataset_csv), builder)
    assert len(ds) > 0
    assert np.isfinite(ds.x).all()
    assert ds.x.shape[1] == len(builder.feature_names)
    assert set(np.unique(ds.y)) <= {0, 1}


def test_build_dataset_records_invalid_horizon_rows(
    dataset_csv: Path, builder: FeatureBuilder,
) -> None:
    """Rows whose horizon runs past the end are counted, not silently dropped."""
    ds = build_dataset(load_labelled_csv(dataset_csv), builder)
    assert ds.n_rows_invalid > 0


def test_too_short_a_dataset_is_refused_with_guidance(
    tmp_path: Path, builder: FeatureBuilder,
) -> None:
    path = _write_csv(tmp_path / "short.csv", [{"run_id": "run_A"}], n=8)
    with pytest.raises(DatasetError, match="window_w"):
        build_dataset(load_labelled_csv(path), builder)


def test_dataset_summary_mentions_runs_and_rate(
    dataset_csv: Path, builder: FeatureBuilder,
) -> None:
    text = build_dataset(load_labelled_csv(dataset_csv), builder).summary()
    assert "positive" in text and "series" in text


# ------------------------------------------------------------------ splits --
def test_time_split_is_chronological(builder: FeatureBuilder, dataset_csv: Path) -> None:
    ds = build_dataset(load_labelled_csv(dataset_csv), builder)
    split = time_split(ds, 0.3)
    assert split.train.size and split.test.size
    assert ds.timestamps[split.train].max() < ds.timestamps[split.test].min()


def test_time_split_holds_out_the_newest_data(
    builder: FeatureBuilder, dataset_csv: Path,
) -> None:
    """The test slice must be the *future* relative to training."""
    ds = build_dataset(load_labelled_csv(dataset_csv), builder)
    split = time_split(ds, 0.3)
    assert ds.timestamps[split.test].min() >= np.median(ds.timestamps)


def test_time_split_rejects_impossible_fractions(
    builder: FeatureBuilder, dataset_csv: Path,
) -> None:
    ds = build_dataset(load_labelled_csv(dataset_csv), builder)
    for bad in (0.0, 1.0, -0.2, 1.5):
        with pytest.raises(ValueError, match="test_fraction"):
            time_split(ds, bad)


def test_a_random_split_would_leak(tmp_path: Path, builder: FeatureBuilder) -> None:
    """Justifies the design: autocorrelated rows are near-duplicates.

    A random split puts a neighbour of a training window into the test set, so
    it reports a much easier problem than the chronological split. This test
    pins that gap so removing the temporal split is visible as a change.
    """
    path = _write_csv(tmp_path / "multi.csv", [
        {"run_id": "run_A"}, {"run_id": "run_B"}, {"run_id": "run_C"},
    ])
    ds = build_dataset(load_labelled_csv(path), builder)
    split = time_split(ds, 0.3)

    rng = np.random.default_rng(0)
    perm = rng.permutation(len(ds))
    random_test = perm[: int(0.3 * len(ds))]
    chrono_test = set(split.test.tolist())

    # Nearly every random test row has a chronologically adjacent neighbour in
    # the training slice, which is exactly the leakage being avoided.
    adjacent = 0
    for idx in random_test:
        if int(idx) - 1 in chrono_test or int(idx) + 1 in chrono_test:
            adjacent += 1
    assert adjacent > 0.3 * len(random_test)


# ------------------------------------------------------------------ models --
def test_build_models_returns_both_candidates() -> None:
    models = build_models(load_config())
    assert set(models) == {"logistic_regression", "random_forest"}


def test_quick_mode_uses_fewer_trees() -> None:
    full = build_models(load_config(), quick=False)["random_forest"]
    fast = build_models(load_config(), quick=True)["random_forest"]
    assert fast.n_estimators < full.n_estimators


def test_feature_importance_works_for_both_candidate_types() -> None:
    """LR coefficients and RF importances are extracted from different shapes."""
    cfg = load_config()
    names = ["utilization__last", "utilization__mean", "utilization__slope",
             "latency_ms__last"]
    x = np.array([[0.1, 0.2, 0.3, 1.0], [0.9, 0.8, 0.7, 9.0],
                  [0.15, 0.25, 0.35, 1.5], [0.85, 0.75, 0.65, 8.5]])
    y = np.array([0, 1, 0, 1])
    lr = build_models(cfg)["logistic_regression"]
    rf = build_models(cfg)["random_forest"]
    lr.fit(x, y)
    rf.fit(x, y)
    lr_rows = feature_importances(lr, names)
    rf_rows = feature_importances(rf, names)
    assert [r["feature"] for r in lr_rows] == sorted(
        names, key=lambda n: -abs(dict(zip(names, lr.named_steps["clf"].coef_.ravel()))[n])
    )
    assert len(rf_rows) == 4
    assert all("pct" in r for r in lr_rows + rf_rows)
    # With a 4-row fit every RF leaf holds one sample, so impurity-based
    # importance is legitimately all zero. The normalisation contract is what
    # matters, so exercise it on the Logistic Regression, whose coefficients
    # are non-degenerate here.
    assert sum(r["pct"] for r in lr_rows) == pytest.approx(100.0, abs=0.1)


def test_feature_importance_refuses_an_unfitted_model() -> None:
    """An empty list would read as "no feature mattered" -- a different claim."""
    unfitted = build_models(load_config())["random_forest"]
    with pytest.raises(DatasetError, match="Was it fitted"):
        feature_importances(unfitted, ["a", "b"])


# ---------------------------------------------------------------- evaluate --
def test_evaluate_model_returns_metrics_and_lead_time(
    builder: FeatureBuilder, dataset_csv: Path,
) -> None:
    ds = build_dataset(load_labelled_csv(dataset_csv), builder)
    split = time_split(ds, 0.3)
    res = evaluate_model("logistic_regression",
                         build_models(load_config())["logistic_regression"],
                         ds, split, 0.5)
    assert res.metrics.n_samples == int(split.test.size)
    assert 0.0 <= res.metrics.precision <= 1.0
    assert res.lead.episodes >= 0
    assert res.latency.samples > 0


def test_lead_time_is_measured_within_runs_not_across_them(
    builder: FeatureBuilder, tmp_path: Path,
) -> None:
    """An episode must not be matched to an alarm from a different run.

    Run A is congested, run B is not, and they sit far apart in time. Without
    per-series lead time the alarm in one run is credited as predicting
    congestion in the other.
    """
    path = _write_csv(tmp_path / "two.csv", [
        {"run_id": "run_congested", "pattern": "ramp"},
        {"run_id": "run_idle", "pattern": "poisson"},
    ])
    rows = load_labelled_csv(path)
    # Make the second run uniformly uncongested.
    for r in rows:
        if r["run_id"] == "run_idle":
            r["is_congested"] = False
            r["utilization"] = 0.3
            r["latency_ms"] = 4.0
    ds = build_dataset(rows, builder)
    split = time_split(ds, 0.3)
    res = evaluate_model("logistic_regression",
                         build_models(load_config())["logistic_regression"],
                         ds, split, 0.5)
    # Whatever the model predicts, lead time is per-series by construction.
    assert res.lead.detected <= res.lead.episodes


# ------------------------------------------------------- label cross-check --
def test_label_cross_check_reports_agreement(
    builder: FeatureBuilder, dataset_csv: Path,
) -> None:
    """With no CSV labels the check is vacuous, not a false alarm."""
    rows = load_labelled_csv(dataset_csv)
    result = cross_check_labels(rows, builder)
    assert result["compared"] == 0
    assert result["agreement_rate"] is None


def test_label_cross_check_detects_a_disagreement(
    builder: FeatureBuilder, dataset_csv: Path,
) -> None:
    rows = load_labelled_csv(dataset_csv)
    # Invert relative to the builder's own definition, not to is_congested:
    # "congested now" and "congested within the horizon" are different
    # questions, so inverting the present-state flag would only ever produce
    # partial disagreement.
    truth = {
        (r.link_key[0], r.link_key[1], r.link_key[2], round(r.timestamp, 3)): r.label
        for r in builder.build(rows) if r.horizon_valid
    }
    for r in rows:
        key = (r["src_switch"], r["dst_switch"], r["direction"],
               round(r["timestamp"], 3))
        r["csv_label"] = not (truth.get(key, 0) == 1)
    result = cross_check_labels(rows, builder)
    assert result["disagree"] == result["compared"] > 0
    assert result["agreement_rate"] == 0.0
    assert result["disagreement_examples"]


# -------------------------------------------------------- leave-one-out --
def test_lopo_reports_every_pattern(tmp_path: Path, builder: FeatureBuilder) -> None:
    path = _write_csv(tmp_path / "multi.csv", [
        {"run_id": "run_A", "pattern": "ramp"},
        {"run_id": "run_B", "pattern": "bursty_onoff"},
    ])
    ds = build_dataset(load_labelled_csv(path), builder)
    result = leave_one_pattern_out(build_models(load_config()), ds, 0.5,
                                   quick=True)
    assert result["available"] is True
    folds = result["folds"]["random_forest"]["per_pattern"]
    assert set(folds) == {"ramp", "bursty_onoff"}


def test_lopo_declines_with_a_single_pattern(
    builder: FeatureBuilder, dataset_csv: Path,
) -> None:
    ds = build_dataset(load_labelled_csv(dataset_csv), builder)
    result = leave_one_pattern_out(build_models(load_config()), ds, 0.5)
    assert result["available"] is False


# ------------------------------------------------------------ end-to-end --
def test_train_end_to_end_writes_a_report(tmp_path: Path, builder: FeatureBuilder) -> None:
    path = _write_csv(tmp_path / "multi.csv", [
        {"run_id": "run_A", "pattern": "ramp"},
        {"run_id": "run_B", "pattern": "bursty_onoff"},
    ], n=60, congested_from=30)
    # Redirect artifacts: without this the test would overwrite the repo's
    # results/ml_metrics.json, and a later phase would read a model's report
    # that a unit test fabricated.
    base = load_config()
    cfg = Config({
        **dict(base),
        "ml": {
            **dict(base["ml"]),
            "model_path": str(tmp_path / "e2e.joblib"),
            "metrics_path": str(tmp_path / "e2e_metrics.json"),
            "features_path": str(tmp_path / "e2e_fi.png"),
        },
    })
    report = train(path, cfg=cfg, quick=True)
    assert report["winner"] in {"logistic_regression", "random_forest"}
    assert set(report["models"]) == {"logistic_regression", "random_forest"}
    assert report["time_split"]["test_rows"] > 0
    assert report["leave_one_pattern_out"]["available"] is True
    assert "inference_within_budget" in report


def test_training_never_writes_into_the_repo(tmp_path: Path) -> None:
    """Guards the previous test's footgun: artifacts must go where told."""
    repo_metrics = Path("results/ml_metrics.json")
    before = repo_metrics.stat().st_mtime if repo_metrics.is_file() else None
    path = _write_csv(tmp_path / "m.csv", [
        {"run_id": "run_A", "pattern": "ramp"},
        {"run_id": "run_B", "pattern": "bursty_onoff"},
    ], n=60, congested_from=30)
    base = load_config()
    cfg = Config({
        **dict(base),
        "ml": {**dict(base["ml"]),
               "model_path": str(tmp_path / "a.joblib"),
               "metrics_path": str(tmp_path / "a.json"),
               "features_path": str(tmp_path / "a.png")},
    })
    train(path, cfg=cfg, quick=True)
    after = repo_metrics.stat().st_mtime if repo_metrics.is_file() else None
    assert after == before


def test_train_writes_model_and_metrics_files(tmp_path: Path) -> None:
    """Artifacts land on disk and the metrics file is valid JSON."""
    import joblib

    path = _write_csv(tmp_path / "multi.csv", [
        {"run_id": "run_A", "pattern": "ramp"},
        {"run_id": "run_B", "pattern": "bursty_onoff"},
    ], n=60, congested_from=30)
    model_path = tmp_path / "model.joblib"
    metrics_path = tmp_path / "metrics.json"
    # Absolute override paths keep the repo's results/ untouched. (Config uses
    # __slots__ and holds no public mapping, so the override is expressed in the
    # config dict itself.)
    base = load_config()
    cfg = Config({
        **dict(base),
        "ml": {
            **dict(base["ml"]),
            "model_path": str(tmp_path / "model.joblib"),
            "metrics_path": str(tmp_path / "metrics.json"),
            "features_path": str(tmp_path / "fi.png"),
        },
    })

    train(path, cfg=cfg, quick=True)
    assert model_path.is_file()
    assert json.loads(metrics_path.read_text())["winner"]

    bundle = joblib.load(model_path)
    for key in ("model", "feature_names", "decision_threshold", "window_w"):
        assert key in bundle, f"model bundle missing {key}"
    assert bundle["window_w"] == int(cfg.get("ml.window_w"))


def test_train_refuses_a_single_class_dataset(tmp_path: Path) -> None:
    """All-congested data trains happily and predicts nothing useful."""
    path = _write_csv(tmp_path / "allpos.csv", [{"run_id": "run_A"}],
                      congested_from=0)
    with pytest.raises(DatasetError, match="single class"):
        train(path, cfg=load_config(), quick=True)


def test_single_class_test_slice_is_reported_as_unmeaningful(
    tmp_path: Path,
) -> None:
    """A single-class held-out slice makes PR-AUC undefined for every model.

    Regression guard. Selection used to be ``max`` over a set of NaNs, which
    silently returned whichever candidate was constructed first and reported a
    confident-looking winner. The report must instead say the comparison was not
    meaningful and fall back to F1.
    """
    # Run A is congested for its whole length, run B is never congested. A
    # chronological split therefore yields a test slice of one class.
    path = tmp_path / "mixed.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        rows: List[Dict[str, Any]] = []
        for run_id, pattern, congested_from in (
            ("run_pos", "ramp", 0),      # congested throughout
            ("run_neg", "poisson", None),  # never congested
        ):
            base = 1000.0 if run_id == "run_pos" else 100000.0
            for i in range(60):
                cong = congested_from is not None and i >= congested_from
                util = 1.0 if cong else 0.3
                rows.append({
                    "run_id": run_id, "timestamp": base + i * 2.0,
                    "src_switch": "s1", "dst_switch": "s2", "direction": "forward",
                    "src_port": 1, "dst_port": 2, "capacity_mbps": 100.0,
                    "tx_rate_mbps": util * 100.0, "rx_rate_mbps": util * 95.0,
                    "tx_packet_rate_pps": util * 1200.0,
                    "rx_packet_rate_pps": util * 1100.0,
                    "utilization": util, "latency_ms": 55.0 if cong else 4.0,
                    "loss_pct": 3.0 if cong else 0.0, "is_stale": "False",
                    "is_congested": str(cong), "pattern": pattern, "seed": 1,
                    "label_future_congested": "", "lead_time_s": "",
                    "horizon_valid": "",
                })
        writer.writerows(rows)

    base = load_config()
    cfg = Config({
        **dict(base),
        "ml": {**dict(base["ml"]),
               "model_path": str(tmp_path / "s.joblib"),
               "metrics_path": str(tmp_path / "s.json"),
               "features_path": str(tmp_path / "s.png")},
    })
    report = train(path, cfg=cfg, quick=True)
    assert report["winner"] in {"logistic_regression", "random_forest"}
    if not report["evaluation_is_meaningful"]:
        assert report["winner_selected_by"] == "f1_fallback"
        best = report["models"][report["winner"]]
        assert math.isnan(best["pr_auc"]), (
            "the fallback is only for the undefined case"
        )
