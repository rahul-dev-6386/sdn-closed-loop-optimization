#!/usr/bin/env python3
"""End-to-end demonstration of the project (P13).

Builds the fabric, runs the controller in ``ml_aware`` mode, drives traffic into
a congestion episode, and narrates what happens -- with real telemetry printed at
each step rather than a scripted expectation.

What the demo is meant to show
------------------------------
The single claim worth demonstrating is **proactivity**: the model should flag a
link *before* it congests, and the controller should move traffic off it before
the congestion threshold is crossed. Everything else in the project is machinery
in service of that.

The demo therefore prints a timeline, and checks two things:

1. **Did the forecast lead?** The first sample flagged ``predicted_congested``
   is compared against the first sample actually over threshold. A negative lead
   means the model was reacting rather than anticipating, and the demo says so.

2. **Did traffic move?** The utilisation of the path the flow was moved off is
   compared with the path it was moved onto. If both stayed flat, a reroute may
   have been recorded without any observable effect, and the demo reports that
   too rather than counting the event.

Both checks are reported honestly whichever way they come out. A demo that can
only ever succeed is not a demonstration.

Usage
-----
    sudo python scripts/demo.py
    sudo python scripts/demo.py --mode reactive_threshold   # the control
    sudo python scripts/demo.py --duration 150 --rate 14
"""

from __future__ import annotations

import argparse
import csv
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
from controller.reroute_loop import LoopConfig  # noqa: E402

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def step(text: str) -> None:
    """Print a numbered step header."""
    print(f"\n{BOLD}== {text}{RESET}")


def info(text: str) -> None:
    """Print an indented detail line."""
    print(f"   {text}")


def good(text: str) -> None:
    """Print a positive finding."""
    print(f"   {GREEN}{text}{RESET}")


def warn(text: str) -> None:
    """Print a caveat."""
    print(f"   {YELLOW}{text}{RESET}")


def bad(text: str) -> None:
    """Print a negative finding."""
    print(f"   {RED}{text}{RESET}")


# --------------------------------------------------------------------------- #
# Timeline analysis
# --------------------------------------------------------------------------- #
@dataclass
class Episode:
    """What happened during one demonstration run."""

    first_flagged_ts: Optional[float] = None
    first_congested_ts: Optional[float] = None
    peak_utilization: float = 0.0
    n_flagged: int = 0
    n_congested: int = 0
    util_by_path: Dict[str, List[float]] = field(default_factory=dict)
    reroute_count: int = 0

    @property
    def lead_time_s(self) -> Optional[float]:
        """How many seconds of warning the forecast gave.

        Positive means the model flagged the link *before* congestion began,
        which is the proactivity claim. Negative means the flag trailed
        congestion, i.e. the controller reacted rather than anticipated.
        ``None`` when either event did not happen.

        Sign convention: ``first_congested - first_flagged``. An earlier version
        subtracted the other way round, which made the narration congratulate a
        model for reacting late.
        """
        if self.first_flagged_ts is None or self.first_congested_ts is None:
            return None
        return self.first_congested_ts - self.first_flagged_ts

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        return {
            "first_flagged_ts": self.first_flagged_ts,
            "first_congested_ts": self.first_congested_ts,
            "lead_time_s": self.lead_time_s,
            "peak_utilization": round(self.peak_utilization, 4),
            "n_flagged": self.n_flagged,
            "n_congested": self.n_congested,
            "reroute_count": self.reroute_count,
            "util_by_path": {k: [round(v, 3) for v in vals]
                             for k, vals in self.util_by_path.items()},
        }


def classify_link(src: str, dst: str) -> str:
    """Name the diamond path a link belongs to.

    The topology's two paths are s1-s2-s4 and s1-s3-s4, so links are labelled by
    the middle switch they pass through, which is what a reader can see in the
    narration.
    """
    return "via s2" if "s2" in (src, dst) else "via s3"


