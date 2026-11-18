#!/usr/bin/env python3
"""Experiment harness: 4 routing modes x scenarios x repetitions (P10).

Runs the full fabric once per (mode, scenario, repetition), drives a traffic
scenario on it, reduces the resulting telemetry to one row of metrics, and
appends it to ``results/experiments.csv``.

Design notes
------------
**Mininet runs strictly sequentially.** Only ~4 GiB of RAM is free on the target
machine, and two fabrics fighting over ``ovs-vsftpd`` and the OpenFlow ports
produce results that are wrong rather than slow. Runs are therefore serial, and
each one tears the previous fabric down before building the next.

**Each run is isolated.** The telemetry CSV is deleted before every run, so a
run cannot silently inherit the previous run's samples -- which would make the
first few seconds of every run look uncongested by construction.

**The mode is selected through the environment, not by editing config.yaml.**
See :attr:`controller.reroute_loop.LoopConfig.MODE_ENV_VAR`.

**Degradation is recorded per run.** Every run stores the loop's ``mode_report``
alongside its metrics. A row whose ``effective_mode`` differs from its
``mode`` is a run that did not test what its label claims, and P11 surfaces
that rather than hiding it.

Usage
-----
    sudo python scripts/run_experiments.py --dry-run
    sudo python scripts/run_experiments.py --modes ecmp ml_aware --repetitions 3
    sudo python scripts/run_experiments.py --resume
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.config import load_config  # noqa: E402
from common.proc import (  # noqa: E402
    ControllerLog,
    controller_log,
    mn_clean,
    venv_bin,
)
from controller.reroute_loop import LoopConfig, ROUTING_MODES  # noqa: E402
from experiments.stats import (  # noqa: E402
    RunMetrics,
    find_episodes,
    percentile,
    summarise_run,
)

#: Extra columns appended after RunMetrics.COLUMNS for traceability.
RUN_CONTEXT_COLUMNS: Tuple[str, ...] = (
    "effective_mode", "degraded_ticks", "fallbacks", "n_samples_link_runs",
    "error",
)


class ExperimentError(RuntimeError):
    """Raised when a run cannot be completed."""


# --------------------------------------------------------------------------- #
# Run plan
# --------------------------------------------------------------------------- #
@dataclass
class RunSpec:
    """One experiment run: a mode, a scenario and a repetition index."""

    mode: str
    scenario: str
    repetition: int
    #: The traffic pattern this scenario drives, and any extra rate it needs.
    pattern: str = "ramp"
    base_rate_mbps: float = 2.0
    peak_rate_mbps: float = 12.0
    duration_s: float = 90.0

    @property
    def key(self) -> str:
        """Stable identifier used for resume and de-duplication."""
        return f"{self.mode}__{self.scenario}__r{self.repetition}"

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        return {
            "key": self.key, "mode": self.mode, "scenario": self.scenario,
            "repetition": self.repetition, "pattern": self.pattern,
            "duration_s": self.duration_s,
            "base_rate_mbps": self.base_rate_mbps,
            "peak_rate_mbps": self.peak_rate_mbps,
        }


#: Scenario -> traffic pattern. A scenario is a *shape* of offered load, not a
#: different generator, so the mapping stays explicit and reviewable.
SCENARIOS: Dict[str, Dict[str, Any]] = {
    # Steady load well under capacity: the control case. A rerouting mode should
    # cost nothing here, and any latency it adds is pure overhead.
    "baseline": {"pattern": "poisson", "base_rate_mbps": 2.0,
                 "peak_rate_mbps": 4.0},
    # Load that climbs through the congestion threshold, exercising proactive
    # avoidance -- the case the ML forecast is supposed to win.
    "ramp_bursty": {"pattern": "ramp", "base_rate_mbps": 2.0,
                    "peak_rate_mbps": 14.0},
    # A small number of heavy flows aimed at one sink, where load spreading
    # matters more than reacting to a global ramp.
    "hot_sink": {"pattern": "bursty_onoff", "base_rate_mbps": 4.0,
                 "peak_rate_mbps": 18.0},
}


def build_run_plan(
    modes: Sequence[str], scenarios: Sequence[str], repetitions: int,
    duration_s: float = 90.0,
) -> List[RunSpec]:
    """Enumerate every run to perform, in execution order.

    The order is repetitions-outermost so that the four modes for a given
    repetition run back to back. If the fabric degrades partway through a
    sweep, the surviving rows still compare modes under similar conditions
    rather than comparing one mode against a stale machine.

    Raises
    ------
    ExperimentError
        If a mode, a scenario, or the repetition count is invalid -- an empty
        plan would otherwise produce a results file with no rows and look like a
        completed experiment.
    """
    if repetitions < 1:
        raise ExperimentError(f"repetitions must be >= 1, got {repetitions}")
    unknown_modes = [m for m in modes if m not in ROUTING_MODES]
    if unknown_modes:
        raise ExperimentError(
            f"unknown mode(s) {unknown_modes}; expected from {list(ROUTING_MODES)}"
        )
    unknown_scenarios = [s for s in scenarios if s not in SCENARIOS]
    if unknown_scenarios:
        raise ExperimentError(
            f"unknown scenario(s) {unknown_scenarios}; "
            f"expected from {sorted(SCENARIOS)}"
        )

    plan: List[RunSpec] = []
    for repetition in range(repetitions):
        for scenario in scenarios:
            for mode in modes:
                spec = SCENARIOS[scenario]
                plan.append(RunSpec(
                    mode=mode, scenario=scenario, repetition=repetition,
                    pattern=spec["pattern"],
                    base_rate_mbps=spec["base_rate_mbps"],
                    peak_rate_mbps=spec["peak_rate_mbps"],
                    duration_s=duration_s,
                ))
    return plan


def completed_keys(csv_path: Path) -> set:
    """Keys already present in the results CSV, for ``--resume``.

    A row whose ``error`` column is non-empty does not count as completed: the
    run failed, and resuming must retry it rather than record a permanent blank.
    """
    if not csv_path.is_file():
        return set()
    done: set = set()
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if (row.get("error") or "").strip():
                continue
            mode = row.get("mode")
            scenario = row.get("scenario")
            rep = row.get("repetition")
            if not (mode and scenario and rep):
                continue
            done.add(f"{mode}__{scenario}__r{rep}")
    return done


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def csv_columns() -> List[str]:
    """The results CSV header."""
    return list(RunMetrics.COLUMNS) + list(RUN_CONTEXT_COLUMNS)


def append_result(
    csv_path: Path, metrics: RunMetrics, context: Dict[str, Any],
) -> None:
    """Append one run's row, creating the file and header if needed."""
    row = metrics.as_row()
    row.update(context)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not csv_path.is_file() or csv_path.stat().st_size == 0
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_columns(),
                                extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow(row)


