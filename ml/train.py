"""Train the future-congestion predictor (P7).

Pipeline
--------
1. Load the labelled telemetry CSV produced by :mod:`ml.collect_dataset`.
2. Build sliding-window features with :class:`ml.features.FeatureBuilder`, which
   re-derives the label from ``is_congested`` rather than trusting the CSV's
   precomputed ``label_future_congested``. The two are then cross-checked; a
   disagreement is reported, not hidden, because two disagreeing definitions of
   the target is the fastest way to ship a meaningless model.
3. Split **chronologically**, never randomly. Telemetry rows are strongly
   autocorrelated, so a random split puts near-duplicates of a training window
   into the test set and reports a spectacular, fictional score.
4. Validate with leave-one-traffic-pattern-out: the model must generalise to a
   load shape it has never seen, because that is what happens in deployment.
5. Fit Logistic Regression and Random Forest, select on PR-AUC, and persist the
   winner together with its feature names, decision threshold and full metrics.

Usage
-----
    python -m ml.train                          # config defaults
    python -m ml.train --csv data/telemetry_live_labeled.csv
    python -m ml.train --quick                  # 3-fold LOPO, fewer trees
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.config import Config, load_config  # noqa: E402
from ml.evaluate import (  # noqa: E402
    LeadTimeStats,
    LatencyStats,
    binary_metrics,
    lead_time_stats,
    measure_inference_latency,
    normalised_importance,
)
from ml.features import BASE_FIELDS, FeatureBuilder  # noqa: E402

LOGGER = logging.getLogger("ml.train")

#: Numeric columns read from the labelled CSV.
_TELEMETRY_FIELDS = (
    "tx_rate_mbps", "rx_rate_mbps", "tx_packet_rate_pps", "rx_packet_rate_pps",
    "utilization", "latency_ms", "loss_pct",
)


class DatasetError(RuntimeError):
    """Raised when the dataset cannot support training."""


def _config_path(cfg: Config, dotted: str, default: str) -> Path:
    """Resolve a path-valued config entry to an absolute :class:`Path`.

    ``Config.path()`` is deliberately *not* used here: it returns the YAML
    location of a key (``config.yaml:ml.model_path``) for error messages, not a
    filesystem location. Config paths are repo-relative, so they are anchored to
    the project root rather than the process CWD -- otherwise training writes its
    artifacts wherever the shell happened to be.
    """
    value = cfg.get(dotted, default)
    path = Path(str(value))
    return path if path.is_absolute() else (ROOT / path)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _to_float(value: Any) -> Optional[float]:
    """Parse a CSV cell into a float, or ``None`` when it is unusable.

    Empty strings and non-numeric junk become ``None`` rather than raising, so
    one bad cell costs one feature row instead of the whole dataset. A row with
    a missing measurement is genuinely absent data, not a zero.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_bool(value: Any) -> bool:
    """Parse a CSV truth cell, accepting the usual spellings."""
    return str(value).strip().lower() in {"1", "true", "yes", "y", "t"}


def load_labelled_csv(path: str | os.PathLike[str]) -> List[Dict[str, Any]]:
    """Read the labelled telemetry CSV into typed records.

    Rows with an unusable timestamp or identity are dropped -- they cannot be
    grouped into a link series and would corrupt the window alignment.

    Raises
    ------
    FileNotFoundError
        If the CSV does not exist.
    DatasetError
        If no usable rows remain, with a count of why they were rejected.
    """
    csv_path = Path(path)
    if not csv_path.is_file():
        raise FileNotFoundError(
            f"labelled dataset not found: {csv_path}. "
            "Run `sudo python ml/collect_dataset.py --live` first."
        )

    rows: List[Dict[str, Any]] = []
    dropped_no_ts = 0
    dropped_no_link = 0
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            timestamp = _to_float(raw.get("timestamp"))
            if timestamp is None:
                dropped_no_ts += 1
                continue
            src = str(raw.get("src_switch", "") or "").strip()
            dst = str(raw.get("dst_switch", "") or "").strip()
            if not src or not dst:
                dropped_no_link += 1
                continue
            record: Dict[str, Any] = {
                "timestamp": timestamp,
                "src_switch": src,
                "dst_switch": dst,
                "direction": str(raw.get("direction", "forward") or "forward").strip(),
                "is_congested": _to_bool(raw.get("is_congested")),
                "run_id": str(raw.get("run_id", "") or ""),
                "pattern": str(raw.get("pattern", "") or ""),
            }
            csv_label = raw.get("label_future_congested")
            record["csv_label"] = _to_bool(csv_label) if csv_label not in (None, "") else None
            for field in _TELEMETRY_FIELDS:
                record[field] = _to_float(raw.get(field))
            rows.append(record)

    if not rows:
        raise DatasetError(
            f"{csv_path} yielded no usable rows "
            f"({dropped_no_ts} without a timestamp, {dropped_no_link} without a link)"
        )
    LOGGER.info(
        "loaded %d rows from %s (%d dropped: no ts, %d no link)",
        len(rows), csv_path.name, dropped_no_ts, dropped_no_link,
    )
    return rows


