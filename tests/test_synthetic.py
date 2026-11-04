"""Unit tests for physics-informed synthetic telemetry generator."""

from __future__ import annotations

import csv
import os
from pathlib import Path
import pytest

from common.config import load_config
from ml.features import FeatureBuilder
from ml.synthetic import (
    DEFAULT_PATTERNS,
    LinkQueueSimulator,
    LinkSpec,
    SYNTHETIC_FIELDNAMES,
    SyntheticTelemetryGenerator,
    generate_offered_rate,
)
from ml.train import load_labelled_csv, build_dataset, train


def test_link_queue_simulator_no_congestion():
    spec = LinkSpec("s1", "s2", capacity_mbps=10.0, base_delay_ms=5.0)
    sim = LinkQueueSimulator(spec)

    # 4 Mbps traffic on 10 Mbps link for 2.0s
    res = sim.step(offered_rate_mbps=4.0, dt_s=2.0)
    assert res["utilization"] == pytest.approx(0.4, abs=0.05)
    assert res["loss_pct"] == 0.0
    assert res["latency_ms"] < 8.0  # minimal queuing delay
    assert res["tx_dropped"] == 0


def test_link_queue_simulator_congestion_overflow():
    spec = LinkSpec("s1", "s2", capacity_mbps=10.0, base_delay_ms=5.0, max_queue_pkts=50)
    sim = LinkQueueSimulator(spec)

    # 18 Mbps traffic on 10 Mbps link for 5 consecutive steps
    for _ in range(5):
        res = sim.step(offered_rate_mbps=18.0, dt_s=2.0)

    # After buffer fills, drops must occur
    assert res["utilization"] == 1.0
    assert res["tx_dropped"] > 0
    assert res["loss_pct"] > 5.0
    assert res["latency_ms"] > 30.0  # queuing delay present


def test_traffic_patterns_generate_valid_rates():
    duration = 90.0
    capacity = 10.0
    for pattern in DEFAULT_PATTERNS:
        rates = [
            generate_offered_rate(pattern, t=t, duration_s=duration, capacity_mbps=capacity, seed=42)
            for t in range(0, 90, 5)
        ]
        assert all(r > 0 for r in rates)
        assert any(r > 8.0 for r in rates)  # all patterns test high load


def test_synthetic_generator_creates_valid_csv(tmp_path: Path):
    cfg = load_config()
    generator = SyntheticTelemetryGenerator(config=cfg)
    out_csv = str(tmp_path / "synthetic_test.csv")

    summary = generator.generate_dataset(
        output_csv_path=out_csv,
        patterns=["ramp", "bursty_onoff", "microburst"],
        seeds_per_pattern=2,
        duration_s=60.0,
    )

    assert os.path.exists(out_csv)
    assert summary["total_rows"] > 0
    assert summary["valid_rows"] > 0
    assert summary["future_congested_rows"] > 0
    # Balanced dataset
    assert 10.0 <= summary["future_congested_pct"] <= 90.0

    # Verify CSV headers match schema
    with open(out_csv, "r") as f:
        reader = csv.reader(f)
        header = next(reader)
        assert header == SYNTHETIC_FIELDNAMES


def test_synthetic_dataset_trains_successfully(tmp_path: Path):
    """Verify that FeatureBuilder and ml.train can train on synthetic dataset."""
    cfg = load_config()
    generator = SyntheticTelemetryGenerator(config=cfg)
    out_csv = str(tmp_path / "train_synth.csv")

    generator.generate_dataset(
        output_csv_path=out_csv,
        patterns=["ramp", "bursty_onoff", "poisson", "step_stress"],
        seeds_per_pattern=2,
        duration_s=60.0,
    )

    # Load dataset
    rows = load_labelled_csv(out_csv)
    assert len(rows) > 0

    builder = FeatureBuilder(
        window_w=4,
        sample_interval_s=2.0,
        horizon_n_s=10.0,
        label_gap_s=2.0,
    )

    dataset = build_dataset(rows, builder=builder)
    assert dataset.x.shape[0] > 0
    assert dataset.x.shape[1] == 28  # 7 features * 4 stats
    assert set(dataset.y).issubset({0, 1})
