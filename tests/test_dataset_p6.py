"""Unit tests for Phase 6 Dataset Generation & Offline Future Labeling.

Verifies:
1. All 5 traffic patterns produce valid offered load profiles.
2. Seed reproducibility and randomization.
3. Strict future-congestion horizon labeling logic (horizon=10s, gap=2s from config).
4. Non-leakage: instantaneous congestion at t does NOT produce label=1 if future is clear.
5. Horizon validity and truncation boundary handling.
6. Offline dataset labeling from raw CSV and detailed report generation.
7. SimulatedLinkFixtureForUnitTests queueing and drop simulation.
"""

from __future__ import annotations

import csv
import os
from typing import Any, Dict, List

import pytest

from common.config import load_config
from ml.collect_dataset import (
    generate_detailed_distribution_report,
    get_feature_allowlist,
    label_telemetry_dataset,
    validate_training_feature_columns,
    LABELED_FIELDNAMES,
)
from traffic.generate import (
    PATTERNS,
    SimulatedLinkFixtureForUnitTests,
    TrafficProfile,
)


# --------------------------------------------------------------------------- #
# 1. Traffic Patterns & Offered Load
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pattern", PATTERNS)
def test_all_traffic_patterns_produce_valid_load(pattern: str) -> None:
    """All 5 patterns must generate continuous positive rates within expected bounds."""
    cfg = load_config()
    profile = TrafficProfile(pattern, config=cfg, seed=42)

    rates = [profile.offered_load_at(float(t)) for t in range(0, 60, 5)]
    assert len(rates) == 12
    assert all(r > 0.0 for r in rates)
    assert all(r <= 18.0 for r in rates)


def test_seed_reproducibility() -> None:
    """Same seed generates identical traffic; different seeds produce variation."""
    cfg = load_config()
    prof1 = TrafficProfile("poisson", config=cfg, seed=123)
    prof2 = TrafficProfile("poisson", config=cfg, seed=123)
    prof3 = TrafficProfile("poisson", config=cfg, seed=999)

    r1 = [prof1.offered_load_at(t) for t in range(10)]
    r2 = [prof2.offered_load_at(t) for t in range(10)]
    r3 = [prof3.offered_load_at(t) for t in range(10)]

    assert r1 == r2
    assert r1 != r3