# --------------------------------------------------------------------------- #
# Dataset assembly
# --------------------------------------------------------------------------- #
@dataclass
class Dataset:
    """A training-ready matrix plus the metadata needed to split it."""

    x: np.ndarray
    y: np.ndarray
    timestamps: np.ndarray
    patterns: List[str]
    link_keys: List[Tuple[str, str, str]]
    feature_names: List[str]
    run_ids: List[str] = field(default_factory=list)
    series_keys: List[Tuple[str, ...]] = field(default_factory=list)
    n_rows_invalid: int = 0

    def __len__(self) -> int:
        """Number of training rows."""
        return int(self.x.shape[0])

    def summary(self) -> str:
        """One-line description used in logs and the metrics report."""
        positives = int(self.y.sum())
        rate = positives / len(self) if len(self) else 0.0
        return (
            f"{len(self)} rows x {self.x.shape[1]} features, "
            f"{positives} positive ({rate:.1%}), "
            f"{len(set(self.patterns))} patterns, "
            f"{len(set(self.link_keys))} links, "
            f"{len(set(self.series_keys))} link-run series, "
            f"{self.n_rows_invalid} rows dropped for an unobserved horizon"
        )


def build_dataset(
    rows: Sequence[Dict[str, Any]], builder: FeatureBuilder,
) -> Dataset:
    """Turn raw telemetry into a numeric, split-ready dataset.

    Parameters
    ----------
    rows:
        Records from :func:`load_labelled_csv`.
    builder:
        Feature/label builder. Its ``fields`` become the matrix columns.
    """
    built = builder.build(rows)
    x_list, y_list, keys_list, ts_list, pattern_list = [], [], [], [], []
    series_list: List[Tuple[str, ...]] = []
    run_list: List[str] = []

    n_invalid = 0
    for item in built:
        if not item.horizon_valid:
            n_invalid += 1
            continue
        x_list.append(item.values)
        y_list.append(item.label)
        keys_list.append(item.link_key)
        ts_list.append(item.timestamp)
        pattern_list.append(item.pattern or "unknown")
        series_list.append(item.link_key + (item.run_id,))
        run_list.append(item.run_id)

    if not x_list:
        raise DatasetError(
            "no valid training rows after windowing. The dataset is too short: "
            f"each link needs at least window_w={builder.window_w} samples plus "
            f"{builder.label_gap_s + builder.horizon_n_s:.0f}s of horizon "
            f"(~{(builder.window_w + (builder.label_gap_s + builder.horizon_n_s) / builder.sample_interval_s) * builder.sample_interval_s:.0f}s "
            "of contiguous traffic per link)."
        )

    x = np.asarray(x_list, dtype=float)
    y = np.asarray(y_list, dtype=int)
    if not np.isfinite(x).all():
        raise DatasetError("feature matrix contains NaN/Inf; refusing to train")

    return Dataset(
        x=x, y=y, timestamps=np.asarray(ts_list, dtype=float),
        patterns=pattern_list, link_keys=keys_list,
        feature_names=list(builder.feature_names), run_ids=run_list,
        series_keys=series_list, n_rows_invalid=n_invalid,
    )


