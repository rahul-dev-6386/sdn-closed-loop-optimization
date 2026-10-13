"""Report and figure generation for the experiment results (P11).

Produces, from one ``results/experiments.csv``:

* ``results/experiments_summary.csv`` -- per (mode, scenario) mean/spread
* ``results/analysis.json`` -- every comparison and verdict, machine-readable
* ``results/RESULTS.md`` -- the human-readable write-up
* ``results/figures/*.png`` -- latency distributions, congestion duration,
  and the mode comparison per scenario

The write-up's most important job is restraint. It states which differences the
data supports, and says plainly which ones it cannot support. A table of means
with no statement of spread invites the reader to over-read every entry, so the
means are always accompanied by ``± stdev`` and by the verdict, and a mode is
never called the winner where the verdict is "overlapping".
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from experiments.analyze import (
    Analysis,
    Comparison,
    analyse_csv,
    audit_degradation,
)
from experiments.stats import RunMetrics

LOGGER = logging.getLogger("experiments.report")

#: Metrics rendered in the main comparison table, with display labels.
TABLE_METRICS = (
    ("mean_latency_ms", "Latency mean (ms)", "lower"),
    ("p95_latency_ms", "Latency p95 (ms)", "lower"),
    ("throughput_mbps", "Throughput (Mbps)", "higher"),
    ("loss_pct", "Loss (%)", "lower"),
    ("utilization_balance", "Util balance (sd)", "lower"),
    ("congestion_duration_s", "Congestion (s)", "lower"),
    ("recovery_time_s", "Recovery (s)", "lower"),
)


def fmt(value: Optional[float], places: int = 2, suffix: str = "") -> str:
    """Format an optional number, with an explicit dash for "not measured"."""
    if value is None:
        return "--"
    return f"{value:.{places}f}{suffix}"


def fmt_pm(mean_value: Optional[float], stdev: Optional[float],
           places: int = 2) -> str:
    """Format ``mean ± stdev``, or a dash when the metric was never measured."""
    if mean_value is None:
        return "--"
    if stdev is None:
        return f"{mean_value:.{places}f}"
    return f"{mean_value:.{places}f} ± {stdev:.{places}f}"


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def write_summary_csv(analysis: Analysis, out_path: Path) -> Path:
    """Write the per-(mode, scenario) summary as a flat CSV."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not analysis.summary:
        out_path.write_text("")
        return out_path

    metric_columns = [
        column
        for column in next(iter(analysis.summary.values())).keys()
        if column.endswith(("_mean", "_stdev"))
    ]
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["mode", "scenario", "n_runs", "recovery_rate",
                         *metric_columns])
        for key, entry in analysis.summary.items():
            writer.writerow([
                entry["mode"], entry["scenario"], entry["n_runs"],
                entry["recovery_rate"],
                *[entry.get(column) for column in metric_columns],
            ])
    return out_path