# --------------------------------------------------------------------------- #
# Telemetry reduction
# --------------------------------------------------------------------------- #
def load_telemetry_rows(csv_path: Path) -> List[Dict[str, Any]]:
    """Read the telemetry CSV written during a run, or ``[]`` if absent."""
    if not csv_path.is_file():
        return []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def reduce_run(
    mode: str, scenario: str, repetition: int,
    rows: Sequence[Dict[str, Any]],
    reroute_count: int = 0,
) -> Optional[RunMetrics]:
    """Reduce one run's telemetry to a metrics row.

    Telemetry is directional, so it is aggregated per undirected link: reporting
    utilisation separately for each direction would halve the apparent load and
    make the balance metric meaningless.

    Returns
    -------
    RunMetrics or None
        ``None`` when the run produced no usable telemetry, which is a failure
        to record rather than a row of zeros.

    Raises
    ------
    ExperimentError
        If telemetry exists but contains no parseable sample.
    """
    usable = [r for r in rows if r.get("timestamp")]
    if not usable:
        return None

    def _num(row: Dict[str, Any], key: str, default: float = 0.0) -> float:
        try:
            value = float(row.get(key, default))
        except (TypeError, ValueError):
            return default
        return default if value != value else value  # drop NaN

    timestamps = [_num(r, "timestamp") for r in usable]
    congested_flags = [
        str(r.get("is_congested", "")).strip().lower() in {"1", "true", "yes"}
        for r in usable
    ]
    latencies = [_num(r, "latency_ms") for r in usable]
    throughputs = [_num(r, "tx_rate_mbps") for r in usable]
    losses = [_num(r, "loss_pct") for r in usable]

    # Undirected link -> mean utilisation across its recorded samples.
    per_link: Dict[Tuple[str, str], List[float]] = {}
    for row in usable:
        key = tuple(sorted((row.get("src_switch", ""), row.get("dst_switch", ""))))
        per_link.setdefault(key, []).append(_num(row, "utilization"))
    link_utilisation = {
        f"{a}<->{b}": sum(values) / len(values)
        for (a, b), values in per_link.items() if values
    }

    return summarise_run(
        mode=mode, scenario=scenario, repetition=repetition,
        timestamps=timestamps, congested=congested_flags,
        latency_ms=latencies, throughput_mbps=throughputs, loss_pct=losses,
        per_link_utilisation=link_utilisation,
        reroute_count=reroute_count,
    )


