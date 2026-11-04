#!/usr/bin/env python3
"""CLI utility to generate physics-informed synthetic telemetry datasets.

Usage:
    python scripts/generate_synthetic_dataset.py
    python scripts/generate_synthetic_dataset.py --output data/telemetry_synthetic.csv --seeds 5
    python scripts/generate_synthetic_dataset.py --augment --output data/telemetry_augmented.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.config import load_config
from ml.synthetic import DEFAULT_PATTERNS, SYNTHETIC_FIELDNAMES, SyntheticTelemetryGenerator


def augment_with_live_dataset(
    synthetic_csv: str,
    live_csv: str = "data/telemetry_live_labeled.csv",
    output_csv: str = "data/telemetry_augmented.csv",
) -> int:
    """Merge synthetic data with real live dataset for hybrid training."""
    if not os.path.exists(live_csv):
        print(f"Warning: Live dataset {live_csv} not found, using synthetic data only.")
        return 0

    merged_count = 0
    os.makedirs(os.path.dirname(os.path.abspath(output_csv)), exist_ok=True)

    with open(output_csv, "w", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=SYNTHETIC_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()

        # 1. Read live records
        with open(live_csv, "r", newline="", encoding="utf-8") as in_f:
            for row in csv.DictReader(in_f):
                writer.writerow(row)
                merged_count += 1
        live_count = merged_count

        # 2. Read synthetic records
        with open(synthetic_csv, "r", newline="", encoding="utf-8") as in_f:
            for row in csv.DictReader(in_f):
                writer.writerow(row)
                merged_count += 1
        synthetic_count = merged_count - live_count

    print(f"Augmented dataset created: {output_csv}")
    print(f"  Live records:      {live_count}")
    print(f"  Synthetic records: {synthetic_count}")
    print(f"  Total records:     {merged_count}")
    return merged_count


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic telemetry datasets for SDN congestion prediction.")
    parser.add_argument(
        "--output",
        default="data/telemetry_synthetic.csv",
        help="Target output CSV path (default: data/telemetry_synthetic.csv)",
    )
    parser.add_argument(
        "--seeds",
        type=int,
        default=5,
        help="Number of seeded runs per pattern (default: 5)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=90.0,
        help="Duration of each episode in seconds (default: 90.0)",
    )
    parser.add_argument(
        "--patterns",
        nargs="+",
        default=DEFAULT_PATTERNS,
        help=f"Patterns to generate (default: {' '.join(DEFAULT_PATTERNS)})",
    )
    parser.add_argument(
        "--augment",
        action="store_true",
        help="Merge synthetic data with real live dataset into data/telemetry_augmented.csv",
    )
    args = parser.parse_args()

    cfg = load_config()
    generator = SyntheticTelemetryGenerator(config=cfg)

    print("========================================================")
    print(" Synthetic SDN Telemetry Generator")
    print("========================================================")
    print(f"Patterns:         {', '.join(args.patterns)}")
    print(f"Seeds/pattern:    {args.seeds}")
    print(f"Episode duration: {args.duration} s")
    print(f"Output path:      {args.output}\n")

    summary = generator.generate_dataset(
        output_csv_path=args.output,
        patterns=args.patterns,
        seeds_per_pattern=args.seeds,
        duration_s=args.duration,
    )

    print("Generation complete:")
    print(f"  Total rows:              {summary['total_rows']}")
    print(f"  Horizon-valid rows:      {summary['valid_rows']}")
    print(f"  Future congested rows:   {summary['future_congested_rows']} ({summary['future_congested_pct']}%)")
    print(f"  Output CSV:              {summary['output_csv']}")

    if args.augment:
        print("\nMerging with live dataset...")
        aug_path = "data/telemetry_augmented.csv" if args.output != "data/telemetry_augmented.csv" else "data/telemetry_augmented_combined.csv"
        augment_with_live_dataset(synthetic_csv=args.output, output_csv=aug_path)

    print("========================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