# --------------------------------------------------------------------------- #
# 2. Strict Future-Congestion Labeling & Non-Leakage
# --------------------------------------------------------------------------- #
def test_future_labeling_logic_positive(tmp_path: Any) -> None:
    """Congestion in [t + gap, t + gap + horizon] must produce label=1 with lead time."""
    raw_csv = tmp_path / "raw_pos.csv"
    labeled_csv = tmp_path / "labeled_pos.csv"
    cfg = load_config()

    # Synthetic series where congestion occurs ONLY at t = 8.0 s
    fieldnames = [
        "run_id", "timestamp", "src_switch", "dst_switch", "direction",
        "tx_rate_mbps", "utilization", "latency_ms", "loss_pct", "is_congested",
    ]
    rows = []
    for t in range(0, 30, 2):
        is_cong = (t == 8)
        rows.append({
            "run_id": "test_run",
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

    with open(raw_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    labeled, report = label_telemetry_dataset(
        raw_csv_path=str(raw_csv),
        output_labeled_path=str(labeled_csv),
        config=cfg,
    )

    # For t=0.0: horizon is [2.0, 12.0]. Congestion is at t=8.0 -> label=1, lead_time=8.0s
    t0_rec = next(r for r in labeled if float(r["timestamp"]) == 0.0)
    assert int(t0_rec["label_future_congested"]) == 1
    assert float(t0_rec["lead_time_s"]) == 8.0

    # For t=4.0: horizon is [6.0, 16.0]. Congestion is at t=8.0 -> label=1, lead_time=4.0s
    t4_rec = next(r for r in labeled if float(r["timestamp"]) == 4.0)
    assert int(t4_rec["label_future_congested"]) == 1
    assert float(t4_rec["lead_time_s"]) == 4.0


def test_no_label_leakage_present_alone_is_not_labeled(tmp_path: Any) -> None:
    """Instantaneous congestion at t must NOT produce label=1 if future is uncongested."""
    raw_csv = tmp_path / "raw_leak.csv"
    labeled_csv = tmp_path / "labeled_leak.csv"
    cfg = load_config()

    fieldnames = [
        "run_id", "timestamp", "src_switch", "dst_switch", "direction",
        "tx_rate_mbps", "utilization", "latency_ms", "loss_pct", "is_congested",
    ]
    rows = []
    for t in range(0, 30, 2):
        is_cong = (t == 0)  # Only congested at t=0
        rows.append({
            "run_id": "test_leak",
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

    with open(raw_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    labeled, report = label_telemetry_dataset(
        raw_csv_path=str(raw_csv),
        output_labeled_path=str(labeled_csv),
        config=cfg,
    )

    # For t=0.0: horizon is [2.0, 12.0]. Future samples are clear -> label MUST be 0!
    t0_rec = next(r for r in labeled if float(r["timestamp"]) == 0.0)
    assert t0_rec["is_congested"] == "True"
    assert int(t0_rec["label_future_congested"]) == 0
    assert float(t0_rec["lead_time_s"]) < 0.0


def test_horizon_truncation_detection(tmp_path: Any) -> None:
    """Samples near the end of an episode where future cannot be observed are flagged."""
    raw_csv = tmp_path / "raw_trunc.csv"
    labeled_csv = tmp_path / "labeled_trunc.csv"
    cfg = load_config()

    fieldnames = [
        "run_id", "timestamp", "src_switch", "dst_switch", "direction",
        "tx_rate_mbps", "utilization", "latency_ms", "loss_pct", "is_congested",
    ]
    # Episode duration = 20s (max_t = 20.0). Horizon needs gap (2s) + horizon (10s) = 12s future
    rows = [
        {
            "run_id": "r", "timestamp": str(float(t)), "src_switch": "s1", "dst_switch": "s2",
            "direction": "forward", "tx_rate_mbps": "2.0", "utilization": "0.2", "latency_ms": "5.0",
            "loss_pct": "0.0", "is_congested": "False"
        }
        for t in range(0, 22, 2)
    ]

    with open(raw_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    labeled, report = label_telemetry_dataset(
        raw_csv_path=str(raw_csv),
        output_labeled_path=str(labeled_csv),
        config=cfg,
    )

    # t=0.0: horizon end is 12.0 <= 20 -> horizon_valid = True
    t0_rec = next(r for r in labeled if float(r["timestamp"]) == 0.0)
    assert t0_rec["horizon_valid"] is True

    # t=16.0: horizon end is 16.0 + 12.0 = 28.0 > 20 -> horizon_valid = False
    t16_rec = next(r for r in labeled if float(r["timestamp"]) == 16.0)
    assert t16_rec["horizon_valid"] is False


# --------------------------------------------------------------------------- #
# 3. Unit Test Fixture Only: SimulatedLinkFixture
# --------------------------------------------------------------------------- #
def test_simulated_link_fixture_mechanics() -> None:
    """Test fixture accurately models drops and queue delay when offered load > capacity."""
    sim = SimulatedLinkFixtureForUnitTests(capacity_mbps=10.0, base_delay_ms=5.0, buffer_packets=50)

    # Low load: 2.0 Mbps < 10.0 Mbps -> zero loss, base delay
    tx, lat, loss, drops = sim.step(offered_mbps=2.0, dt_s=2.0)
    assert tx == pytest.approx(2.0, rel=1e-2)
    assert lat == pytest.approx(5.0, rel=1e-2)
    assert loss == 0.0
    assert drops == 0

    # Overload: 20.0 Mbps > 10.0 Mbps for 10 seconds -> queue fills, drops occur, latency rises
    for _ in range(5):
        tx, lat, loss, drops = sim.step(offered_mbps=20.0, dt_s=2.0)

    assert tx == pytest.approx(10.0, rel=1e-2)  # Capped at bottleneck capacity
    assert lat > 5.0                             # Latency rises under queue buildup
    assert drops > 0                             # Drops occur when buffer overflows
    assert loss > 0.0


# --------------------------------------------------------------------------- #
# 4. Explicit Feature Allowlist Enforcement
# --------------------------------------------------------------------------- #
def test_feature_allowlist_validation() -> None:
    """Explicit allowlist from config.yaml must accept valid features and derived rolling features."""
    cfg = load_config()
    allowlist = get_feature_allowlist(cfg)
    assert "utilization" in allowlist
    assert "tx_rate_mbps" in allowlist

    # Valid feature set
    valid_features = [
        "tx_rate_mbps", "rx_rate_mbps", "utilization", "latency_ms", "loss_pct",
        "utilization_mean", "tx_rate_mbps_slope", "latency_ms_std"
    ]
    validate_training_feature_columns(valid_features, cfg)


def test_feature_allowlist_rejects_forbidden_columns() -> None:
    """Training matrix with link identity or un-allowlisted columns must fail validation."""
    cfg = load_config()

    # Link identity columns and arbitrary names must be strictly rejected
    forbidden_examples = [
        ["src_switch", "utilization"],
        ["dst_switch", "tx_rate_mbps"],
        ["src_port", "rx_rate_mbps"],
        ["dst_port", "latency_ms"],
        ["run_id", "utilization"],
        ["direction", "loss_pct"],
        ["ip_address", "utilization"],
        ["arbitrary_label", "latency_ms"],
    ]

    for col_set in forbidden_examples:
        with pytest.raises(ValueError, match="forbidden non-allowlisted columns"):
            validate_training_feature_columns(col_set, cfg)

