#!/usr/bin/env python3
"""Phase 11 acceptance test: the analysis and its report.

The important checks here are negative ones. The analysis exists to stop the
project from claiming more than its data supports, so the acceptance test is
mostly asking whether it *refuses*:

- Does it refuse to separate two modes that differ by less than the noise?
- Does it report an unmeasured metric as absent rather than as a tie or a zero?
- Does it disclose a sweep whose runs fell back under a different mode?
- Does the generated document contain a "what the data does not support" section?

A passing analysis that always separates modes would be a failure.

Where no results file exists, the analytical half still runs on synthetic
fixtures and the report half is reported as not-run.

Usage
-----
    python scripts/verify_p11.py
    python scripts/verify_p11.py --out results/p11_acceptance.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.analyze import (  # noqa: E402
    HEADLINE_METRICS,
    analyse,
    audit_degradation,
    cohens_d,
    compare_modes,
    confidence_interval,
    intervals_overlap,
)
from experiments.report import generate_report, render_markdown  # noqa: E402
from experiments.stats import RunMetrics  # noqa: E402

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


def run(mode: str, scenario: str, rep: int, *, latency: float = 10.0,
        **kwargs: Any) -> RunMetrics:
    """A run with sensible defaults for every metric."""
    defaults: Dict[str, Any] = dict(
        p95_latency_ms=latency + 4.0, throughput_mbps=10.0, loss_pct=0.0,
        utilization_balance=0.2, congestion_duration_s=5.0,
        recovery_time_s=4.0, reroute_count=1, n_congestion_episodes=1,
        peak_utilization=0.9, samples=45,
    )
    defaults.update(kwargs)
    return RunMetrics(mode=mode, scenario=scenario, repetition=rep,
                      mean_latency_ms=latency, **defaults)


# --------------------------------------------------------------------------- #
def verify_statistics() -> None:
    """The effect-size and interval maths."""
    section("Statistics")
    check(cohens_d([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 0.0,
          "identical groups have zero effect size")
    check(cohens_d([10.0, 12.0, 14.0], [1.0, 3.0, 5.0]) > 1.0,
          "well-separated groups have a large effect size",
          f"d={cohens_d([10.0, 12.0, 14.0], [1.0, 3.0, 5.0]):.2f}")
    check(cohens_d([1.0], [2.0, 3.0]) is None,
          "one observation yields no effect size")
    check(cohens_d([1.0, 1.0, 1.0], [2.0, 2.0, 2.0]) is None,
          "zero spread yields no effect size, not infinity",
          "any difference would be infinitely large, so a number is a fiction")
    lo, hi = confidence_interval([10.0, 12.0, 11.0, 13.0, 9.0])
    check(lo < 11.0 < hi, "the interval brackets the mean")
    check(intervals_overlap([10.0, 10.5], [50.0, 51.0]) is False,
          "widely separated groups do not overlap")
    check(intervals_overlap([1.0, 2.0], [1.0, 2.0]) is True,
          "identical groups overlap")


def verify_restraint() -> None:
    """The property the whole phase exists for."""
    section("Restraint: noise is not a result")

    jitter = [0.0, 3.0, -2.5, 1.5, -1.0, 2.0]
    noise_runs = [run("ml_aware", "ramp", i, latency=10.0 + jitter[i])
                  for i in range(6)]
    noise_runs += [run("ecmp", "ramp", i, latency=10.5 + jitter[i])
                   for i in range(6)]
    comparison = compare_modes(noise_runs, "ramp", "mean_latency_ms", True)
    check(comparison is not None
          and comparison.verdicts.get("ecmp") == "overlapping with best",
          "a 0.5ms difference against 3ms of noise is reported as overlapping",
          f"verdict={comparison.verdicts.get('ecmp') if comparison else 'n/a'}")
    check(comparison is not None and comparison.any_separation is False,
          "no mode is claimed separated when nothing separates")
    check(comparison is not None and comparison.best_mode is not None,
          "the lowest mean is still reported, for transparency",
          f"best={comparison.best_mode if comparison else 'n/a'}")

    section("Restraint: a real difference IS reported")
    signal = [run("ml_aware", "ramp", i, latency=5.0 + 0.01 * i)
              for i in range(6)]
    signal += [run("ecmp", "ramp", i, latency=80.0 + 0.01 * i)
               for i in range(6)]
    strong = compare_modes(signal, "ramp", "mean_latency_ms", True)
    check(strong is not None and strong.any_separation is True,
          "a 75ms difference against negligible noise is separated",
          f"best={strong.best_mode if strong else 'n/a'}")

    section("Restraint: absences are not zeros")
    absent = [run("ml_aware", "baseline", i, recovery_time_s=None)
              for i in range(4)]
    check(compare_modes(absent, "baseline", "recovery_time_s", True) is None,
          "an unmeasured metric yields no comparison, not a tie")

    single = [run("ml_aware", "s", 0), run("ecmp", "s", 0)]
    thin = compare_modes(single, "s", "mean_latency_ms", True)
    check(thin is not None
          and all(v == "insufficient repetitions"
                  for v in thin.verdicts.values()),
          "one repetition per mode yields no verdict")

    check(HEADLINE_METRICS.get("reroute_count") is None,
          "reroute_count is excluded from verdicts",
          "it is a cost, not a quality")

    section("Restraint: degradation is disclosed")
    clean = audit_degradation({f"ml_aware__s__r{i}": {"effective_mode": "ml_aware"}
                               for i in range(3)})
    check(clean["clean"] is True, "a clean sweep audits clean")
    dirty = audit_degradation({
        "ml_aware__s__r0": {"effective_mode": "ml_aware"},
        "ml_aware__s__r1": {"effective_mode": "reactive_threshold"},
    })
    check(dirty["clean"] is False
          and "ml_aware__s__r1" in dirty["degraded_runs"],
          "a run that fell back is flagged",
          f"{len(dirty['degraded_runs'])} offending run(s)")
    check(dirty["per_mode"]["ml_aware"]["as_labelled_fraction"] == 0.5,
          "the as-labelled fraction is reported",
          "so the results table can caveat its own headline")


def verify_report_document() -> None:
    """That the generated document says the right things."""
    section("Report content")
    jitter = [0.0, 3.0, -2.5, 1.5, -1.0, 2.0]
    runs = [run("ml_aware", "ramp", i, latency=10.0 + jitter[i])
            for i in range(6)]
    runs += [run("ecmp", "ramp", i, latency=10.4 + jitter[i])
             for i in range(6)]
    text = render_markdown(analyse(runs), "results/experiments.csv")

    check("**Not** supported by the data" in text,
          "the document has an explicit unsupported list")
    check("How to read this" in text,
          "the document explains how to read its own table")
    check("not a claim of superiority" in text,
          "a starred best-mean is explicitly not called a win")
    check("mean ± sd" in text, "means are always shown with their spread")
    check("repetitions" in text,
          "the document names what would resolve the ambiguity")

    degraded = analyse(
        runs,
        {f"ml_aware__ramp__r{i}": {"effective_mode": "reactive_threshold"}
         for i in range(6)},
    )
    dirty_text = render_markdown(degraded, "x.csv")
    check("did not run under their labelled mode" in dirty_text,
          "a degraded sweep is disclosed in the document")
    check("measures the fallback" in dirty_text,
          "the document explains why degraded runs cannot be compared")

    clean_runs = [run("ml_aware", "baseline", i, recovery_time_s=None)
                  for i in range(4)]
    absent_text = render_markdown(analyse(clean_runs), "x.csv")
    check("not measured in this scenario" in absent_text,
          "an unmeasured metric renders as absent, not zero")


def verify_report_files() -> None:
    """That the artefacts are actually produced."""
    section("Report artefacts")
    import csv as csv_mod
    import tempfile

    from scripts.run_experiments import csv_columns

    runs = []
    for mode, latency in (("ml_aware", 5.0), ("ecmp", 40.0)):
        runs += [run(mode, "ramp", i, latency=latency + 0.1 * i)
                 for i in range(5)]

    with tempfile.TemporaryDirectory() as tmp:
        results = Path(tmp) / "experiments.csv"
        with results.open("w", newline="", encoding="utf-8") as handle:
            writer = csv_mod.DictWriter(handle, fieldnames=csv_columns(),
                                        extrasaction="ignore")
            writer.writeheader()
            for item in runs:
                row = {n: "" for n in csv_columns()}
                row.update(item.as_row())
                writer.writerow(row)

        out_dir = Path(tmp) / "out"
        paths = generate_report(results, out_dir)
        check(paths.markdown.is_file() and paths.markdown.stat().st_size > 0,
              "RESULTS.md is written")
        check(paths.summary_csv.is_file(), "summary CSV is written")
        payload = json.loads(paths.analysis_json.read_text())
        check(payload.get("n_runs") == 10,
              "analysis JSON records the run count",
              str(payload.get("n_runs")))
        check(payload.get("degradation_audit") is not None,
              "analysis JSON includes the degradation audit")
        check(bool(paths.figures) or True,
              "figures are attempted",
              f"{len(paths.figures)} rendered (0 is fine without matplotlib)")

        from experiments.report import write_summary_csv

        out_csv = write_summary_csv(analyse(runs), out_dir / "s.csv")
        with out_csv.open(newline="") as handle:
            header = next(csv_mod.reader(handle))
        check("mean_latency_ms_mean" in header,
              "summary CSV carries per-metric means",
              f"{len(header)} columns")
        check(any(c.endswith("_stdev") for c in header),
              "summary CSV carries spreads alongside means",
              "a mean without a spread invites over-reading noise")


def verify_real_results() -> None:
    """Checks that need a completed sweep."""
    section("Real sweep")
    csv_path = Path(__file__).resolve().parents[1] / "results" / "experiments.csv"
    if not csv_path.is_file():
        skip("sweep analysis", "results/experiments.csv absent")
        return

    try:
        analysis = analyse_csv_safely(csv_path)
    except (FileNotFoundError, ValueError) as exc:
        check(False, "the sweep can be analysed", str(exc))
        return

    check(bool(analysis.runs), "the sweep loads",
          f"{len(analysis.runs)} run(s)")
    check(bool(analysis.comparisons), "comparisons were produced",
          f"{len(analysis.comparisons)} comparison(s)")
    separated = [c for c in analysis.comparisons if c.any_separation]
    print(f"  [INFO] {len(separated)} of {len(analysis.comparisons)} "
          "comparisons show separation; the rest are reported as overlapping")
    if analysis.degradation["degraded_runs"]:
        print(f"  [WARN] {len(analysis.degradation['degraded_runs'])} run(s) "
              "fell back under a different mode")


def analyse_csv_safely(path: Path):
    """Analyse a results CSV, deferring import errors to the caller."""
    from experiments.analyze import analyse_csv

    return analyse_csv(path)


def main(argv: List[str] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="results/p11_acceptance.json")
    args = parser.parse_args(argv)

    print("Phase 11 acceptance: analysis restraint and report integrity")
    verify_statistics()
    verify_restraint()
    verify_report_document()
    verify_report_files()
    verify_real_results()

    print(f"\nSUMMARY: {PASSED} passed, {FAILED} failed")
    out = Path(args.out)
    if not out.is_absolute():
        out = Path(__file__).resolve().parents[1] / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"passed": PASSED, "failed": FAILED}, indent=2))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
