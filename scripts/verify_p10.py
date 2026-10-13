#!/usr/bin/env python3
"""Phase 10 acceptance test: the experiment harness and its results.

Verifies two separate things, because they fail differently:

**The harness** (always checkable, no root needed) — that the run plan is
complete and correctly ordered, that results are written with a stable schema,
and that a failed run is recorded rather than silently dropped. These are the
properties that decide what ends up in the results table.

**The sweep** (needs ``results/experiments.csv``) — that every configured mode
and scenario actually ran, with the configured repetition count, and that the
rows carry measurements rather than zeros.

If no results file exists, the harness half still runs and the sweep half is
reported as not-run rather than passed. A harness test that quietly succeeds
because there is nothing to check would be worse than useless.

Usage
-----
    python scripts/verify_p10.py
    python scripts/verify_p10.py --out results/p10_acceptance.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import load_config  # noqa: E402
from controller.reroute_loop import ROUTING_MODES  # noqa: E402
from experiments.stats import RunMetrics  # noqa: E402
from scripts.run_experiments import (  # noqa: E402
    RUN_CONTEXT_COLUMNS,
    SCENARIOS,
    ExperimentError,
    build_run_plan,
    completed_keys,
    csv_columns,
    reduce_run,
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


def skip(label: str, detail: str = "") -> None:
    """Record a check that could not run."""
    print(f"  [SKIP] {label}" + (f" -- {detail}" if detail else ""))


def section(title: str) -> None:
    """Print a section header."""
    print(f"\n[{title}]")


# --------------------------------------------------------------------------- #
def verify_harness(cfg: Any) -> None:
    """Checks that need no fabric and no results file."""
    section("Harness: run plan")
    modes = list(cfg.get("experiments.modes", list(ROUTING_MODES)))
    scenarios = list(cfg.get("experiments.scenarios", sorted(SCENARIOS)))
    repetitions = int(cfg.get("experiments.repetitions", 10))

    check(set(modes) <= set(ROUTING_MODES),
          "every configured mode is a known mode",
          ", ".join(modes))
    check(len(modes) == 4 or len(modes) > 0,
          "at least one mode is configured", f"{len(modes)} mode(s)")

    try:
        plan = build_run_plan(modes, scenarios, repetitions, 90.0)
        check(len(plan) == len(modes) * len(scenarios) * repetitions,
              "plan covers every mode x scenario x repetition",
              f"{len(plan)} runs")
        check(len({s.key for s in plan}) == len(plan),
              "run keys are unique")
        # Plan order is repetition -> scenario -> mode, so within one
        # (repetition, scenario) the modes run consecutively and in order.
        first_scenario = scenarios[0] if scenarios else None
        first_block = [s.mode for s in plan
                       if s.repetition == 0 and s.scenario == first_scenario]
        check(first_block == modes,
              "modes run back-to-back within a (repetition, scenario)",
              "so a mid-sweep degradation leaves comparable rows")
    except ExperimentError as exc:
        check(False, "plan builds without error", str(exc))

    section("Harness: rejections")
    for label, kwargs in (
        ("unknown mode rejected", dict(modes=["teleportation"], scenarios=["baseline"], repetitions=1)),
        ("unknown scenario rejected", dict(modes=["ecmp"], scenarios=["nope"], repetitions=1)),
        ("zero repetitions rejected", dict(modes=["ecmp"], scenarios=["baseline"], repetitions=0)),
    ):
        try:
            build_run_plan(duration_s=90.0, **kwargs)
            check(False, label)
        except ExperimentError:
            check(True, label)

    section("Harness: results schema")
    columns = csv_columns()
    check(columns[:len(RunMetrics.COLUMNS)] == list(RunMetrics.COLUMNS),
          "metric columns come first and match RunMetrics",
          f"{len(RunMetrics.COLUMNS)} metric columns")
    for extra in ("effective_mode", "error"):
        check(extra in columns,
              f"context column '{extra}' is recorded",
              "a degraded sweep must be visible in the results file")

    section("Harness: telemetry reduction")
    rows = []
    for i in range(30):
        congested = i >= 20
        for src, dst in (("s1", "s2"), ("s2", "s1")):
            rows.append({
                "timestamp": f"{1000.0 + i * 2.0:.4f}", "src_switch": src,
                "dst_switch": dst, "direction": "forward",
                "is_congested": str(congested),
                "utilization": "0.95" if congested else "0.50",
                "latency_ms": "55.0" if congested else "4.0",
                "loss_pct": "3.0" if congested else "0.0",
                "tx_rate_mbps": "95.0" if congested else "50.0",
            })
    metrics = reduce_run("ml_aware", "ramp_bursty", 0, rows)
    check(metrics is not None and metrics.n_congestion_episodes >= 1,
          "a congested series yields a congestion episode",
          f"{metrics.n_congestion_episodes if metrics else 0} episode(s)")
    check(metrics is not None and metrics.utilization_balance == 0.0,
          "both directions of one link aggregate to one undirected link",
          "per-direction utilisation would halve the apparent load")
    check(reduce_run("ml_aware", "baseline", 0, []) is None,
          "no telemetry yields None, not a row of zeros",
          "an empty run is a failure, not a perfect one")

    section("Harness: resume semantics")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        from scripts.run_experiments import append_result

        out = Path(tmp) / "e.csv"
        append_result(out, RunMetrics("ml_aware", "baseline", 0), {"error": ""})
        append_result(out, RunMetrics("ml_aware", "baseline", 1),
                      {"error": "fabric refused to start"})
        done = completed_keys(out)
        check("ml_aware__baseline__r0" in done, "a successful run counts as done")
        check("ml_aware__baseline__r1" not in done,
              "a failed run is retried by --resume, not skipped")


def verify_sweep(cfg: Any) -> None:
    """Checks that need a completed sweep."""
    section("Sweep: results")
    csv_path = Path(cfg.get("experiments.output_csv", "results/experiments.csv"))
    if not csv_path.is_file():
        skip("sweep verification", f"{csv_path} absent; run `make experiments`")
        return

    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = [dict(r) for r in csv.DictReader(handle)]
    check(bool(rows), "results file has rows", f"{len(rows)} row(s)")

    failed = [r for r in rows if (r.get("error") or "").strip()]
    check(not failed, "no run recorded an error",
          f"{len(failed)} failed" if failed else "0 failed")

    modes = {r["mode"] for r in rows}
    scenarios = {r["scenario"] for r in rows}
    check(modes == set(cfg.get("experiments.modes", list(ROUTING_MODES))),
          "every configured mode appears", ", ".join(sorted(modes)))
    check(scenarios == set(cfg.get("experiments.scenarios", sorted(SCENARIOS))),
          "every configured scenario appears", ", ".join(sorted(scenarios)))

    counts = Counter(r["mode"] for r in rows)
    expected_reps = int(cfg.get("experiments.repetitions", 10))
    if expected_reps < 10:
        expected_reps = 10  # a short development sweep is acceptable
    check(all(v >= expected_reps for v in counts.values()),
          "every mode reached the configured repetition count",
          ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    section("Sweep: measurements are real")
    # A sweep where every metric is exactly zero did not actually measure
    # anything, whatever its exit code said.
    zero_latency = [r for r in rows
                    if float(r.get("mean_latency_ms") or 0) == 0.0]
    check(not zero_latency,
          "no run recorded zero mean latency",
          f"{len(zero_latency)} suspicious row(s)" if zero_latency else "all measured")

    zero_samples = [r for r in rows if int(float(r.get("samples") or 0)) == 0]
    check(not zero_samples,
          "every run collected telemetry samples",
          f"{len(zero_samples)} row(s) with 0 samples" if zero_samples else "all collected")

    section("Sweep: degradation audit")
    degraded = [r for r in rows
                if (r.get("effective_mode") or "").strip()
                and r["effective_mode"] != r["mode"]]
    if degraded:
        warn = Counter(r["mode"] for r in degraded)
        print(f"  [WARN] {len(degraded)} run(s) ran under a fallback mode: "
              + ", ".join(f"{k}={v}" for k, v in sorted(warn.items())))
        print("         These must be excluded from head-to-head claims; P11 "
              "does this automatically.")
    else:
        unrecorded = [r for r in rows
                      if not (r.get("effective_mode") or "").strip()]
        if unrecorded:
            print(f"  [WARN] {len(unrecorded)} run(s) recorded no effective_mode")
        else:
            print("  [PASS] every run executed under its labelled mode")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="results/p10_acceptance.json")
    args = parser.parse_args(argv)

    cfg = load_config()
    print("Phase 10 acceptance: experiment harness and sweep")
    verify_harness(cfg)
    verify_sweep(cfg)

    print(f"\nSUMMARY: {PASSED} passed, {FAILED} failed")
    out = Path(args.out)
    if not out.is_absolute():
        out = Path(__file__).resolve().parents[1] / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"passed": PASSED, "failed": FAILED}, indent=2))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