def cross_check_labels(
    rows: Sequence[Dict[str, Any]], builder: FeatureBuilder,
) -> Dict[str, Any]:
    """Compare the builder's labels against the CSV's precomputed column.

    Two independent implementations of "will this link congest" should agree.
    When they do not, the report says so explicitly instead of silently
    preferring one -- a silent mismatch is how a model ends up trained on a
    target that P8 will never actually predict.
    """
    built = [r for r in builder.build(rows) if r.horizon_valid]
    index = {
        (r["src_switch"], r["dst_switch"], r["direction"], round(r["timestamp"], 3)): r
        for r in rows if r.get("csv_label") is not None
    }
    agree = disagree = missing = 0
    examples: List[Dict[str, Any]] = []
    for item in built:
        key = (item.link_key[0], item.link_key[1], item.link_key[2],
               round(item.timestamp, 3))
        csv_row = index.get(key)
        if csv_row is None:
            missing += 1
            continue
        if bool(csv_row["csv_label"]) == bool(item.label == 1):
            agree += 1
        else:
            disagree += 1
            if len(examples) < 5:
                examples.append({
                    "timestamp": item.timestamp,
                    "link": f"{item.link_key[0]}->{item.link_key[1]}",
                    "builder_label": int(item.label),
                    "csv_label": int(bool(csv_row["csv_label"])),
                })
    return {
        "compared": agree + disagree,
        "agree": agree,
        "disagree": disagree,
        "no_csv_label": missing,
        "agreement_rate": round(agree / (agree + disagree), 4) if (agree + disagree) else None,
        "disagreement_examples": examples,
    }


# --------------------------------------------------------------------------- #
# Splitting
# --------------------------------------------------------------------------- #
@dataclass
class Split:
    """A chronological train/test partition."""

    train: np.ndarray
    test: np.ndarray


def time_split(ds: Dataset, test_fraction: float) -> Split:
    """Split by timestamp: the last ``test_fraction`` of *time* is the test set.

    Raises
    ------
    ValueError
        If ``test_fraction`` leaves either side empty.
    """
    if not 0.0 < test_fraction < 1.0:
        raise ValueError(f"test_fraction must be in (0, 1), got {test_fraction}")
    order = np.argsort(ds.timestamps, kind="stable")
    # Split on the timestamp boundary so all links at the same instant stay
    # together -- otherwise the test set sees rows a training row "knew".
    unique_ts = np.unique(ds.timestamps[order])
    if len(unique_ts) < 2:
        raise DatasetError("cannot split: all rows share one timestamp")
    cut_index = int(round(len(unique_ts) * (1.0 - test_fraction)))
    cut_index = min(max(cut_index, 1), len(unique_ts) - 1)
    cutoff = unique_ts[cut_index]

    train = order[ds.timestamps[order] < cutoff]
    test = order[ds.timestamps[order] >= cutoff]
    if train.size == 0 or test.size == 0:
        raise ValueError(
            f"degenerate split at cutoff {cutoff:.1f}: "
            f"{train.size} train / {test.size} test rows"
        )
    LOGGER.info("time split at t=%.1f: %d train / %d test rows",
                cutoff, train.size, test.size)
    return Split(train=train, test=test)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
def build_models(cfg: Config, quick: bool = False) -> Dict[str, Any]:
    """Instantiate the configured candidate models.

    Both are probability-calibrated enough for a threshold decision and fast
    enough to score every link inside the telemetry interval; that constraint is
    why a deep model is not a candidate here.
    """
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    rf = cfg.get("ml.random_forest", {}) or {}
    lr = cfg.get("ml.logistic_regression", {}) or {}
    leaf = int(rf.get("min_samples_leaf", 5))
    models: Dict[str, Any] = {
        "logistic_regression": Pipeline([
            ("scale", StandardScaler()),
            ("clf", LogisticRegression(
                C=float(lr.get("C", 1.0)),
                max_iter=int(lr.get("max_iter", 2000)),
                class_weight=lr.get("class_weight", "balanced"),
                random_state=int(rf.get("random_state", 42)),
            )),
        ]),
        "random_forest": RandomForestClassifier(
            # ``quick`` trades a little accuracy for a fast sanity run; the leaf
            # floor of 1 keeps tiny datasets trainable.
            n_estimators=60 if quick else int(rf.get("n_estimators", 300)),
            max_depth=8 if quick else rf.get("max_depth", 12),
            min_samples_leaf=1 if quick else leaf,
            n_jobs=int(rf.get("n_jobs", -1)),
            random_state=int(rf.get("random_state", 42)),
        ),
    }
    return models


def feature_importances(model: Any, names: Sequence[str]) -> List[Dict[str, Any]]:
    """Extract per-feature importance from either candidate model.

    Handles the pipeline-wrapped Logistic Regression (use the classifier's
    coefficients) and a bare Random Forest (use impurity-based importances).

    Raises
    ------
    DatasetError
        If the model has not been fitted, or exposes neither attribute. Failing
        loudly matters: an empty importance list reads as "no feature mattered",
        which is a very different claim from "the model was never trained".
    """
    estimator = model
    if hasattr(model, "named_steps"):
        estimator = model.named_steps["clf"]
    if hasattr(estimator, "coef_"):
        # A binary LR has coef_ of shape (1, n_features).
        return normalised_importance(estimator.coef_.ravel(), names)
    if hasattr(estimator, "feature_importances_"):
        return normalised_importance(estimator.feature_importances_, names)
    raise DatasetError(
        f"cannot read feature importance from {type(estimator).__name__}: it "
        "exposes neither coef_ nor feature_importances_. Was it fitted?"
    )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
