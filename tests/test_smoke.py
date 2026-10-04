"""Smoke tests for P1: configuration contract and environment invariants.

These are pure-logic tests (no root, no mininet) so they can run anywhere.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
REQUIREMENTS = ROOT / "requirements.txt"


@pytest.fixture(scope="module")
def cfg() -> Dict[str, Any]:
    with CONFIG_PATH.open() as fh:
        return yaml.safe_load(fh)


# --------------------------------------------------------------- config file --
def test_config_file_exists() -> None:
    assert CONFIG_PATH.is_file(), "config.yaml is the single source of tunables"


def test_config_top_level_sections(cfg: Dict[str, Any]) -> None:
    expected = {
        "experiment", "topology", "controller", "discovery", "telemetry",
        "ml", "routing", "traffic", "experiments", "logging",
    }
    assert expected <= set(cfg), f"missing sections: {expected - set(cfg)}"


def test_cost_weights_sum_to_one(cfg: Dict[str, Any]) -> None:
    """Design rule 4: the four cost terms must form a convex combination."""
    w = cfg["routing"]["weights"]
    total = sum(w[k] for k in ("a_utilization", "b_latency", "c_loss", "d_congestion"))
    assert abs(total - 1.0) < 1e-6, f"cost weights sum to {total}, expected 1.0"


def test_improvement_ratio_is_a_real_hysteresis(cfg: Dict[str, Any]) -> None:
    """Rule 5: alt must be cheaper than current by a meaningful margin."""
    r = cfg["routing"]["improvement_ratio"]
    assert 0.0 < r < 1.0, "improvement_ratio must be strictly between 0 and 1"


def test_cooldown_dwell_and_sample_interval_are_positive(cfg: Dict[str, Any]) -> None:
    assert cfg["routing"]["cooldown_s"] > 0
    assert cfg["routing"]["min_dwell_s"] > 0
    assert cfg["telemetry"]["sample_interval_s"] > 0


def test_ml_window_and_horizon_are_consistent(cfg: Dict[str, Any]) -> None:
    """The feature window must be strictly in the past w.r.t. the label horizon."""
    ml = cfg["ml"]
    assert ml["window_w"] >= 2, "need at least 2 samples for a slope feature"
    assert ml["horizon_n_s"] > ml["sample_interval_s"], "horizon must span >1 sample"
    assert ml["label_gap_s"] > 0, "guard band prevents label leakage"
    window_span = ml["window_w"] * ml["sample_interval_s"]
    assert ml["label_gap_s"] >= ml["sample_interval_s"], (
        "the gap must cover at least one sampling interval, otherwise the "
        "boundary sample is shared between the feature window and the horizon"
    )
    assert window_span + ml["label_gap_s"] < ml["horizon_n_s"] + window_span


def test_ml_sample_interval_matches_telemetry(cfg: Dict[str, Any]) -> None:
    """Feature windows are indexed in samples; both sides must agree."""
    assert cfg["ml"]["sample_interval_s"] == cfg["telemetry"]["sample_interval_s"]


def test_routing_mode_is_valid(cfg: Dict[str, Any]) -> None:
    valid = {"static_shortest", "ecmp", "reactive_threshold", "ml_aware"}
    assert cfg["routing"]["mode"] in valid
    assert set(cfg["experiments"]["modes"]) == valid, "all 4 modes must be compared"


def test_congestion_thresholds_are_ordered(cfg: Dict[str, Any]) -> None:
    t = cfg["telemetry"]
    assert 0.0 < t["congestion_utilization_threshold"] < 1.0
    assert t["congestion_loss_threshold_pct"] > 0
    assert t["congestion_latency_threshold_ms"] > 0


def test_peak_traffic_exceeds_link_capacity(cfg: Dict[str, Any]) -> None:
    """A congestion dataset requires offered load > capacity, else no labels."""
    assert cfg["traffic"]["peak_rate_mbps"] > cfg["topology"]["link_bw_mbps"]


# ------------------------------------------------------------- requirements --
def test_requirements_are_fully_pinned() -> None:
    """Reproducibility rule: every dependency needs an exact version."""
    reqs = [
        line.strip() for line in REQUIREMENTS.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert reqs, "requirements.txt is empty"
    unpinned = [r for r in reqs if not re.search(r"==\d", r)]
    assert not unpinned, f"unpinned requirements: {unpinned}"


def test_ryu_and_eventlet_are_pinned_to_the_spec() -> None:
    text = REQUIREMENTS.read_text()
    assert "ryu==4.34" in text
    assert "eventlet==0.30.2" in text


def test_python_39_is_required_by_the_pins() -> None:
    """eventlet 0.30.2 has no wheels for >=3.11, so 3.9 is a hard constraint."""
    import sys

    assert sys.version_info[:2] == (3, 9), (
        f"this project targets CPython 3.9 (eventlet 0.30.2), running {sys.version}"
    )


# ------------------------------------------------------------------ makefile --
def test_makefile_declares_p1_targets() -> None:
    mk = (ROOT / "Makefile").read_text()
    for target in ("setup:", "verify:", "test:", "clean:"):
        assert target in mk, f"Makefile missing target {target}"