"""Tests for the P10 experiment harness.

Everything tested here is the part that decides *what gets measured and
recorded*, separated from the part that drives Mininet. A harness bug that
silently drops a run, or records a failure as a completed row, would corrupt
the results table without any visible symptom.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.stats import RunMetrics  # noqa: E402
from scripts.run_experiments import (  # noqa: E402
    RUN_CONTEXT_COLUMNS,
    SCENARIOS,
    ExperimentError,
    RunSpec,
    append_result,
    build_run_plan,
    completed_keys,
    csv_columns,
    reduce_run,
)

ALL_MODES = ["static_shortest", "ecmp", "reactive_threshold", "ml_aware"]


# ----------------------------------------------------------------- plan --
def test_plan_covers_every_combination() -> None:
    plan = build_run_plan(ALL_MODES, ["baseline", "ramp_bursty"], 3)
    assert len(plan) == len(ALL_MODES) * 2 * 3
    assert len({s.key for s in plan}) == len(plan), "keys must be unique"


def test_plan_order_groups_modes_within_a_repetition() -> None:
    """A degraded fabric must not leave one mode measured against a stale one."""
    plan = build_run_plan(ALL_MODES, ["baseline"], 2)
    first_rep = [s.mode for s in plan if s.repetition == 0]
    assert first_rep == ALL_MODES
    assert [s.repetition for s in plan] == [0] * 4 + [1] * 4


def test_plan_carries_the_scenario_traffic_profile() -> None:
    plan = build_run_plan(["ml_aware"], ["hot_sink"], 1)
    spec = plan[0]
    assert spec.pattern == SCENARIOS["hot_sink"]["pattern"]
    assert spec.peak_rate_mbps == SCENARIOS["hot_sink"]["peak_rate_mbps"]


def test_baseline_scenario_stays_under_capacity() -> None:
    """The control case must not congest, or it is not a control."""
    assert SCENARIOS["baseline"]["peak_rate_mbps"] < 20.0


def test_plan_rejects_an_unknown_mode() -> None:
    with pytest.raises(ExperimentError, match="unknown mode"):
        build_run_plan(["teleportation"], ["baseline"], 1)


def test_plan_rejects_an_unknown_scenario() -> None:
    with pytest.raises(ExperimentError, match="unknown scenario"):
        build_run_plan(ALL_MODES, ["apocalypse"], 1)


def test_plan_rejects_zero_repetitions() -> None:
    """An empty plan produces an empty results file that looks complete."""
    for bad in (0, -1):
        with pytest.raises(ExperimentError, match="repetitions"):
            build_run_plan(ALL_MODES, ["baseline"], bad)


def test_run_key_is_stable() -> None:
    assert RunSpec("ml_aware", "baseline", 2).key == "ml_aware__baseline__r2"


# ------------------------------------------------------------------ CSV --
def test_csv_header_covers_metrics_and_context() -> None:
    columns = csv_columns()
    assert columns[:len(RunMetrics.COLUMNS)] == list(RunMetrics.COLUMNS)
    for extra in RUN_CONTEXT_COLUMNS:
        assert extra in columns


def test_appending_creates_the_file_with_a_header(tmp_path: Path) -> None:
    out = tmp_path / "experiments.csv"
    append_result(out, RunMetrics("ml_aware", "baseline", 0), {"error": ""})
    assert out.is_file()
    with out.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["mode"] == "ml_aware"


def test_appending_adds_rows_without_repeating_the_header(tmp_path: Path) -> None:
    out = tmp_path / "experiments.csv"
    for rep in range(3):
        append_result(out, RunMetrics("ml_aware", "baseline", rep), {"error": ""})
    text = out.read_text()
    assert text.count("mode,scenario,repetition") == 1
    with out.open(newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 3


def test_completed_keys_ignores_failed_runs(tmp_path: Path) -> None:
    """A run that errored must be retried by --resume, not skipped."""
    out = tmp_path / "experiments.csv"
    append_result(out, RunMetrics("ml_aware", "baseline", 0), {"error": ""})
    append_result(out, RunMetrics("ml_aware", "baseline", 1),
                  {"error": "run produced no metrics"})
    keys = completed_keys(out)
    assert "ml_aware__baseline__r0" in keys
    assert "ml_aware__baseline__r1" not in keys


def test_completed_keys_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert completed_keys(tmp_path / "absent.csv") == set()


# ------------------------------------------------------------ reduction --
def telemetry_rows(n: int = 30, *, congested_from: int = 20,
                   util: float = 0.5) -> List[Dict[str, Any]]:
    """Synthetic telemetry for two directions of one link."""
    rows: List[Dict[str, Any]] = []
    for i in range(n):
        congested = i >= congested_from
        for src, dst in (("s1", "s2"), ("s2", "s1")):
            rows.append({
                "timestamp": f"{1000.0 + i * 2.0:.4f}",
                "src_switch": src, "dst_switch": dst, "direction": "forward",
                "is_congested": str(congested),
                "utilization": "0.95" if congested else f"{util}",
                "latency_ms": "55.0" if congested else "4.0",
                "loss_pct": "3.0" if congested else "0.0",
                "tx_rate_mbps": "95.0" if congested else "50.0",
            })
    return rows


def test_reduce_run_produces_metrics() -> None:
    metrics = reduce_run("ml_aware", "ramp_bursty", 0, telemetry_rows())
    assert metrics is not None
    assert metrics.mode == "ml_aware"
    assert metrics.samples == 60
    assert metrics.n_congestion_episodes >= 1


def test_reduce_run_aggregates_directions_into_one_link() -> None:
    """Per-direction utilisation would halve the apparent load."""
    metrics = reduce_run("ml_aware", "ramp_bursty", 0, telemetry_rows())
    # Two directions of one link collapse to a single undirected link.
    assert metrics.utilization_balance == pytest.approx(0.0, abs=1e-6)


def test_reduce_run_of_a_clean_run_reports_no_congestion() -> None:
    metrics = reduce_run("ecmp", "baseline", 0,
                         telemetry_rows(congested_from=999, util=0.2))
    assert metrics is not None
    assert metrics.n_congestion_episodes == 0
    assert metrics.recovery_time_s is None


def test_reduce_run_returns_none_without_telemetry() -> None:
    """None, not a row of zeros: an empty run is a failure, not a perfect one."""
    assert reduce_run("ml_aware", "baseline", 0, []) is None
    assert reduce_run("ml_aware", "baseline", 0, [{"no_timestamp": "x"}]) is None


def test_reduce_run_tolerates_missing_optional_fields() -> None:
    """A run where loss was never measured should not crash the harness."""
    rows = telemetry_rows()
    for row in rows:
        row.pop("loss_pct", None)
        row["loss_pct"] = ""
    metrics = reduce_run("ml_aware", "baseline", 0, rows)
    assert metrics is not None
    assert metrics.loss_pct == 0.0


def test_reduce_run_tolerates_junk_numeric_cells() -> None:
    rows = telemetry_rows()
    rows[0]["utilization"] = "not-a-number"
    metrics = reduce_run("ml_aware", "baseline", 0, rows)
    assert metrics is not None, "one bad cell must not lose the whole run"


def test_uncongested_flags_parse_both_spellings() -> None:
    for spelling in ("True", "true", "1", "yes"):
        rows = telemetry_rows(n=5, congested_from=999)
        rows[0]["is_congested"] = spelling
        metrics = reduce_run("ml_aware", "baseline", 0, rows)
        assert metrics.n_congestion_episodes == 1, spelling