@dataclass
class ModelResult:
    """Evaluation outcome for one candidate model."""

    name: str
    metrics: Any
    lead: LeadTimeStats
    latency: LatencyStats
    importances: List[Dict[str, Any]]
    y_prob: np.ndarray = field(repr=False, default_factory=lambda: np.empty(0))


def evaluate_model(
    name: str, model: Any, ds: Dataset, split: Split,
    threshold: float,
) -> ModelResult:
    """Fit on the training slice and score the held-out slice.

    Lead time is measured per (link, run) series on the held-out slice, ordered
    by time, because an alarm only has meaning relative to the congestion that
    precedes it -- and an episode must not be measured across a run boundary.
    """
    x_train, y_train = ds.x[split.train], ds.y[split.train]
    x_test, y_test = ds.x[split.test], ds.y[split.test]
    model.fit(x_train, y_train)
    y_prob = model.predict_proba(x_test)[:, 1]

    metrics = binary_metrics(y_test, y_prob, threshold=threshold)
    latency = measure_inference_latency(model.predict_proba, x_test, repeats=20)

    lead = lead_time_stats([], [], [], threshold=threshold)
    # ``split.test`` is positional; ``y_prob`` was produced in that same order,
    # so position p in the loop corresponds to y_prob[p].
    for series in sorted(set(ds.series_keys[i] for i in split.test)):
        positions = [
            p for p, i in enumerate(split.test) if ds.series_keys[i] == series
        ]
        positions.sort(key=lambda p: ds.timestamps[split.test[p]])
        if not positions:
            continue
        idx = np.asarray([split.test[p] for p in positions], dtype=int)
        pos = np.asarray(positions, dtype=int)
        per_series = lead_time_stats(
            ds.timestamps[idx], ds.y[idx], y_prob[pos], threshold=threshold,
        )
        lead.episodes += per_series.episodes
        lead.detected += per_series.detected
        lead.per_episode.extend(per_series.per_episode)
    _recompute_lead_summary(lead)

    return ModelResult(
        name=name, metrics=metrics, lead=lead, latency=latency,
        importances=feature_importances(model, ds.feature_names),
        y_prob=y_prob,
    )


def _recompute_lead_summary(lead: LeadTimeStats) -> None:
    """Recompute mean/median/min/max after merging per-link episodes."""
    if not lead.per_episode:
        return
    values = np.asarray(lead.per_episode, dtype=float)
    lead.mean_s = float(values.mean())
    lead.median_s = float(np.median(values))
    lead.min_s = float(values.min())
    lead.max_s = float(values.max())