def write_analysis_json(analysis: Analysis, out_path: Path) -> Path:
    """Write the full analysis as JSON."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(analysis.as_dict(), indent=2, default=str))
    return out_path


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def _comparison_table(analysis: Analysis, scenario: str, metric: str,
                      label: str, places: int = 2) -> List[str]:
    """One metric, one scenario, as a markdown table with a verdict column."""
    comparison = analysis.headline(scenario, metric)
    lines = [
        f"**{label}** "
        f"({'lower' if comparison and comparison.lower_is_better else 'higher'}"
        f" is better)",
        "",
        "| Mode | mean ± sd | n | Verdict |",
        "|---|---|---|---|",
    ]
    if comparison is None:
        lines.append("| _not measured in this scenario_ | -- | -- | -- |")
        return lines
    for mode in sorted(comparison.groups):
        group = comparison.groups[mode]
        verdict = comparison.verdicts.get(mode, "")
        star = " **\***" if mode == comparison.best_mode else ""
        lines.append(
            f"| `{mode}`{star} | {fmt_pm(group['mean'], group['stdev'], places)} "
            f"| {group['n']} | {verdict} |"
        )
    lines.append("")
    if comparison.any_separation:
        lines.append(
            f"`{comparison.best_mode}` is separated from at least one other mode "
            "on this metric (non-overlapping 95% intervals)."
        )
    else:
        lines.append(
            "**No mode is separated from the best on this metric.** The ranking "
            "above is not supported by the data at this repetition count; it is "
            "shown only so the raw numbers are visible."
        )
    lines.append("")
    return lines


def render_markdown(analysis: Analysis, results_csv: str) -> str:
    """Render the full results write-up."""
    out: List[str] = [
        "# Experiment results",
        "",
        f"Generated from `{results_csv}` with {len(analysis.runs)} run(s) across "
        f"{len(analysis.modes)} mode(s) and {len(analysis.scenarios)} scenario(s).",
        "",
        "Modes: " + ", ".join(f"`{m}`" for m in analysis.modes) + ".",
        "",
        "## How to read this",
        "",
        "Each cell is `mean ± population sd` across repetitions. A mode marked",
        "*\\** is the lowest (or highest) mean for that metric, and that alone is",
        "not a claim of superiority -- read the verdict column. `separated from",
        "best` means the 95% intervals do not overlap; `overlapping with best`",
        "means the data does not distinguish them.",
        "",
    ]

    # ---------------------------------------------------- degradation audit --
    audit = analysis.degradation
    out += ["## Did each run test what it claims?", ""]
    if audit["clean"]:
        out += ["Every run executed under the mode named in its row.", ""]
    else:
        out += [
            f"**{len(audit['degraded_runs'])} run(s) did not run under their "
            "labelled mode.** Rows affected are excluded from any head-to-head "
            "claim below, because comparing a real mode against a fallback "
            "measures the fallback:",
            "",
            "| Mode | Runs | As labelled | Degraded | Unknown |",
            "|---|---|---|---|---|",
        ]
        for mode, bucket in sorted(audit["per_mode"].items()):
            out.append(
                f"| `{mode}` | {bucket['runs']} | {bucket['as_labelled']} "
                f"| {bucket['degraded']} | {bucket['unknown']} |"
            )
        out.append("")
        out += ["Affected runs: " + ", ".join(
            f"`{k}`" for k in sorted(audit["degraded_runs"])[:20])
            + (" ..." if len(audit["degraded_runs"]) > 20 else ""), ""]

    # ------------------------------------------------------------ per group --
    out += ["## Per-mode summary", ""]
    out += ["| Mode | Scenario | n | Latency mean (ms) | Congestion (s) "
            "| Recovery (s) | Reroutes |",
            "|---|---|---|---|---|---|---|"]
    for key, entry in analysis.summary.items():
        out.append(
            f"| `{entry['mode']}` | {entry['scenario']} | {entry['n_runs']} "
            f"| {fmt_pm(entry.get('mean_latency_ms_mean'), entry.get('mean_latency_ms_stdev'))} "
            f"| {fmt_pm(entry.get('congestion_duration_s_mean'), entry.get('congestion_duration_s_stdev'), 1)} "
            f"| {fmt_pm(entry.get('recovery_time_s_mean'), entry.get('recovery_time_s_stdev'), 1)} "
            f"| {fmt_pm(entry.get('reroute_count_mean'), entry.get('reroute_count_stdev'), 1)} "
            f"|"
        )
    out.append("")

    # ----------------------------------------------------------- per scenario --
    out += ["## Scenario comparison", ""]
    for scenario in analysis.scenarios:
        out += [f"### {scenario}", ""]
        for metric, label, _direction in TABLE_METRICS:
            out += _comparison_table(analysis, scenario, metric, label)

    # ------------------------------------------------------------- verdicts --
    out += ["## What the data supports", ""]
    supported: List[str] = []
    unsupported: List[str] = []
    for scenario in analysis.scenarios:
        for metric, label, _ in TABLE_METRICS:
            comparison = analysis.headline(scenario, metric)
            if comparison is None:
                continue
            if comparison.any_separation:
                supported.append(
                    f"- **{scenario} / {label}**: `{comparison.best_mode}` is "
                    "separated from at least one alternative.")
            else:
                unsupported.append(
                    f"- **{scenario} / {label}**: no separation; "
                    f"`{comparison.best_mode}` has the lowest mean but the data "
                    "does not distinguish it.")
    out += ["Supported by the data:", ""]
    out += supported or ["- Nothing in this sweep is separated at this "
                         "repetition count."]
    out += ["", "**Not** supported by the data:", ""]
    out += unsupported or ["- None."]
    out += ["", "Increasing `experiments.repetitions` is the only way to turn "
                "the second list into the first.", ""]

    return "\n".join(out) + "\n"


def write_markdown(analysis: Analysis, results_csv: str, out_path: Path) -> Path:
    """Write the human-readable results document."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(analysis, results_csv))
    return out_path


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #
def plot_latency_distribution(
    analysis: Analysis, scenario: str, out_path: Path, metric: str = "mean_latency_ms",
) -> Optional[Path]:
    """Per-mode latency distribution for one scenario.

    Points are individual repetitions and the bar is the mean, so the spread is
    visible rather than implied.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - environment dependent
        LOGGER.warning("skipping %s: %s", out_path.name, exc)
        return None

    data = []
    labels = []
    runs_by_mode: Dict[str, List[float]] = {}
    for run in analysis.runs:
        if run.scenario == scenario:
            runs_by_mode.setdefault(run.mode, []).append(
                float(getattr(run, metric)))
    if not runs_by_mode:
        return None
    for mode in sorted(runs_by_mode):
        labels.append(mode)
        data.append(runs_by_mode[mode])

    fig, ax = plt.subplots(figsize=(7, 4.2))
    # `labels` was renamed to `tick_labels` in matplotlib 3.9; this venv has 3.8,
    # so detect rather than pin a version and break on an upgrade.
    tick_kwarg = ({"tick_labels": labels}
                  if tuple(int(p) for p in matplotlib.__version__.split(".")[:2])
                  >= (3, 9) else {"labels": labels})
    ax.boxplot(data, showmeans=True, **tick_kwarg,
               meanprops={"marker": "D", "markerfacecolor": "#c44e52",
                          "markeredgecolor": "#c44e52"})
    ax.set_title(f"{metric} by routing mode -- {scenario}")
    ax.set_ylabel(metric.replace("_", " "))
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def plot_scenario_bars(
    analysis: Analysis, metric: str, out_path: Path,
) -> Optional[Path]:
    """Grouped bars: every mode's mean for one metric across scenarios."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("skipping %s: %s", out_path.name, exc)
        return None

    modes = sorted({r.mode for r in analysis.runs})
    scenarios = analysis.scenarios
    if not modes or not scenarios:
        return None

    by_cell: Dict[tuple, List[float]] = {}
    for run in analysis.runs:
        value = getattr(run, metric, None)
        if value is None:
            continue
        by_cell.setdefault((run.mode, run.scenario), []).append(float(value))
    if not by_cell:
        return None

    positions = np.arange(len(scenarios))
    width = 0.8 / max(1, len(modes))
    fig, ax = plt.subplots(figsize=(max(7, 2.4 * len(scenarios)), 4.2))
    for index, mode in enumerate(modes):
        heights, errors = [], []
        for scenario in scenarios:
            values = by_cell.get((mode, scenario), [])
            if values:
                heights.append(sum(values) / len(values))
                errors.append(_sem(values))
            else:
                heights.append(0.0)
                errors.append(0.0)
        ax.bar(positions + index * width - 0.4 + width / 2, heights, width,
               yerr=errors, capsize=3, label=mode)
    ax.set_xticks(positions)
    ax.set_xticklabels(scenarios)
    ax.set_ylabel(metric.replace("_", " "))
    ax.set_title(f"{metric} by mode and scenario (mean ± SEM)")
    ax.legend(title="routing mode", fontsize=8)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def _sem(values: Sequence[float]) -> float:
    """Standard error of the mean, 0.0 when undefined."""
    if len(values) < 2:
        return 0.0
    mean_value = sum(values) / len(values)
    var = sum((v - mean_value) ** 2 for v in values) / len(values)
    return (var ** 0.5) / (len(values) ** 0.5)