def loop_report_context(report: Optional[Dict[str, Any]],
                        configured_mode: str) -> Dict[str, Any]:
    """Distil the controller's loop report into the CSV context columns.

    An absent or unreadable report means the loop's behaviour was never
    observed, so ``effective_mode`` stays empty and P11 will classify the run
    as unverifiable rather than as a mode that held.
    """
    ctx: Dict[str, Any] = {"effective_mode": "", "degraded_ticks": 0,
                           "fallbacks": ""}
    if not report:
        return ctx
    effective = report.get("effective_modes") or {}
    if int(report.get("n_ticks", 0) or 0) and effective:
        ctx["effective_mode"] = max(effective.items(), key=lambda kv: kv[1])[0]
    elif report.get("started") is False and configured_mode == "static_shortest":
        # static_shortest spawns no decision thread by design; the fabric
        # forwards on the baseline rules, which *is* static routing.
        ctx["effective_mode"] = "static_shortest"
    ctx["degraded_ticks"] = int(report.get("degraded_ticks", 0) or 0)
    fallbacks = report.get("fallbacks") or {}
    ctx["fallbacks"] = "; ".join(f"{count}x {reason}"
                                 for reason, count in sorted(fallbacks.items()))
    return ctx


def read_loop_report(path: Path) -> Optional[Dict[str, Any]]:
    """Read the controller's loop-report file, or None if it is absent."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
class ExperimentRunner:
    """Runs one experiment at a time against a real fabric.

    Parameters
    ----------
    config:
        Parsed ``config.yaml``.
    telemetry_csv:
        Where the controller writes during a run. Deleted before each run.
    """

    def __init__(self, config: Any, telemetry_csv: Optional[Path] = None) -> None:
        self.config = config
        self.telemetry_csv = telemetry_csv or Path(
            config.get("telemetry.csv_path", "data/telemetry.csv"))
        self.duration_override: Optional[float] = None

    def run(self, spec: RunSpec) -> Dict[str, Any]:
        """Execute one run and return its metrics plus context.

        Returns
        -------
        dict
            ``{"metrics": RunMetrics | None, "context": {...}}``. A failed run
            returns ``metrics=None`` with a populated ``error``, so the harness
            records the failure and continues rather than aborting the sweep.
        """
        duration = self.duration_override or spec.duration_s
        print(f"\n>>> {spec.key}: mode={spec.mode} scenario={spec.scenario} "
              f"pattern={spec.pattern} {duration:.0f}s", flush=True)

        mn_clean()
        if self.telemetry_csv.exists():
            self.telemetry_csv.unlink()

        report_path = Path("data/logs") / f"loop_report_{spec.key}.json"
        report_path.unlink(missing_ok=True)

        ryu_proc: Optional[subprocess.Popen] = None
        topo = None
        net = None
        context: Dict[str, Any] = {
            "effective_mode": "", "degraded_ticks": 0, "fallbacks": "",
            "n_samples_link_runs": 0, "error": "",
        }

        try:
            ryu_proc = self._start_controller(spec, report_path)
            topo, net = self._start_fabric()

            from traffic.generate import run_live_pattern_on_hosts

            net.pingAll()
            run_live_pattern_on_hosts(
                net=net, pattern=spec.pattern, duration_s=duration,
                seed=spec.repetition * 1000 + abs(hash(spec.mode)) % 997,
                base_rate_mbps=spec.base_rate_mbps,
                peak_rate_mbps=spec.peak_rate_mbps,
            )
            # Let the final telemetry samples land before the fabric dies.
            time.sleep(2.5)

            # What the control loop actually did, from the file it writes per
            # tick -- not from what the run label claims.
            report = read_loop_report(report_path)
            context.update(loop_report_context(report, spec.mode))
            reroute_count = int(report.get("reroutes_planned", 0) or 0) \
                if report else 0

            rows = load_telemetry_rows(self.telemetry_csv)
            metrics = reduce_run(spec.mode, spec.scenario, spec.repetition, rows,
                                 reroute_count=reroute_count)
            context["n_samples_link_runs"] = len(rows)
            if metrics is None:
                context["error"] = "no telemetry produced"
            return {"metrics": metrics, "context": context}

        except Exception as exc:  # noqa: BLE001
            context["error"] = f"{type(exc).__name__}: {exc}"
            print(f"    run failed: {context['error']}", file=sys.stderr)
            return {"metrics": None, "context": context}
        finally:
            self._teardown(topo, net, ryu_proc)

    def _start_controller(self, spec: RunSpec,
                          report_path: Optional[Path] = None) -> subprocess.Popen:
        """Start Ryu with the requested routing mode."""
        from ml.collect_dataset import wait_for_port

        env = os.environ.copy()
        env["PYTHONPATH"] = "."
        env[LoopConfig.MODE_ENV_VAR] = spec.mode
        if report_path is not None:
            env["SDN_LOOP_REPORT"] = str(report_path.resolve())
        # Output goes to a file, not an undrained pipe: a full pipe blocks the
        # controller and silently halts its event loop. See common.proc.
        log_sink = controller_log(f"experiment_{spec.key}")
        log_handle = ControllerLog(log_sink)
        log_fd = log_handle.__enter__()
        print(f"    controller log -> {log_sink}")
        proc = subprocess.Popen(
            [venv_bin("ryu-manager"), "--ofp-listen-host", "127.0.0.1",
             "--ofp-tcp-listen-port", "6653", "controller.sdn_controller"],
            env=env, stdout=log_fd, stderr=log_fd,
            preexec_fn=os.setsid,
        )
        log_handle.__exit__()  # our copy; the child has its own
        if not wait_for_port(6653, timeout_s=10.0):
            raise ExperimentError("Ryu controller failed to listen on 6653")
        return proc

    def _start_fabric(self) -> Tuple[Any, Any]:
        """Build the multipath topology against the running controller."""
        from topology.multipath_topology import MultipathTopology, build_spec

        topo = MultipathTopology(
            build_spec(self.config),
            controller=("127.0.0.1", 6653),
            datapath_type=str(self.config.get("topology.datapath_type", "kernel")),
        )
        net = topo.build()
        time.sleep(3.0)  # let switches handshake and exchange LLDP
        return topo, net

    def _teardown(self, topo: Any, net: Any, ryu_proc: Optional[subprocess.Popen]) -> None:
        """Stop the fabric and controller, never raising.

        Mininet is cleaned up strictly sequentially: a failed teardown that
        leaves a process behind will poison the *next* run, so ``mn -c`` runs
        even when the controller or the topology object already failed.
        """
        for closer in (getattr(topo, "stop", None), getattr(net, "stop", None)):
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        if ryu_proc is not None and ryu_proc.poll() is None:
            try:
                os.killpg(os.getpgid(ryu_proc.pid), signal.SIGTERM)
                ryu_proc.wait(timeout=3.0)
            except Exception:  # noqa: BLE001
                try:
                    os.killpg(os.getpgid(ryu_proc.pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    pass
        mn_clean()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--modes", nargs="*", default=None,
                        help=f"routing modes (from {list(ROUTING_MODES)})")
    parser.add_argument("--scenarios", nargs="*", default=None,
                        help=f"scenarios (from {sorted(SCENARIOS)})")
    parser.add_argument("--repetitions", type=int, default=None)
    parser.add_argument("--duration", type=float, default=None,
                        help="override each run's duration, in seconds")
    parser.add_argument("--out", default=None, help="results CSV path")
    parser.add_argument("--resume", action="store_true",
                        help="skip runs already recorded without an error")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the run plan and exit without touching mininet")
    args = parser.parse_args(argv)

    config = load_config()
    modes = args.modes or list(config.get("experiments.modes",
                                          list(ROUTING_MODES)))
    scenarios = args.scenarios or list(config.get("experiments.scenarios",
                                                  sorted(SCENARIOS)))
    repetitions = args.repetitions or int(config.get("experiments.repetitions", 10))
    out = Path(args.out or config.get("experiments.output_csv",
                                      "results/experiments.csv"))
    duration = args.duration or 90.0

    try:
        plan = build_run_plan(modes, scenarios, repetitions, duration)
    except ExperimentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"{len(plan)} run(s) planned, writing to {out}:")
        for spec in plan:
            print(f"  {spec.key:34} pattern={spec.pattern:14} "
                  f"{spec.duration_s:.0f}s")
        total = sum(s.duration_s for s in plan)
        print(f"\n~{total / 60.0:.0f} min of traffic "
              f"(+ ~15 s setup/teardown per run)")
        return 0

    done = completed_keys(out) if args.resume else set()
    remaining = [s for s in plan if s.key not in done]
    if done:
        print(f"resuming: {len(done)} run(s) already recorded, "
              f"{len(remaining)} to go")
    if not remaining:
        print("nothing to do")
        return 0

    runner = ExperimentRunner(config)
    failures = 0
    for index, spec in enumerate(remaining, start=1):
        print(f"[{index}/{len(remaining)}] {spec.key}", flush=True)
        result = runner.run(spec)
        metrics = result["metrics"]
        context = result["context"]
        if metrics is None:
            failures += 1
            # Still record the failure, so --resume retries it and the results
            # file shows a gap rather than hiding a lost run.
            metrics = RunMetrics(mode=spec.mode, scenario=spec.scenario,
                                 repetition=spec.repetition)
            context["error"] = context["error"] or "run produced no metrics"
        append_result(out, metrics, context)
        print(f"    -> latency {metrics.mean_latency_ms:.2f}ms  "
              f"congestion {metrics.congestion_duration_s:.1f}s  "
              f"loss {metrics.loss_pct:.3f}%")

    print(f"\nwrote {out}")
    print(f"runs: {len(remaining)}, failed: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