def leave_one_pattern_out(
    models: Dict[str, Any], ds: Dataset, threshold: float,
    cfg: Optional[Config] = None, quick: bool = False,
) -> Dict[str, Any]:
    """Validate generalisation to unseen traffic shapes.

    Each fold trains on every pattern but one and tests on the held-out one. A
    model that only memorises "bursts look like bursts" collapses here even when
    its random-split score looks excellent.
    """
    patterns = sorted(set(ds.patterns))
    if len(patterns) < 2:
        return {"available": False, "reason": "only one pattern in the dataset"}

    from sklearn.base import clone

    cfg = cfg or load_config()
    template = build_models(cfg, quick=quick)
    folds: Dict[str, Any] = {}
    for model_name in models:
        per_pattern: Dict[str, Any] = {}
        for held_out in patterns:
            mask = np.array([p == held_out for p in ds.patterns])
            if not mask.any():
                continue
            train_idx = np.flatnonzero(~mask)
            test_idx = np.flatnonzero(mask)
            y_test = ds.y[test_idx]
            if y_test.sum() == 0 or y_test.sum() == y_test.size:
                per_pattern[held_out] = {
                    "n_samples": int(test_idx.size),
                    "skipped": "single-class held-out slice",
                }
                continue
            fold_model = clone(template[model_name])
            fold_model.fit(ds.x[train_idx], ds.y[train_idx])
            prob = fold_model.predict_proba(ds.x[test_idx])[:, 1]
            per_pattern[held_out] = binary_metrics(y_test, prob, threshold).as_dict()
        scored = [v for v in per_pattern.values() if "pr_auc" in v]
        folds[model_name] = {
            "per_pattern": per_pattern,
            "mean_pr_auc": round(float(np.nanmean([v["pr_auc"] for v in scored])), 4)
            if scored else None,
            "mean_f1": round(float(np.mean([v["f1"] for v in scored])), 4)
            if scored else None,
        }
    return {"available": True, "folds": folds}


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def save_model(
    path: str | os.PathLike[str], model: Any, ds: Dataset,
    threshold: float, metrics: Dict[str, Any], builder: FeatureBuilder,
) -> Path:
    """Persist the winner with everything inference needs to validate input.

    ``feature_names`` is stored so P8 can refuse to score a matrix built with a
    different feature set -- a silent column-order mismatch would otherwise
    produce confident nonsense. The window geometry is stored for the same
    reason: features built over a different history length are not the same
    model.
    """
    import joblib

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model": model,
        "feature_names": ds.feature_names,
        "window_w": builder.window_w,
        "sample_interval_s": builder.sample_interval_s,
        "horizon_n_s": builder.horizon_n_s,
        "label_gap_s": builder.label_gap_s,
        "decision_threshold": threshold,
        "trained_at": metrics.get("trained_at"),
        "dataset": ds.summary(),
    }, out)
    LOGGER.info("saved model -> %s", out)
    return out


