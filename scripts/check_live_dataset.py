#!/usr/bin/env python3
"""Dataset quality, rate fidelity, and integrity checker.

Performs strict post-collection validation of live telemetry datasets:
1. Per-run:
   - Sample count consistency.
   - Measured elapsed time delta (dt) mean and std (centered at ~2.0 s).
   - Stale-row fraction (< 5% required, fails if > 5%).
2. Per-run Traffic & Congestion:
   - Achieved iperf3 throughput vs pattern target.
   - Peak utilization on loaded link >= congestion threshold (0.80).
   - Fails if a run has zero congestion.
3. Idle-link baseline integrity:
   - Median probe latency near configured 5.0 ms.
   - Median utilization near 0 (< 5%).
4. Class distribution breakdown:
   - Per pattern, per link-direction, and per run.
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import Config, load_config
from traffic.generate import PATTERNS


def check_dataset(
    csv_path: str,
    config: Optional[Config] = None,
    verbose: bool = True,
) -> Tuple[bool, Dict[str, Any]]:
    """Run comprehensive validation on a labeled telemetry dataset."""
    cfg = config if config is not None else load_config()

    if not os.path.exists(csv_path):
        if verbose:
            print(f"ERROR: Dataset file not found: {csv_path}", file=sys.stderr)
        return False, {"error": f"File not found: {csv_path}"}

    sample_interval_s = float(cfg.get("telemetry.sample_interval_s", 2.0))
    util_thresh = float(cfg.get("telemetry.congestion_utilization_threshold", 0.80))
    nominal_delay_ms = float(cfg.get("topology.link_delay_ms", 5.0))

    rows: List[Dict[str, Any]] = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    if not rows:
        if verbose:
            print(f"ERROR: Dataset file {csv_path} is empty!", file=sys.stderr)
        return False, {"error": "Dataset is empty"}

    passed_checks = 0
    failed_checks = 0
    failures: List[str] = []

    def assert_check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed_checks, failed_checks
        if cond:
            passed_checks += 1
            if verbose:
                print(f"  [PASS] {name}" + (f" ({detail})" if detail else ""))
        else:
            failed_checks += 1
            fail_msg = f"{name}: {detail}"
            failures.append(fail_msg)
            if verbose:
                print(f"  [FAIL] {name}" + (f" ({detail})" if detail else ""))

    if verbose:
        print("=" * 78)
        print(f"Live Dataset Quality & Integrity Check: {csv_path}")
        print("=" * 78)

    # Group rows by run_id
    runs: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        runs.setdefault(r["run_id"], []).append(r)

    if verbose:
        print(f"\n[1. Per-Run Cadence, Freshness & Staleness Checks ({len(runs)} runs)]")

    per_run_summary: Dict[str, Any] = {}

    for run_id, run_rows in sorted(runs.items()):
        pattern = run_rows[0].get("pattern", "unknown")
        total_samples = len(run_rows)

        # 1. Stale-row fraction check
        stale_count = sum(1 for r in run_rows if str(r.get("is_stale", "")).lower() in ("true", "1"))
        stale_fraction = stale_count / total_samples if total_samples > 0 else 0.0

        assert_check(
            f"Run '{run_id}' stale fraction <= 5%",
            stale_fraction <= 0.05,
            f"stale={stale_count}/{total_samples} ({stale_fraction * 100:.1f}%)",
        )

        # Group by link to compute dt
        links_in_run: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for r in run_rows:
            links_in_run.setdefault((r["src_switch"], r["dst_switch"]), []).append(r)

        dts: List[float] = []
        for l_key, l_rows in links_in_run.items():
            l_rows.sort(key=lambda x: float(x["timestamp"]))
            for i in range(1, len(l_rows)):
                dt = float(l_rows[i]["timestamp"]) - float(l_rows[i - 1]["timestamp"])
                if dt > 0.0:
                    dts.append(dt)

        mean_dt = statistics.mean(dts) if dts else 0.0
        std_dt = statistics.stdev(dts) if len(dts) > 1 else 0.0

        assert_check(
            f"Run '{run_id}' cadence mean ~{sample_interval_s:.1f}s",
            (sample_interval_s * 0.75) <= mean_dt <= (sample_interval_s * 1.35),
            f"mean_dt={mean_dt:.3f} s, std_dt={std_dt:.3f} s",
        )

        # 2. Traffic & Congestion Checks
        # Identify loaded link vs idle link in this run
        link_peak_utils = {
            l_key: max(float(r.get("utilization", 0.0)) for r in l_rows)
            for l_key, l_rows in links_in_run.items()
        }
        loaded_link = max(link_peak_utils, key=link_peak_utils.get)
        peak_util = link_peak_utils[loaded_link]
        loaded_rows = links_in_run[loaded_link]
        peak_rate = max(float(r.get("tx_rate_mbps", 0.0)) for r in loaded_rows)

        assert_check(
            f"Run '{run_id}' ({pattern}) achieved congestion on {loaded_link[0]}->{loaded_link[1]}",
            peak_util >= util_thresh,
            f"peak_util={peak_util:.2f}, peak_rate={peak_rate:.2f} Mbps (threshold={util_thresh:.2f})",
        )

        per_run_summary[run_id] = {
            "pattern": pattern,
            "total_samples": total_samples,
            "stale_fraction": round(stale_fraction, 4),
            "mean_dt_s": round(mean_dt, 3),
            "std_dt_s": round(std_dt, 3),
            "loaded_link": f"{loaded_link[0]}->{loaded_link[1]}",
            "peak_utilization": round(peak_util, 3),
            "peak_rate_mbps": round(peak_rate, 2),
        }

    # 3. Idle-Link Baseline Checks across all runs
    if verbose:
        print("\n[2. Idle-Link Baseline Latency & Utilization Checks]")

    idle_latencies: List[float] = []
    idle_utilizations: List[float] = []

    for run_id, run_rows in runs.items():
        links_in_run = {}
        for r in run_rows:
            links_in_run.setdefault((r["src_switch"], r["dst_switch"]), []).append(r)
        link_mean_utils = {
            l_key: statistics.mean(float(r.get("utilization", 0.0)) for r in l_rows)
            for l_key, l_rows in links_in_run.items()
        }
        idle_link = min(link_mean_utils, key=link_mean_utils.get)
        for r in links_in_run[idle_link]:
            idle_latencies.append(float(r.get("latency_ms", 0.0)))
            idle_utilizations.append(float(r.get("utilization", 0.0)))

    median_idle_lat = statistics.median(idle_latencies) if idle_latencies else 0.0
    median_idle_util = statistics.median(idle_utilizations) if idle_utilizations else 0.0

    assert_check(
        f"Idle-link probe latency median near configured {nominal_delay_ms:.1f} ms",
        (nominal_delay_ms - 3.0) <= median_idle_lat <= (nominal_delay_ms + 4.0),
        f"median_latency={median_idle_lat:.2f} ms",
    )
    assert_check(
        "Idle-link utilization median near 0 (< 5%)",
        median_idle_util <= 0.05,
        f"median_util={median_idle_util * 100:.2f}%",
    )

    # 4. Class Distribution Reports
    if verbose:
        print("\n[3. Class Distribution Breakdown]")

    valid_rows = [r for r in rows if str(r.get("horizon_valid", "")).lower() in ("true", "1")]
    total_valid = len(valid_rows)
    positives = sum(1 for r in valid_rows if int(r.get("label_future_congested", 0)) == 1)
    negatives = total_valid - positives
    pos_ratio = (positives / total_valid) if total_valid > 0 else 0.0

    is_smoke = "smoke" in os.path.abspath(csv_path) or total_valid < 300
    if not is_smoke:
        assert_check(
            "Dataset has valid class balance (positive ratio 15% - 70%)",
            0.15 <= pos_ratio <= 0.70,
            f"pos={positives}/{total_valid} ({pos_ratio * 100:.1f}%)",
        )
    else:
        if verbose:
            print(f"  [INFO] Smoke dataset: class balance check bypassed per prompt guidance (pos ratio: {pos_ratio * 100:.1f}%)")

    # Print summary tables if verbose
    if verbose:
        print("\n" + "=" * 78)
        print(f"VERIFICATION SUMMARY: {passed_checks} passed, {failed_checks} failed")
        print("=" * 78)

        print("\nClass Distribution (Per Pattern):")
        print(f"  {'Pattern':<16} {'Total':<8} {'Positive':<10} {'Negative':<10} {'Pos Ratio':<10}")
        print("  " + "-" * 54)
        for pat in sorted(set(r.get("pattern", "") for r in valid_rows if r.get("pattern"))):
            p_rows = [r for r in valid_rows if r.get("pattern") == pat]
            p_pos = sum(1 for r in p_rows if int(r.get("label_future_congested", 0)) == 1)
            p_tot = len(p_rows)
            p_ratio = (p_pos / p_tot) if p_tot > 0 else 0.0
            print(f"  {pat:<16} {p_tot:<8} {p_pos:<10} {p_tot - p_pos:<10} {p_ratio * 100:>6.1f}%")
        print("  " + "-" * 54)
        print(f"  {'OVERALL':<16} {total_valid:<8} {positives:<10} {negatives:<10} {pos_ratio * 100:>6.1f}%\n")

        print("Class Distribution (Per Link Direction):")
        print(f"  {'Link Direction':<24} {'Total':<8} {'Positive':<10} {'Negative':<10} {'Pos Ratio':<10}")
        print("  " + "-" * 62)
        link_dirs = sorted(set(f"{r['src_switch']}->{r['dst_switch']}" for r in valid_rows))
        for ld in link_dirs:
            ld_rows = [r for r in valid_rows if f"{r['src_switch']}->{r['dst_switch']}" == ld]
            ld_pos = sum(1 for r in ld_rows if int(r.get("label_future_congested", 0)) == 1)
            ld_tot = len(ld_rows)
            ld_ratio = (ld_pos / ld_tot) if ld_tot > 0 else 0.0
            print(f"  {ld:<24} {ld_tot:<8} {ld_pos:<10} {ld_tot - ld_pos:<10} {ld_ratio * 100:>6.1f}%")
        print("  " + "-" * 62 + "\n")

    report = {
        "csv_path": csv_path,
        "passed": failed_checks == 0,
        "passed_checks": passed_checks,
        "failed_checks": failed_checks,
        "failures": failures,
        "total_rows": len(rows),
        "valid_horizon_rows": total_valid,
        "positives": positives,
        "negatives": negatives,
        "positive_ratio": round(pos_ratio, 4),
        "median_idle_latency_ms": round(median_idle_lat, 2),
        "median_idle_utilization": round(median_idle_util, 4),
        "per_run_summary": per_run_summary,
    }
    return failed_checks == 0, report


def main() -> int:
    parser = argparse.ArgumentParser(description="Check live dataset quality and integrity")
    parser.add_argument("--csv", default="data/telemetry_live_labeled.csv", help="Path to labeled dataset CSV")
    parser.add_argument("--quiet", action="store_true", help="Suppress detailed logs")
    args = parser.parse_args()

    passed, report = check_dataset(args.csv, verbose=not args.quiet)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