def write_figures(analysis: Analysis, fig_dir: Path) -> List[Path]:
    """Render every figure for the analysis, skipping any that cannot be made."""
    written: List[Path] = []
    for scenario in analysis.scenarios:
        path = plot_latency_distribution(
            analysis, scenario, fig_dir / f"latency_{scenario}.png")
        if path:
            written.append(path)
    for metric in ("mean_latency_ms", "congestion_duration_s", "loss_pct"):
        path = plot_scenario_bars(
            analysis, metric, fig_dir / f"{metric}_by_scenario.png")
        if path:
            written.append(path)
    return written


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
@dataclass
class ReportPaths:
    """Where each artefact was written."""

    markdown: Path
    summary_csv: Path
    analysis_json: Path
    figures: List[Path]


def generate_report(results_csv: Path, out_dir: Path) -> ReportPaths:
    """Analyse a results CSV and write every artefact.

    Raises
    ------
    FileNotFoundError, ValueError
        Propagated from :func:`experiments.analyze.analyse_csv` when there is no
        usable result to report on.
    """
    analysis = analyse_csv(results_csv)
    out_dir.mkdir(parents=True, exist_ok=True)

    markdown = write_markdown(analysis, str(results_csv), out_dir / "RESULTS.md")
    summary = write_summary_csv(analysis, out_dir / "experiments_summary.csv")
    payload = write_analysis_json(analysis, out_dir / "analysis.json")
    figures = write_figures(analysis, out_dir / "figures")

    LOGGER.info("wrote report to %s (%d figures)", out_dir, len(figures))
    return ReportPaths(markdown=markdown, summary_csv=summary,
                       analysis_json=payload, figures=figures)