def save_feature_importance(
    path: str | os.PathLike[str], importances: Sequence[Dict[str, Any]],
) -> Optional[Path]:
    """Plot the top feature importances, if matplotlib is available.

    A missing plotting backend must not fail training, so this returns ``None``
    when the figure cannot be written.
    """
    if not importances:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - environment dependent
        LOGGER.warning("skipping feature importance plot: %s", exc)
        return None

    top = list(importances)[:15][::-1]
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(9, max(3.0, 0.32 * len(top) + 1.2)))
    ax.barh([r["feature"] for r in top], [r["pct"] for r in top],
            color="#4c72b0")
    ax.set_xlabel("relative importance (%)")
    ax.set_title("Feature importance (future congestion)")
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)
    LOGGER.info("saved feature importance plot -> %s", out)
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def train(
    csv_path: str | os.PathLike[str], cfg: Optional[Config] = None,
    quick: bool = False,
) -> Dict[str, Any]:
    """Run the full P7 training pipeline and return the metrics report.

    Raises
    ------
    DatasetError
        If the dataset is too small, too short, or single-class -- each of which
        would otherwise yield a model that trains happily and predicts garbage.
    """
    from datetime import datetime, timezone

    cfg = cfg or load_config()
    rows = load_labelled_csv(csv_path)
    builder = FeatureBuilder(
        window_w=int(cfg.get("ml.window_w")),
        sample_interval_s=float(cfg.get("ml.sample_interval_s")),
        horizon_n_s=float(cfg.get("ml.horizon_n_s")),
        label_gap_s=float(cfg.get("ml.label_gap_s")),
        fields=tuple(cfg.get("ml.features", BASE_FIELDS)),
    )
    LOGGER.info("features: %s", builder.describe())
    ds = build_dataset(rows, builder)
    LOGGER.info("dataset: %s", ds.summary())

    if ds.y.sum() == 0 or ds.y.sum() == len(ds):
        raise DatasetError(
            f"dataset has a single class ({int(ds.y.sum())} positive of {len(ds)}). "
            "Both classes must appear for the predictor to be meaningful."
        )

    label_check = cross_check_labels(rows, builder)
    if label_check["disagree"]:
        LOGGER.warning(
            "builder and CSV labels disagree on %d/%d rows (%.2f%% agree)",
            label_check["disagree"], label_check["compared"],
            100.0 * (label_check["agreement_rate"] or 0.0),
        )

    split = time_split(ds, float(cfg.get("ml.test_fraction", 0.3)))
    threshold = float(cfg.get("ml.decision_threshold", 0.5))
    models = build_models(cfg, quick=quick)

    results: Dict[str, ModelResult] = {}
    for name, model in models.items():
        try:
            results[name] = evaluate_model(name, model, ds, split, threshold)
            LOGGER.info(
                "%s: PR-AUC %.4f  F1 %.4f  lead %.1fs",
                name, results[name].metrics.pr_auc, results[name].metrics.f1,
                results[name].lead.mean_s,
            )
        except Exception as exc:  # keep the other candidate alive
            LOGGER.error("%s failed to train: %s", name, exc)

    if not results:
        raise DatasetError("every candidate model failed to train")

    # Select on PR-AUC, the headline metric for imbalanced classes. When the
    # held-out slice happens to be single-class PR-AUC is undefined for *every*
    # candidate; picking the "max" of a set of NaNs would silently choose
    # whichever model was constructed first. Fall back to F1 and, more
    # importantly, record that the comparison was not meaningful.
    def _pr_auc(name: str) -> float:
        value = results[name].metrics.pr_auc
        return -1.0 if (value is None or np.isnan(value)) else float(value)

    pr_auc_defined = any(_pr_auc(n) > -1.0 for n in results)
    if pr_auc_defined:
        winner_name = max(results, key=_pr_auc)
        selection = "pr_auc"
    else:
        winner_name = max(results, key=lambda n: results[n].metrics.f1)
        selection = "f1_fallback"
        LOGGER.warning(
            "PR-AUC is undefined for every candidate (the held-out slice is "
            "single-class: %d positive of %d test rows). Falling back to F1 to "
            "pick a model; the comparison is NOT meaningful until the dataset "
            "produces a test slice containing both classes.",
            int(results[winner_name].metrics.n_positive),
            results[winner_name].metrics.n_samples,
        )
    winner = results[winner_name]
    LOGGER.info("winner: %s (selected by %s)", winner_name, selection)

    lopo = leave_one_pattern_out(models, ds, threshold, cfg=cfg, quick=quick)

    report: Dict[str, Any] = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset_csv": str(csv_path),
        "dataset": ds.summary(),
        "feature_builder": builder.describe(),
        "feature_names": ds.feature_names,
        "rows_dropped_unobserved_horizon": ds.n_rows_invalid,
        "label_cross_check": label_check,
        "time_split": {
            "test_fraction": float(cfg.get("ml.test_fraction", 0.3)),
            "train_rows": int(split.train.size),
            "test_rows": int(split.test.size),
            "train_cutoff_ts": float(ds.timestamps[split.train].max())
            if split.train.size else None,
            "test_start_ts": float(ds.timestamps[split.test].min())
            if split.test.size else None,
        },
        "decision_threshold": threshold,
        "winner": winner_name,
        "winner_selected_by": selection,
        "evaluation_is_meaningful": bool(pr_auc_defined),
        "models": {
            name: {
                **res.metrics.as_dict(),
                "lead_time": res.lead.as_dict(),
                "inference_latency": res.latency.as_dict(),
                "feature_importance": res.importances,
            }
            for name, res in results.items()
        },
        "leave_one_pattern_out": lopo,
        "inference_budget_ms": float(cfg.get("ml.max_inference_ms", 50.0)),
        "inference_within_budget":
            winner.latency.p95_ms <= float(cfg.get("ml.max_inference_ms", 50.0)),
    }

    metrics_path = _config_path(cfg, "ml.metrics_path", "results/ml_metrics.json")
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(report, indent=2, default=str))

    save_model(_config_path(cfg, "ml.model_path", "results/model.joblib"),
               models[winner_name], ds, threshold, report, builder)
    save_feature_importance(
        _config_path(cfg, "ml.features_path", "results/feature_importance.png"),
        winner.importances,
    )

    print(f"winner: {winner_name}")
    print(f"  PR-AUC      {winner.metrics.pr_auc:.4f}")
    print(f"  precision   {winner.metrics.precision:.4f}")
    print(f"  recall      {winner.metrics.recall:.4f}")
    print(f"  F1          {winner.metrics.f1:.4f}")
    print(f"  lead time   {winner.lead.mean_s:.1f}s mean "
          f"({winner.lead.detection_rate:.0%} of {winner.lead.episodes} episodes)")
    print(f"  inference   {winner.latency.p95_ms:.2f}ms p95 "
          f"(budget {report['inference_budget_ms']}ms)")
    print(f"  metrics     {metrics_path}")
    return report


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Train the P7 congestion predictor")
    parser.add_argument("--csv", default=None, help="labelled telemetry CSV")
    parser.add_argument("--quick", action="store_true",
                        help="fewer trees, for a fast sanity run")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    csv_path = args.csv or str(
        _config_path(cfg, "dataset.labeled_csv_path", "data/telemetry_live_labeled.csv")
    )
    try:
        train(csv_path, cfg=cfg, quick=args.quick)
    except (DatasetError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