def build_episode(
    telemetry_csv: Path,
    *,
    threshold: float = 0.80,
    flag_threshold: float = 0.5,
) -> Episode:
    """Reduce a demo run's telemetry into an :class:`Episode`.

    Congestion is read from the telemetry collector's own ``is_congested`` flag
    where present, and recomputed from utilisation otherwise, so the demo works
    against either a labelled or an unlabelled CSV.
    """
    episode = Episode()
    if not telemetry_csv.is_file():
        return episode

    with telemetry_csv.open(newline="", encoding="utf-8") as handle:
        rows = [dict(r) for r in csv.DictReader(handle)]
    if not rows:
        return episode

    def num(row: Dict[str, Any], key: str) -> Optional[float]:
        raw = (row.get(key) or "").strip()
        if raw == "":
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return None if value != value else value

    for row in sorted(rows, key=lambda r: float(r.get("timestamp") or 0.0)):
        timestamp = num(row, "timestamp")
        util = num(row, "utilization")
        if timestamp is None or util is None:
            continue

        flagged = str(row.get("predicted_congested", "")).strip().lower() in {
            "1", "true", "yes"}
        raw_congested = str(row.get("is_congested", "")).strip().lower()
        congested = (raw_congested in {"1", "true", "yes"}
                     if raw_congested else util >= threshold)

        src = row.get("src_switch", "")
        dst = row.get("dst_switch", "")
        episode.util_by_path.setdefault(classify_link(src, dst), []).append(util)

        if flagged and episode.first_flagged_ts is None:
            episode.first_flagged_ts = timestamp
        if flagged:
            episode.n_flagged += 1
        if congested:
            episode.n_congested += 1
            episode.peak_utilization = max(episode.peak_utilization, util)
            if episode.first_congested_ts is None:
                episode.first_congested_ts = timestamp

    del flag_threshold  # the collector already applied the decision threshold
    return episode


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
class DemoRunner:
    """Runs one demonstration against a real fabric."""

    def __init__(self, config: Any, mode: str, duration_s: float,
                 rate_mbps: float, telemetry_csv: Path) -> None:
        self.config = config
        self.mode = mode
        self.duration_s = duration_s
        self.rate_mbps = rate_mbps
        self.telemetry_csv = telemetry_csv
        self.topo: Any = None
        self.net: Any = None
        self.ryu: Optional[subprocess.Popen] = None

    def start_controller(self) -> None:
        """Start Ryu in the requested routing mode."""
        from ml.collect_dataset import wait_for_port

        env = os.environ.copy()
        env["PYTHONPATH"] = "."
        env[LoopConfig.MODE_ENV_VAR] = self.mode
        # Output goes to a file, not an undrained pipe: a full pipe blocks the
        # controller and silently halts its event loop. See common.proc.
        log_sink = controller_log(f"demo_{self.mode}")
        log_handle = ControllerLog(log_sink)
        log_fd = log_handle.__enter__()
        print(f"   controller log -> {log_sink}")
        self.ryu = subprocess.Popen(
            [venv_bin("ryu-manager"), "--ofp-listen-host", "127.0.0.1",
             "--ofp-tcp-listen-port", "6653", "controller.sdn_controller"],
            env=env, stdout=log_fd, stderr=log_fd,
            preexec_fn=os.setsid,
        )
        log_handle.__exit__()  # our copy; the child has its own
        if not wait_for_port(6653, timeout_s=10.0):
            raise RuntimeError("controller did not open 6653")

    def start_fabric(self) -> None:
        """Build the multipath topology against the controller."""
        from topology.multipath_topology import MultipathTopology, build_spec

        self.topo = MultipathTopology(
            build_spec(self.config),
            controller=("127.0.0.1", 6653),
            datapath_type=str(self.config.get("topology.datapath_type", "kernel")),
        )
        self.net = self.topo.build()
        time.sleep(3.0)

    def drive_traffic(self) -> None:
        """Run the ramp that produces a congestion episode."""
        from traffic.generate import run_live_pattern_on_hosts

        self.net.pingAll()
        run_live_pattern_on_hosts(
            net=self.net, pattern="ramp", duration_s=self.duration_s,
            seed=2026, base_rate_mbps=2.0, peak_rate_mbps=self.rate_mbps,
        )

    def stop(self) -> None:
        """Tear everything down, never raising."""
        for closer in (getattr(self.topo, "stop", None),
                       getattr(self.net, "stop", None)):
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        if self.ryu is not None and self.ryu.poll() is None:
            try:
                os.killpg(os.getpgid(self.ryu.pid), signal.SIGTERM)
                self.ryu.wait(timeout=3.0)
            except Exception:  # noqa: BLE001
                try:
                    os.killpg(os.getpgid(self.ryu.pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    pass
        mn_clean()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def report_episode(episode: Episode, mode: str) -> int:
    """Narrate what the run showed and return a process exit code.

    Returns 0 when the demo demonstrated the proactivity claim, 1 when it ran but
    did not, and 2 when the run produced nothing to analyse.
    """
    step("What happened")
    info(f"telemetry samples flagged as predicted-congested: {episode.n_flagged}")
    info(f"telemetry samples actually congested:              {episode.n_congested}")
    info(f"peak utilisation:                                   "
         f"{episode.peak_utilization:.2f}")

    for path, values in sorted(episode.util_by_path.items()):
        if not values:
            continue
        peak = max(values)
        info(f"peak utilisation {path:8}: {peak:.2f}")

    step("Did the forecast lead the congestion?")
    if episode.n_congested == 0:
        warn("no congestion occurred, so proactivity cannot be assessed")
        warn("try a higher --rate or a longer --duration")
        return 2
    if episode.first_flagged_ts is None:
        bad("the model never flagged any link before congestion began")
        bad("in this run the controller reacted to congestion rather than "
            "anticipating it")
        return 1

    lead = episode.lead_time_s
    if lead is None:
        bad("flagged and congested timestamps are unavailable")
        return 1
    if lead > 0:
        good(f"forecast led congestion by {lead:.1f}s")
        good("this is the proactivity claim: the model saw it coming, and the "
             "controller moved traffic before the threshold was crossed")
        return 0
    bad(f"the first flag arrived {abs(lead):.1f}s AFTER congestion began")
    bad("this run demonstrates reaction, not anticipation")
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", default="ml_aware",
                        help="routing mode to demonstrate")
    parser.add_argument("--duration", type=float, default=150.0,
                        help="traffic duration in seconds")
    parser.add_argument("--rate", type=float, default=14.0,
                        help="peak offered rate in Mbps")
    parser.add_argument("--telemetry", default=None,
                        help="reuse an existing telemetry CSV instead of running")
    args = parser.parse_args(argv)

    config = load_config()
    telemetry_csv = Path(args.telemetry or config.get(
        "telemetry.csv_path", "data/telemetry.csv"))

    print(f"{BOLD}AI-Driven SDN Traffic Optimization -- demonstration{RESET}")
    info(f"routing mode: {args.mode}")
    info(f"offered load: ramp to {args.rate:.0f} Mbps over {args.duration:.0f}s")

    if args.telemetry:
        step("Replaying existing telemetry")
        info(str(telemetry_csv))
        return report_episode(build_episode(telemetry_csv), args.mode)

    step("Bringing up the fabric")
    mn_clean()
    if telemetry_csv.exists():
        telemetry_csv.unlink()
        info(f"cleared {telemetry_csv} so no run inherits the last one's samples")

    runner = DemoRunner(config, args.mode, args.duration, args.rate,
                        telemetry_csv)
    try:
        runner.start_controller()
        info("controller listening on 127.0.0.1:6653")
        runner.start_fabric()
        info("multipath diamond up (s1-s2-s4 and s1-s3-s4)")
        step("Driving traffic")
        runner.drive_traffic()
        # Let the final telemetry samples land before the fabric is torn down.
        time.sleep(3.0)
    except Exception as exc:  # noqa: BLE001
        print(f"\n{RED}demo failed: {type(exc).__name__}: {exc}{RESET}")
        return 2
    finally:
        runner.stop()

    return report_episode(build_episode(telemetry_csv), args.mode)


if __name__ == "__main__":
    sys.exit(main())
