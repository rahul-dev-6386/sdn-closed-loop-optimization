#!/usr/bin/env python3
"""Generate the P11 results report from experiments/experiments.csv.

Thin CLI over :mod:`experiments.report` so `make figures` stays a one-liner and
the logic stays importable and testable.

Usage
-----
    python scripts/make_report.py
    python scripts/make_report.py --results results/experiments.csv --out results
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.report import generate_report  # noqa: E402


def main(argv=None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", default="results/experiments.csv")
    parser.add_argument("--out", default="results")
    args = parser.parse_args(argv)

    try:
        paths = generate_report(Path(args.results), Path(args.out))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"report:   {paths.markdown}")
    print(f"summary:  {paths.summary_csv}")
    print(f"analysis: {paths.analysis_json}")
    for figure in paths.figures:
        print(f"figure:   {figure}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
