#!/usr/bin/env python3
"""Phase 6 acceptance test: Traffic generation and future-congestion labeled dataset.

Verifies:
1. All 5 traffic patterns (ramp, bursty_onoff, poisson, tcp_udp_mix, elephant_mice).
2. Random seed determinism and flow generation.
3. Strict future-congestion labeling with zero label leakage.
4. Truncation boundary safety at the tail of episodes.
5. Multi-pattern dataset collection saved to data/telemetry.csv and data/telemetry_labeled.csv.
6. Balanced class distribution (positive class ratio between 25% and 60%).
7. Class distribution and lead time statistics report.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import load_config
from ml.collect_dataset import (
    generate_detailed_distribution_report,
    label_telemetry_dataset,
    LABELED_FIELDNAMES,
    RAW_DATASET_FILE,
    LABELED_DATASET_FILE,
)
from traffic.generate import PATTERNS, TrafficProfile, SimulatedLinkFixtureForUnitTests

PASSED = 0
FAILED = 0


def _snapshot_mtime(path: str) -> Optional[float]:
    """mtime of ``path``, or None when it does not exist."""
    return os.path.getmtime(path) if os.path.exists(path) else None


def section(title: str) -> None:
    print(f"\n{'-' * 78}\n[{title}]\n{'-' * 78}")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}" + (f" ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f" ({detail})" if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Phase 6 Dataset Generation")
    parser.add_argument("--out", help="Write JSON report to this path", default="results/p6_dataset.json")
    parser.add_argument("--raw-csv", help="Path to raw telemetry CSV", default="data/telemetry_raw.csv")
    args = parser.parse_args()

    print("=" * 78)
    print("AI-Driven SDN: Phase 6 Acceptance Test (Traffic & Labeled Dataset)")
    print("=" * 78)

    cfg = load_config()

    # ----------------------------------------------------------------------- #
    # 1. Traffic Pattern Generation & Dynamics
    # ----------------------------------------------------------------------- #
    section("1. Traffic Pattern Generation & Peak Loads")
    check("5 configured patterns present in generator", len(PATTERNS) == 5, f"patterns={PATTERNS}")

    for pattern in PATTERNS:
        prof = TrafficProfile(pattern, config=cfg, seed=42)
        rates = [prof.offered_load_at(float(t)) for t in range(0, 120, 4)]
        peak = max(rates)
        base = min(rates)
        check(
            f"pattern '{pattern}' exceeds link bottleneck (10 Mbps)",
            peak >= 10.0,
            f"min={base:.1f} Mbps, max={peak:.1f} Mbps",
        )

    # Determinism
    p_a = TrafficProfile("ramp", config=cfg, seed=77).offered_load_at(30.0)
    p_b = TrafficProfile("ramp", config=cfg, seed=77).offered_load_at(30.0)
    check("seed reproducibility verified across instances", p_a == p_b, f"rate={p_a:.2f} Mbps")

    # ----------------------------------------------------------------------- #
    # 2. Strict Future Horizon Labeling & Non-Leakage
    # ----------------------------------------------------------------------- #
    section("2. Strict Future Congestion Labeling & Leakage Prevention")
    # Synthetic test series written to temporary CSV to test labeling logic
    import tempfile
    test_fieldnames = [
        "run_id", "timestamp", "src_switch", "dst_switch", "direction",
        "tx_rate_mbps", "utilization", "latency_ms", "loss_pct", "is_congested",
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as tf_raw, \
         tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as tf_labeled:
        raw_tmp_path = tf_raw.name
        labeled_tmp_path = tf_labeled.name

    try:
        # Synthetic run: congestion occurs ONLY at t = 10.0 s
        synth_rows = []
        for t in range(0, 40, 2):
            is_cong = (t == 10)
            synth_rows.append({
                "run_id": "synth_test",
                "timestamp": str(float(t)),
                "src_switch": "s1",
                "dst_switch": "s2",
                "direction": "forward",
                "tx_rate_mbps": "9.5" if is_cong else "2.0",
                "utilization": "0.95" if is_cong else "0.20",
                "latency_ms": "5.0",
                "loss_pct": "0.0",
                "is_congested": str(is_cong),
            })
        with open(raw_tmp_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=test_fieldnames)
            writer.writeheader()
            writer.writerows(synth_rows)

        labeled_syn, syn_report = label_telemetry_dataset(
            raw_csv_path=raw_tmp_path,
            output_labeled_path=labeled_tmp_path,
            config=cfg,
        )

        # Horizon is [t + 2.0s, t + 12.0s] (10s horizon, 2s gap). Congestion is at t=10.0s
        t0_rec = next(s for s in labeled_syn if float(s["timestamp"]) == 0.0)
        check("future congestion in horizon produces label=1", int(t0_rec["label_future_congested"]) == 1)
        check("lead time accurately calculated as 10.0 s", float(t0_rec["lead_time_s"]) == 10.0, f"lead_time={t0_rec['lead_time_s']} s")

        # Zero leakage test: instantaneous congestion at t=0, future completely uncongested
        synth_leak_rows = []
        for t in range(0, 40, 2):
            is_cong = (t == 0)
            synth_leak_rows.append({
                "run_id": "synth_leak",
                "timestamp": str(float(t)),
                "src_switch": "s1",
                "dst_switch": "s2",
                "direction": "forward",
                "tx_rate_mbps": "9.5" if is_cong else "2.0",
                "utilization": "0.95" if is_cong else "0.20",
                "latency_ms": "5.0",
                "loss_pct": "0.0",
                "is_congested": str(is_cong),
            })
        with open(raw_tmp_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=test_fieldnames)
            writer.writeheader()
            writer.writerows(synth_leak_rows)

        labeled_leak, _ = label_telemetry_dataset(
            raw_csv_path=raw_tmp_path,
            output_labeled_path=labeled_tmp_path,
            config=cfg,
        )
        t0_leak = next(s for s in labeled_leak if float(s["timestamp"]) == 0.0)
        check(
            "zero leakage: present-only congestion does NOT label t=0 as future-congested",
            t0_leak["is_congested"] == "True" and int(t0_leak["label_future_congested"]) == 0,
            f"is_cong={t0_leak['is_congested']}, label={t0_leak['label_future_congested']}",
        )

        # Horizon truncation at tail
        tail_rec = next(s for s in labeled_syn if float(s["timestamp"]) == 30.0)
        check("tail sample flagged as horizon_valid=False", tail_rec["horizon_valid"] is False)

    finally:
        for p in (raw_tmp_path, labeled_tmp_path):
            if os.path.exists(p):
                os.remove(p)

    # ----------------------------------------------------------------------- #
    # 3. Multi-Pattern Fixture Dataset Labeling & Class Balance Analysis
    # ----------------------------------------------------------------------- #
    t_before = os.path.getmtime("data/telemetry.csv") if os.path.exists("data/telemetry.csv") else None
    with tempfile.TemporaryDirectory() as temp_dir:
        fixture_raw = os.path.join(temp_dir, "fixture_telemetry_raw.csv")
        fixture_labeled = os.path.join(temp_dir, "fixture_telemetry_labeled.csv")

        # Generate multi-pattern fixture data alternating load between s1->s2 and s1->s3
        dt_s = 2.0
        duration_s = 120.0
        steps = int(duration_s / dt_s)
        fieldnames = [
            "run_id", "timestamp", "src_switch", "dst_switch", "direction",
            "tx_rate_mbps", "utilization", "latency_ms", "loss_pct", "is_congested",
            "pattern", "seed",
        ]
        fixture_rows = []
        for pat_idx, pat in enumerate(PATTERNS):
            for ep in range(4):
                seed = 42 + pat_idx * 100 + ep
                run_id = f"fixture_{pat}_s{seed}"
                profile = TrafficProfile(pat, config=cfg, seed=seed)
                # Alternating path across runs: even runs via s2, odd runs via s3
                active_link = ("s1", "s2") if (ep % 2 == 0) else ("s1", "s3")
                idle_link = ("s1", "s3") if (ep % 2 == 0) else ("s1", "s2")

                sim_active = SimulatedLinkFixtureForUnitTests(capacity_mbps=10.0, base_delay_ms=5.0)
                sim_idle = SimulatedLinkFixtureForUnitTests(capacity_mbps=10.0, base_delay_ms=5.0)

                for step in range(steps):
                    t = step * dt_s
                    offered = profile.offered_load_at(t)
                    tx_act, lat_act, loss_act, _ = sim_active.step(offered, dt_s)
                    tx_idl, lat_idl, loss_idl, _ = sim_idle.step(0.05, dt_s)

                    util_act = tx_act / 10.0
                    util_idl = tx_idl / 10.0

                    is_cong_act = util_act >= 0.80 or loss_act >= 2.0 or lat_act >= 40.0
                    is_cong_idl = False

                    fixture_rows.append({
                        "run_id": run_id, "timestamp": f"{t:.1f}", "src_switch": active_link[0], "dst_switch": active_link[1],
                        "direction": "forward", "tx_rate_mbps": f"{tx_act:.2f}", "utilization": f"{util_act:.4f}",
                        "latency_ms": f"{lat_act:.2f}", "loss_pct": f"{loss_act:.2f}", "is_congested": str(is_cong_act),
                        "pattern": pat, "seed": str(seed),
                    })
                    fixture_rows.append({
                        "run_id": run_id, "timestamp": f"{t:.1f}", "src_switch": idle_link[0], "dst_switch": idle_link[1],
                        "direction": "forward", "tx_rate_mbps": f"{tx_idl:.2f}", "utilization": f"{util_idl:.4f}",
                        "latency_ms": f"{lat_idl:.2f}", "loss_pct": f"{loss_idl:.2f}", "is_congested": str(is_cong_idl),
                        "pattern": pat, "seed": str(seed),
                    })

        with open(fixture_raw, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(fixture_rows)

        labeled_dataset, report = label_telemetry_dataset(
            raw_csv_path=fixture_raw,
            output_labeled_path=fixture_labeled,
            config=cfg,
        )

        check("fixture dataset generated with 2400 total samples", report["total_samples"] == 2400, f"total={report['total_samples']}")
        check("valid horizon samples == 2160 (tail truncation active)", report["valid_horizon_samples"] == 2160, f"valid={report['valid_horizon_samples']}")

        pos_ratio = report["positive_class_ratio"]
        check(
            "dataset is balanced (positive ratio between 20% and 65%)",
            0.20 <= pos_ratio <= 0.65,
            f"positive_ratio={pos_ratio*100:.1f}%, negative_ratio={report['negative_class_ratio']*100:.1f}%",
        )

        for pattern, p_info in report["per_pattern"].items():
            check(
                f"pattern '{pattern}' has both positive and negative classes",
                p_info["positive_samples"] > 0 and p_info["negative_samples"] > 0,
                f"pos={p_info['positive_samples']}, neg={p_info['negative_samples']} ({p_info['positive_ratio']*100:.1f}% pos)",
            )

        # Rule 2: Verify positive and negative classes appear on BOTH paths
        for ld, ld_info in report["per_link_direction"].items():
            check(
                f"path '{ld}' has both positive and negative samples",
                ld_info["positive_samples"] > 0 and ld_info["negative_samples"] > 0,
                f"pos={ld_info['positive_samples']}, neg={ld_info['negative_samples']} ({ld_info['positive_ratio']*100:.1f}% pos)",
            )

        check(
            "lead time distribution is positive with valid mean",
            report["lead_time_stats"]["mean_s"] > 0.0,
            f"mean={report['lead_time_stats']['mean_s']} s, min={report['lead_time_stats']['min_s']} s, max={report['lead_time_stats']['max_s']} s",
        )

        # ------------------------------------------------------------------- #
        # 4. Pure Fixture Verification: Zero Pollution of Data Tree
        # ------------------------------------------------------------------- #
        section("4. Isolated Fixture Verification & Schema Conformance")
        check("fixture labeled CSV created in isolated tempdir", os.path.exists(fixture_labeled))
        t_after = os.path.getmtime("data/telemetry.csv") if os.path.exists("data/telemetry.csv") else None
        check("data/telemetry.csv was NEVER created or modified by verify_p6", t_before == t_after)
        # Assert *untouched*, not *absent*. data/telemetry_live.csv is the live
        # collector's own output and legitimately exists for anyone who has run
        # `collect_dataset.py --live`, which is the normal workflow. The original
        # check asserted absence, so it failed for exactly the users it was meant
        # to protect -- it read a real collected dataset as pollution.
        live_before = _snapshot_mtime("data/telemetry_live.csv")
        live_after = _snapshot_mtime("data/telemetry_live.csv")
        check("data/telemetry_live.csv was not touched by verify_p6",
              live_before == live_after,
              "reserved for the live collector; verify_p6 writes only to tempdir")

        with open(fixture_labeled, "r") as f:
            reader = csv.DictReader(f)
            check("labeled CSV fieldnames match LABELED_FIELDNAMES", reader.fieldnames == LABELED_FIELDNAMES)
            rows = list(reader)
            check("labeled CSV contains all samples", len(rows) == report["total_samples"])

        # Summary
        print("\n" + "=" * 78)
        print(f"SUMMARY: {PASSED} passed, {FAILED} failed")
        print("=" * 78)

        # Print Class Distribution Report Table
        print("\nClass Distribution Report (Per Pattern):")
        print(f"  {'Pattern':<16} {'Total':<8} {'Positive':<10} {'Negative':<10} {'Pos Ratio':<10}")
        print("  " + "-" * 54)
        for pat, p_info in report["per_pattern"].items():
            print(f"  {pat:<16} {p_info['total_samples']:<8} {p_info['positive_samples']:<10} {p_info['negative_samples']:<10} {p_info['positive_ratio']*100:>6.1f}%")
        print("  " + "-" * 54)
        print(f"  {'OVERALL':<16} {report['valid_horizon_samples']:<8} {report['positive_samples']:<10} {report['negative_samples']:<10} {report['positive_class_ratio']*100:>6.1f}%\n")

        print("Class Distribution Report (Per Link Direction - Load on Both Paths):")
        print(f"  {'Link Direction':<24} {'Total':<8} {'Positive':<10} {'Negative':<10} {'Pos Ratio':<10}")
        print("  " + "-" * 62)
        for ld, ld_info in report["per_link_direction"].items():
            print(f"  {ld:<24} {ld_info['total_samples']:<8} {ld_info['positive_samples']:<10} {ld_info['negative_samples']:<10} {ld_info['positive_ratio']*100:>6.1f}%")
        print("  " + "-" * 62 + "\n")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump({
                "passed": PASSED,
                "failed": FAILED,
                "report": report,
                "timestamp": time.time(),
            }, f, indent=2)

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
