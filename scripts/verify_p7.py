#!/usr/bin/env python3
"""Phase 7 acceptance test: ML pipeline for future-congestion prediction.

Verifies the properties that make a congestion predictor *trustworthy*, not
merely present:

1. **Feature construction** -- sliding windows over trailing telemetry only, with
   a deterministic column order and no identity (link-name) features, so the
   model cannot memorise which switch is busy.
2. **Leakage guards** -- features are backward-only (mutating the future cannot
   change an already-built row), the label horizon starts strictly after the
   window, and a row whose horizon was never observed is excluded rather than
   labelled negative.
3. **Chronological split** -- the test slice is strictly later than the training
   slice. A random split would leak near-duplicate windows.
4. **Model comparison** -- Logistic Regression vs Random Forest, both trained,
   scored on held-out future data, and compared on PR-AUC.
5. **Lead time** -- reported in seconds; a model that only reacts after
   congestion scores well on F1 and uselessly here.
6. **Inference budget** -- p95 scoring latency fits inside ``ml.max_inference_ms``,
   which is the constraint that makes P8 viable at all.
7. **Generalisation** -- leave-one-traffic-pattern-out validation.
8. **Artifact integrity** -- the saved bundle carries its feature names and
   window geometry, so P8 can refuse a mismatched feature set.

Usage
-----
    python scripts/verify_p7.py                  # needs results/model.joblib
    python scripts/verify_p7.py --out results/p7_acceptance.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import load_config  # noqa: E402
from ml.evaluate import binary_metrics  # noqa: E402
from ml.features import (  # noqa: E402
    BASE_FIELDS,
    FeatureBuilder,
    WindowFeatureRow,
    assert_no_identity_features,
    feature_names,
)
from ml.train import (  # noqa: E402
    DatasetError,
    _config_path,
    build_dataset,
    cross_check_labels,
    load_labelled_csv,
    time_split,
)

PASSED = 0
FAILED = 0


def check(condition: bool, label: str, detail: str = "") -> bool:
    """Record one pass/fail verdict and echo it."""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}" + (f" -- {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))
    return bool(condition)


def check_raises(
    fn: Callable[[], Any], exc_type: type, label: str, pattern: str = "",
) -> bool:
    """Record a pass if calling ``fn`` raises ``exc_type``."""
    try:
        fn()
    except exc_type as exc:
        return check(pattern in str(exc), label, f"raised {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001
        return check(False, label, f"raised {type(exc).__name__}: {exc}")
    return check(False, label, "no exception raised")


# --------------------------------------------------------------------------- #
# Synthetic series, so the checks are independent of the collected dataset
# --------------------------------------------------------------------------- #
def synth_series(
    n: int, *, congested_from: Optional[int] = None, link: str = "s1",
    dst: str = "s2", run_id: str = "run_test", pattern: str = "ramp",
    start: float = 1000.0, dt: float = 2.0,
) -> List[Dict[str, Any]]:
    """A single link-run of telemetry that ramps, optionally congesting."""
    rows: List[Dict[str, Any]] = []
    for i in range(n):
        congested = congested_from is not None and i >= congested_from
        util = 1.0 if congested else min(0.2 + i * 0.02, 0.75)
        rows.append({
            "timestamp": start + i * dt,
            "src_switch": link, "dst_switch": dst, "direction": "forward",
            "run_id": run_id, "pattern": pattern,
            "utilization": util,
            "latency_ms": 55.0 if congested else 4.0,
            "loss_pct": 3.0 if congested else 0.0,
            "tx_rate_mbps": util * 100.0,
            "rx_rate_mbps": util * 95.0,
            "tx_packet_rate_pps": util * 1200.0,
            "rx_packet_rate_pps": util * 1100.0,
            "is_congested": congested,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="results/p7_acceptance.json")
    args = parser.parse_args()

    cfg = load_config()
    root = Path(__file__).resolve().parents[1]
    report: Dict[str, Any] = {}

    # ---------------------------------------------------------------------
    print("\n[1/8] Feature construction")
    # ---------------------------------------------------------------------
    builder = FeatureBuilder(
        window_w=int(cfg.get("ml.window_w")),
        sample_interval_s=float(cfg.get("ml.sample_interval_s")),
        horizon_n_s=float(cfg.get("ml.horizon_n_s")),
        label_gap_s=float(cfg.get("ml.label_gap_s")),
        fields=tuple(cfg.get("ml.features", BASE_FIELDS)),
    )
    check(builder.window_w == int(cfg.get("ml.window_w")),
          "window width matches config", f"window_w={builder.window_w}")
    check(builder.sample_interval_s == float(cfg.get("ml.sample_interval_s")),
          "sample interval matches telemetry cadence",
          f"{builder.sample_interval_s}s")
    check(len(builder.feature_names) == len(builder.fields) * 4,
          "four window statistics per allowlisted field",
          f"{len(builder.feature_names)} columns from {len(builder.fields)} fields")
    check(feature_names(("utilization", "latency_ms"))[:4] ==
          ["utilization__last", "utilization__mean", "utilization__slope",
           "utilization__std"],
          "feature column order is deterministic")

    no_identity = not any(
        n.startswith(("src_", "dst_", "link", "direction"))
        for n in builder.feature_names
    )
    check(no_identity, "no link-identity features in the matrix",
          f"top feature: {builder.feature_names[0]}")
    check_raises(
        lambda: assert_no_identity_features(["utilization__last", "src_switch"]),
        ValueError, "identity guard rejects a link-name column",
        "identity/target fields",
    )
    check_raises(
        lambda: FeatureBuilder(window_w=3, label_gap_s=0.0),
        ValueError, "zero guard band is rejected", "label_gap_s",
    )
    check_raises(
        lambda: FeatureBuilder(window_w=1), ValueError,
        "window too short for a slope is rejected", "window_w",
    )

    # ---------------------------------------------------------------------
    print("\n[2/8] Leakage guards")
    # ---------------------------------------------------------------------
    long_rows = synth_series(120, congested_from=70)
    clean = builder.build(long_rows)
    valid = [r for r in clean if r.horizon_valid]
    check(bool(valid), "long series yields valid labelled rows",
          f"{len(valid)} valid of {len(clean)} built")

    # (a) Features are backward-only: appending a spike must not perturb them.
    spiked = synth_series(120, congested_from=None, start=2000.0)
    for i, row in enumerate(spiked):
        row["utilization"] = 1.0
        row["latency_ms"] = 99.0
    before = builder.build(long_rows)
    after = builder.build(long_rows + spiked)
    # Appending legitimately creates *new* windows, so the property under test is
    # that the rows which already existed are bit-identical -- compare the
    # common prefix by (link, run, timestamp).
    key = lambda r: (r.link_key, r.run_id, r.timestamp)  # noqa: E731
    by_key = {key(r): r.values for r in after}
    shared = [r for r in before if key(r) in by_key]
    unchanged = (
        len(shared) == len(before)
        and all(by_key[key(r)] == r.values for r in shared)
    )
    check(unchanged and bool(shared),
          "features are backward-only (future cannot alter them)",
          f"{len(shared)} pre-existing rows unchanged "
          f"({len(after) - len(shared)} new windows from the appended data)")

    # (b) A sample congested exactly at t must not satisfy its own label.
    boundary = synth_series(60, congested_from=None)
    for i, row in enumerate(boundary):
        hit = i == 40
        row["is_congested"] = hit
        row["utilization"] = 1.0 if hit else 0.5
    built = builder.build(boundary)
    at_hit = [r for r in built if abs(r.timestamp - boundary[40]["timestamp"]) < 1e-6]
    check(bool(at_hit) and at_hit[0].label == 0,
          "window endpoint cannot label itself (guard band works)",
          f"label={at_hit[0].label if at_hit else 'n/a'}")

    # (c) A fully observed horizon with no congestion is a negative.
    quiet = synth_series(60, congested_from=None)
    for row in quiet:
        row["is_congested"] = False
        row["utilization"] = 0.4
    quiet_labels = {r.label for r in builder.build(quiet) if r.horizon_valid}
    check(quiet_labels == {0}, "uncongested horizon labels 0, not 1",
          f"labels={sorted(quiet_labels)}")

    # (d) Rows whose horizon was never observed are excluded, not labelled 0.
    tail = builder.build(synth_series(40, congested_from=20))
    check(any(not r.horizon_valid for r in tail)
          and all(r.label == -1 for r in tail if not r.horizon_valid),
          "unobserved horizon yields label=-1 and is excluded from training",
          f"{sum(not r.horizon_valid for r in tail)} of {len(tail)} rows excluded")

    # (e) Runs of one link are not concatenated into a single series.
    run_a = synth_series(40, congested_from=None, run_id="run_A", start=1000.0)
    run_b = synth_series(40, congested_from=25, run_id="run_B", start=5000.0)
    separate = builder.build(run_a + run_b)
    expected = max(0, len(run_a) - builder.window_w + 1) + \
        max(0, len(run_b) - builder.window_w + 1)
    check(len(separate) == expected,
          "collection runs of one link stay separate (no cross-run windows)",
          f"{len(separate)} rows, expected {expected}")

    # (f) Duplicate timestamps are rejected rather than silently mis-windowed.
    dupes = synth_series(20)
    dupes[5]["timestamp"] = dupes[4]["timestamp"]
    check_raises(lambda: builder.build(dupes), ValueError,
                 "duplicate timestamps rejected", "duplicate timestamp")

    report["leakage"] = {
        "backward_only": unchanged,
        "boundary_label_is_zero": bool(at_hit) and at_hit[0].label == 0,
        "quiet_horizon_labels": sorted(quiet_labels),
        "cross_run_windows": len(separate) == expected,
    }

    # ---------------------------------------------------------------------
    print("\n[3/8] Real dataset and chronological split")
    # ---------------------------------------------------------------------
    csv_path = _config_path(cfg, "dataset.labeled_csv_path",
                            "data/telemetry_live_labeled.csv")
    has_dataset = csv_path.is_file()
    if not has_dataset:
        print(f"  [SKIP] labelled dataset absent at {csv_path}")
        print("         run: sudo python ml/collect_dataset.py --live")
    else:
        rows = load_labelled_csv(csv_path)
        check(bool(rows), "labelled dataset loads",
              f"{len(rows)} rows from {csv_path.name}")
        try:
            ds = build_dataset(rows, builder)
        except DatasetError as exc:
            check(False, "dataset supports training", str(exc))
            ds = None

        if ds is not None:
            check(np.isfinite(ds.x).all(), "feature matrix is finite (no NaN/Inf)")
            check(ds.x.shape[1] == len(builder.feature_names),
                  "matrix width matches the feature allowlist",
                  f"{ds.x.shape[1]} columns")
            classes = set(np.unique(ds.y).tolist())
            check(classes == {0, 1}, "both classes present in the real dataset",
                  f"classes={sorted(classes)}")
            rate = float(ds.y.mean())
            check(0.02 <= rate <= 0.98, "positive rate is non-degenerate",
                  f"{rate:.1%} positive")

            try:
                split = time_split(ds, float(cfg.get("ml.test_fraction", 0.3)))
                chronological = (ds.timestamps[split.train].max()
                                 < ds.timestamps[split.test].min())
                check(chronological,
                      "split is chronological (test is strictly in the future)",
                      f"cut between {ds.timestamps[split.train].max():.0f} "
                      f"and {ds.timestamps[split.test].min():.0f}")
                check(split.train.size > 0 and split.test.size > 0,
                      "both split sides are populated",
                      f"{split.train.size} train / {split.test.size} test")
            except (DatasetError, ValueError) as exc:
                check(False, "chronological split succeeds", str(exc))

            label_check = cross_check_labels(rows, builder)
            if label_check["compared"]:
                rate_agree = label_check["agreement_rate"]
                check(rate_agree is not None and rate_agree >= 0.95,
                      "builder labels agree with the collector's labels",
                      f"{rate_agree:.1%} of {label_check['compared']} rows")
            else:
                print("  [SKIP] collector precomputed labels absent; "
                      "cross-check not applicable")
            report["dataset"] = {
                "csv": str(csv_path),
                "rows": len(ds),
                "positives": int(ds.y.sum()),
                "positive_rate": round(rate, 4),
                "series": len(set(ds.series_keys)),
                "label_cross_check": label_check,
            }

    # ---------------------------------------------------------------------
    print("\n[4/8] Trained artifacts")
    # ---------------------------------------------------------------------
    metrics_path = _config_path(cfg, "ml.metrics_path", "results/ml_metrics.json")
    model_path = _config_path(cfg, "ml.model_path", "results/model.joblib")
    trained = metrics_path.is_file() and model_path.is_file()
    if not trained:
        print(f"  [SKIP] no trained model at {model_path} "
              f"(run: make train)")
        print("         the dataset must be long enough "
              "(~36 s of contiguous traffic per link-run)")
    else:
        metrics = json.loads(metrics_path.read_text())
        report["metrics"] = metrics
        winner = metrics.get("winner")
        models = metrics.get("models", {})
        check(winner in models, "a winning model is recorded", str(winner))
        check(set(models) >= {"logistic_regression", "random_forest"},
              "both candidate models were compared", ", ".join(sorted(models)))

        best = models.get(winner, {})
        pr_auc = best.get("pr_auc")
        check(isinstance(pr_auc, (int, float)) and not math.isnan(pr_auc),
              "winner has a defined PR-AUC", f"{pr_auc}")
        check(isinstance(best.get("f1"), (int, float)),
              "winner has a defined F1", f"{best.get('f1')}")
        check(best.get("n_samples", 0) > 0, "winner was scored on held-out data",
              f"{best.get('n_samples')} test rows")
        check((best.get("f1") or 0) > 0,
              "winner beats a useless model on F1", f"F1={best.get('f1')}")

        lead = best.get("lead_time", {})
        check(lead.get("episodes", 0) > 0,
              "lead time was measured over real episodes",
              f"{lead.get('episodes')} episodes")
        check(isinstance(lead.get("mean_s"), (int, float)),
              "mean lead time is reported", f"{lead.get('mean_s')}s")
        detection = lead.get("detection_rate")
        check(isinstance(detection, (int, float)),
              "detection rate reported (lead time is the operational metric)",
              f"{detection}")

        latency = best.get("inference_latency", {})
        budget = float(cfg.get("ml.max_inference_ms", 50.0))
        p95 = latency.get("p95_ms")
        check(isinstance(p95, (int, float)) and p95 <= budget,
              "inference fits the budget P8 depends on",
              f"p95={p95}ms <= {budget}ms")

        check(len(metrics.get("feature_names", [])) == len(builder.feature_names),
              "report records the exact feature set",
              f"{len(metrics.get('feature_names', []))} names")

        lopo = metrics.get("leave_one_pattern_out", {})
        check(lopo.get("available") is True,
              "leave-one-pattern-out validation ran",
              f"{len(lopo.get('folds', {}).get(winner, {}).get('per_pattern', {}))} folds")

        # -----------------------------------------------------------------
        print("\n[5/8] Model bundle integrity")
        # -----------------------------------------------------------------
        try:
            import joblib

            bundle = joblib.load(model_path)
            for key in ("model", "feature_names", "decision_threshold",
                        "window_w", "sample_interval_s"):
                check(key in bundle, f"bundle carries '{key}'")
            check(list(bundle.get("feature_names", [])) == list(builder.feature_names),
                  "bundle feature names match the current allowlist",
                  "a mismatch would let P8 score a reordered matrix")
            check(bundle.get("window_w") == builder.window_w,
                  "bundle window geometry matches config",
                  f"window_w={bundle.get('window_w')}")
            check(hasattr(bundle.get("model"), "predict_proba"),
                  "bundled model exposes predict_proba for P8")
        except Exception as exc:  # noqa: BLE001
            check(False, "model bundle loads", f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------------
    print(f"\nSUMMARY: {PASSED} passed, {FAILED} failed")
    report["passed"] = PASSED
    report["failed"] = FAILED

    out = Path(args.out)
    if not out.is_absolute():
        out = root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"report: {out}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
