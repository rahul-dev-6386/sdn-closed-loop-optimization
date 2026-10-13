"""Tests for P11 report rendering.

The report is the artefact a reader forms their conclusion from, so these tests
check that it *refuses* to over-claim: that an unsupported difference is labelled
as unsupported, and that a degraded sweep is disclosed rather than quietly
averaged in.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.report import (  # noqa: E402
    _sem,
    fmt,
    fmt_pm,
    generate_report,
    render_markdown,
    write_summary_csv,
)
from scripts.run_experiments import csv_columns  # noqa: E402


def make_run(mode: str, scenario: str, rep: int, *, latency: float = 10.0,
             **kwargs: Any):
    from experiments.stats import RunMetrics

    defaults: Dict[str, Any] = dict(
        p95_latency_ms=latency + 4.0, throughput_mbps=10.0, loss_pct=0.0,
        utilization_balance=0.2, congestion_duration_s=5.0,
        recovery_time_s=4.0, reroute_count=1, n_congestion_episodes=1,
        peak_utilization=0.9, samples=45,
    )
    defaults.update(kwargs)
    return RunMetrics(mode=mode, scenario=scenario, repetition=rep,
                      mean_latency_ms=latency, **defaults)


def write_results(path: Path, runs, *, effective_mode: str = "") -> Path:
    columns = csv_columns()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for run in runs:
            row = {n: "" for n in columns}
            row.update(run.as_row())
            row["effective_mode"] = effective_mode
            writer.writerow(row)
    return path


# ------------------------------------------------------------ formatting --
def test_fmt_distinguishes_absent_from_zero() -> None:
    assert fmt(None) == "--"
    assert fmt(0.0) == "0.00"
    assert fmt(None) != fmt(0.0), "absent must not read as zero"


def test_fmt_pm_includes_the_spread() -> None:
    assert fmt_pm(10.0, 2.0) == "10.00 ± 2.00"
    assert fmt_pm(10.0, None) == "10.00"
    assert fmt_pm(None, 2.0) == "--"


def test_sem_of_nothing_is_zero() -> None:
    assert _sem([5.0]) == 0.0


def test_sem_of_identical_values_is_zero() -> None:
    assert _sem([5.0, 5.0, 5.0]) == pytest.approx(0.0)


# --------------------------------------------------------------- markdown --
def test_markdown_names_modes_and_scenarios() -> None:
    from experiments.analyze import analyse

    runs = [make_run(m, "ramp", i, latency=5.0 if m == "ml_aware" else 50.0)
            for m in ("ml_aware", "ecmp") for i in range(6)]
    text = render_markdown(analyse(runs), "results/experiments.csv")
    assert "`ml_aware`" in text and "`ecmp`" in text
    assert "ramp" in text
    assert "12 run(s)" in text   # 2 modes x 6 repetitions


def test_markdown_does_not_claim_an_unsupported_win() -> None:
    """Noise-level differences must be labelled unsupported in the text."""
    from experiments.analyze import analyse

    jitter = [0.0, 3.0, -2.5, 1.5, -1.0, 2.0]
    runs = [make_run("ml_aware", "ramp", i, latency=10.0 + jitter[i])
            for i in range(6)]
    runs += [make_run("ecmp", "ramp", i, latency=10.4 + jitter[i])
             for i in range(6)]
    text = render_markdown(analyse(runs), "x.csv")
    assert "No mode is separated" in text
    assert "**Not** supported by the data" in text


def test_markdown_confirms_a_supported_win() -> None:
    from experiments.analyze import analyse

    runs = [make_run("ml_aware", "ramp", i, latency=5.0 + 0.01 * i)
            for i in range(6)]
    runs += [make_run("ecmp", "ramp", i, latency=80.0 + 0.01 * i)
             for i in range(6)]
    text = render_markdown(analyse(runs), "x.csv")
    assert "is separated from at least one" in text
    assert "separated from best" in text


def test_markdown_discloses_a_degraded_sweep() -> None:
    from experiments.analyze import analyse, audit_degradation

    runs = [make_run("ml_aware", "ramp", i) for i in range(4)]
    context = {f"ml_aware__ramp__r{i}": {"effective_mode": "reactive_threshold"}
               for i in range(4)}
    analysis = analyse(runs, context)
    text = render_markdown(analysis, "x.csv")
    assert "did not run under their labelled mode" in text
    assert "measures the fallback" in text


def test_markdown_handles_an_unmeasured_metric() -> None:
    """No congestion anywhere, so recovery time has no value to show."""
    from experiments.analyze import analyse

    runs = [make_run("ml_aware", "baseline", i, recovery_time_s=None)
            for i in range(4)]
    text = render_markdown(analyse(runs), "x.csv")
    assert "not measured in this scenario" in text
    assert "--" in text


def test_markdown_explains_how_to_read_the_table() -> None:
    from experiments.analyze import analyse

    runs = [make_run("ml_aware", "baseline", i) for i in range(3)]
    text = render_markdown(analyse(runs), "x.csv")
    assert "How to read this" in text
    assert "not a claim of superiority" in text


# ------------------------------------------------------------------ files --
def test_summary_csv_is_written(tmp_path: Path) -> None:
    from experiments.analyze import analyse

    runs = [make_run("ml_aware", "ramp", i) for i in range(3)]
    out = write_summary_csv(analyse(runs), tmp_path / "summary.csv")
    assert out.is_file()
    with out.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["mode"] == "ml_aware"
    assert "mean_latency_ms_mean" in rows[0]


def test_generate_report_writes_everything(tmp_path: Path) -> None:
    runs = []
    for mode, latency in (("ml_aware", 5.0), ("ecmp", 40.0)):
        runs += [make_run(mode, "ramp", i, latency=latency + 0.1 * i)
                 for i in range(5)]
    results = write_results(tmp_path / "experiments.csv", runs)

    paths = generate_report(results, tmp_path / "out")
    assert paths.markdown.is_file()
    assert paths.summary_csv.is_file()
    assert json.loads(paths.analysis_json.read_text())["n_runs"] == 10
    assert "6 run(s)" in paths.markdown.read_text() or \
        "10 run(s)" in paths.markdown.read_text()


def test_generate_report_renders_figures_when_matplotlib_is_available(
    tmp_path: Path,
) -> None:
    pytest.importorskip("matplotlib")
    runs = [make_run("ml_aware", "ramp", i, latency=5.0 + i) for i in range(4)]
    runs += [make_run("ecmp", "ramp", i, latency=40.0 + i) for i in range(4)]
    results = write_results(tmp_path / "experiments.csv", runs)
    paths = generate_report(results, tmp_path / "out")
    assert paths.figures, "expected at least one figure"
    assert all(p.is_file() and p.stat().st_size > 0 for p in paths.figures)


def test_generate_report_on_a_missing_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="make experiments"):
        generate_report(tmp_path / "absent.csv", tmp_path / "out")
