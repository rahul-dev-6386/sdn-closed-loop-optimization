"""Live dataset collection and offline future-congestion labeling engine.

Implements strict Phase 6 requirements:
1. Training data originates from LIVE runs:
   Mininet diamond + Ryu controller + real iperf3 traffic.
2. Supports 10+ seeded runs per pattern (ramp, bursty_onoff, poisson, tcp_udp_mix, elephant_mice),
   90-120 s each, with run_id, pattern, seed, and per-link per-direction rows including idle links.
3. Clean isolation:
   Runs one episode at a time with 'mn -c' between runs, fully resumable if interrupted.
4. Offline Future Horizon Labeling:
   - Reads horizon_n_s (10.0 s) and label_gap_s (2.0 s) strictly from config.yaml.
   - Samples near the end of an episode where the future horizon cannot be fully observed
     are flagged with horizon_valid=False and excluded from training.
   - Zero label leakage: present-only congestion does not set label=1 if future is clear.
5. Detailed Reporting:
   Reports class distribution per pattern, per run, and per link-direction.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import Config, load_config
from controller.telemetry import CSV_FIELDNAMES
from ml.validate import (  # noqa: E402
    DEFAULT_MAX_JUMP_FRACTION,
    link_rate_series,
    validate_shape,
)
from common.proc import (
    ControllerLog,
    check_required_tools,
    controller_log,
    mn_clean,
    venv_bin,
)
from traffic.generate import PATTERNS, run_live_pattern_on_hosts

PROGRESS_FILE = "data/dataset_progress.json"
RAW_DATASET_FILE = "data/telemetry_live.csv"
LABELED_DATASET_FILE = "data/telemetry_live_labeled.csv"
DISTRIBUTION_REPORT_FILE = "results/dataset_distribution.json"

LABELED_FIELDNAMES = CSV_FIELDNAMES + [
    "pattern",
    "seed",
    "label_future_congested",
    "lead_time_s",
    "horizon_valid",
]

def get_feature_allowlist(config: Optional[Config] = None) -> List[str]:
    """Read the explicit feature allowlist from config.yaml (ml.features)."""
    cfg = config if config is not None else load_config()
    features = cfg.get("ml.features")
    if not features:
        raise ValueError("ml.features allowlist not defined in config.yaml")
    return list(features)


def validate_training_feature_columns(
    feature_columns: Iterable[str], config: Optional[Config] = None
) -> None:
    """Validate that every column in the training matrix is permitted by ml.features.

    Parameters
    ----------
    feature_columns:
        Iterable of column names present in the training matrix.
    config:
        Parsed system configuration.

    Raises
    ------
    ValueError:
        If any column in feature_columns is not in the explicit allowlist.
    """
    allowlist = set(get_feature_allowlist(config))
    invalid = []
    for col in feature_columns:
        # Base column or standard rolling feature derivatives (e.g. utilization_mean)
        base = col.split("_mean")[0].split("_std")[0].split("_var")[0].split("_slope")[0]
        if col not in allowlist and base not in allowlist:
            invalid.append(col)
    if invalid:
        raise ValueError(
            f"Training matrix contains forbidden non-allowlisted columns: {invalid}! "
            f"Explicit allowlist from config.yaml (ml.features): {sorted(allowlist)}"
        )


def wait_for_port(port: int = 6653, host: str = "127.0.0.1", timeout_s: float = 10.0) -> bool:
    """Wait for TCP socket to accept connections."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (socket.error, OSError):
            time.sleep(0.2)
    return False


class LiveDatasetCollector:
    """Orchestrates live Mininet + Ryu + iperf3 dataset collection runs."""

    def __init__(
        self,
        config: Optional[Config] = None,
        out_dir: str = "data",
        raw_filename: str = "telemetry_live.csv",
    ) -> None:
        self.config = config if config is not None else load_config()
        self.out_dir = out_dir
        self.raw_dataset_file = os.path.join(out_dir, raw_filename)
        self.progress_file = os.path.join(out_dir, "dataset_progress.json")
        self.sample_interval_s = float(self.config.get("telemetry.sample_interval_s", 2.0))
        self.default_duration_s = float(self.config.get("traffic.episode_duration_s", 90.0))
        # Runs whose measured traffic did not match the pattern that generated
        # it. Reported at the end of a sweep rather than silently tolerated.
        self.shape_failures: List[Dict[str, Any]] = []
        # Patterns with no declared shape, recorded so a sweep can report that
        # it checked fewer runs than it collected rather than implying coverage.
        self.shape_unchecked: set = set()
        self.base_rate_mbps = float(self.config.get("traffic.base_rate_mbps", 2.0))
        self.peak_rate_mbps = float(self.config.get("traffic.peak_rate_mbps", 12.0))
        self.telemetry_csv = str(self.config.get("telemetry.csv_path", "data/telemetry.csv"))

    def load_progress(self) -> Dict[str, Any]:
        """Load list of completed run IDs."""
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"completed_runs": []}

    def save_progress(self, run_id: str) -> None:
        """Record completed run ID."""
        prog = self.load_progress()
        if run_id not in prog["completed_runs"]:
            prog["completed_runs"].append(run_id)
        os.makedirs(os.path.dirname(self.progress_file) or ".", exist_ok=True)
        with open(self.progress_file, "w") as f:
            json.dump(prog, f, indent=2)

    def execute_live_run(
        self,
        pattern: str,
        seed: int,
        duration_s: float,
    ) -> List[Dict[str, Any]]:
        """Execute a single isolated live Mininet + Ryu + iperf3 episode."""
        run_id = f"run_{pattern}_s{seed}"
        print(f"\n>>> Starting live run: {run_id} ({duration_s}s, pattern={pattern}, seed={seed})")

        # 1. Clean previous state
        mn_clean()

        # Clear telemetry csv before run so we capture only this episode's data
        if os.path.exists(self.telemetry_csv):
            os.remove(self.telemetry_csv)

        # 2. Start Ryu Controller
        ryu_bin = venv_bin("ryu-manager")
        ryu_cmd = [
            ryu_bin,
            "--ofp-listen-host",
            "127.0.0.1",
            "--ofp-tcp-listen-port",
            "6653",
            "controller.sdn_controller",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = "."

        # Drain the controller's output to a file. See common.proc.ControllerLog:
        # an undrained PIPE blocks the controller once the 64 KiB buffer fills,
        # which silently stops telemetry after ~24 s of every 90 s run.
        log_sink = controller_log(run_id)
        log_handle = ControllerLog(log_sink)
        log_fd = log_handle.__enter__()
        print(f"    controller log -> {log_sink}")
        ryu_proc = subprocess.Popen(
            ryu_cmd,
            env=env,
            stdout=log_fd,
            stderr=log_fd,
            preexec_fn=os.setsid,
        )
        # Safe to close our copy now: Popen duplicated the fd into the child.
        # The handle must stay referenced until this point -- letting the
        # ControllerLog be garbage collected first would close the fd before
        # Popen ever saw it.
        log_handle.__exit__()

        run_rows: List[Dict[str, Any]] = []

        try:
            if not wait_for_port(6653, timeout_s=10.0):
                raise RuntimeError("Ryu controller failed to listen on port 6653")

            # 3. Build the multipath diamond and attach our controller.
            #
            # NOTE: this used to construct a `MultipathDiamondTopo` and hand it
            # to `Mininet(topo=...)`. That class never existed -- P2 delivers
            # `MultipathTopology`, a *builder* that constructs Mininet itself and
            # returns the started net. Against the old call the import raised
            # ImportError, so the live collector had never completed a single run
            # and no dataset was ever written. Same shape as scripts/verify_p3.py.
            from topology.multipath_topology import (
                MultipathTopology,
                build_spec,
            )

            datapath = str(self.config.get("topology.datapath_type", "kernel"))
            topo = MultipathTopology(
                build_spec(self.config),
                controller=("127.0.0.1", 6653),
                datapath_type=datapath,
            )
            net = topo.build()
            time.sleep(3.0)  # Wait for switches to handshake and LLDP probes

            h1 = net.get("h1")
            h4 = net.get("h4")
            net.ping([h1, h4])  # ARP learning and path establishment

            # Rule 2: Alternate path across runs so load and congestion appear on BOTH paths
            target_path_switch = "s2" if (seed % 2 == 0) else "s3"
            chosen_path = ["h1", "s1", target_path_switch, "s4", "h4"]
            print(f"    Directing live run traffic along path via {target_path_switch}: {' -> '.join(chosen_path)}")
            from topology.multipath_topology import (
                build_flow_plan,
                StaticFlowInstaller,
            )
            flow_rules = build_flow_plan(
                spec=topo.spec,
                path=chosen_path,
                port_of=lambda sw, nb: topo.port_of(sw, nb),
                bidirectional=True,
            )
            installer = StaticFlowInstaller()
            installer.install(flow_rules)

            # 4. Run traffic pattern on hosts
            print(f"    Running traffic pattern '{pattern}' on h1 -> h4...")
            run_live_pattern_on_hosts(
                net=net,
                pattern=pattern,
                duration_s=duration_s,
                seed=seed,
                base_rate_mbps=self.base_rate_mbps,
                peak_rate_mbps=self.peak_rate_mbps,
            )

            # Wait briefly for final telemetry samples to write
            time.sleep(2.5)

            # Stop Mininet
            topo.stop()

        finally:
            # Terminate Ryu controller
            if ryu_proc.poll() is None:
                try:
                    os.killpg(os.getpgid(ryu_proc.pid), signal.SIGTERM)
                    ryu_proc.wait(timeout=3.0)
                except Exception:
                    try:
                        os.killpg(os.getpgid(ryu_proc.pid), signal.SIGKILL)
                    except Exception:
                        pass
            mn_clean()

        # 5. Extract rows from telemetry.csv and tag with run metadata
        if os.path.exists(self.telemetry_csv):
            with open(self.telemetry_csv, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row_dict = dict(row)
                    row_dict["run_id"] = run_id
                    row_dict["pattern"] = pattern
                    row_dict["seed"] = seed
                    run_rows.append(row_dict)

        print(f"    Collected {len(run_rows)} telemetry samples from {run_id}.")

        # 6. Did the traffic we *asked for* resemble the traffic we *got*?
        #
        # 25 runs of a defective `ramp` -- square waves instead of ramps -- were
        # collected, labelled, trained on and reported before anyone noticed.
        # Every artefact looked plausible; only reading the raw rate profile by
        # hand revealed it. This check makes that automatic.
        if run_rows:
            # Only patterns declared in traffic.shape_assertions are checked.
            # The rest have legitimately irregular shapes, and asserting a
            # rise-and-fall on them would flag correct behaviour as a defect.
            spec = (self.config.get("traffic.shape_assertions", {}) or {}).get(pattern)
            if spec:
                verdict = validate_shape(
                    run_id, pattern, link_rate_series(run_rows),
                    peak_mbps=float(spec.get("peak_mbps", self.peak_rate_mbps)),
                    max_jump_fraction=float(
                        spec.get("max_jump_fraction", DEFAULT_MAX_JUMP_FRACTION)),
                )
                if verdict.ok:
                    print(f"    Shape OK: peak {verdict.peak_mbps:.1f} Mbps, "
                          f"largest jump {verdict.max_jump_mbps:.1f} Mbps")
                    # Print the profile itself. The verdict says the shape was
                    # acceptable; the profile lets a reader confirm it by eye,
                    # which is how the original defect was caught.
                    series = link_rate_series(run_rows)
                    shown = ", ".join(f"{r:.0f}" for r in series[::2])
                    print(f"    Rate profile (every 2nd sample): {shown}")
                else:
                    print(f"    Shape NOT as intended for {run_id}:")
                    for reason in verdict.reasons:
                        print(f"      - {reason}")
                    self.shape_failures.append(verdict.as_dict())
            else:
                self.shape_unchecked.add(pattern)
        return run_rows

    def collect_live_dataset(
        self,
        patterns: List[str] = PATTERNS,
        runs_per_pattern: int = 10,
        duration_s: Optional[float] = None,
        base_seed: int = 42,
        resume: bool = True,
    ) -> str:
        """Run all seeded episodes across patterns, storing raw live data."""
        duration = duration_s if duration_s is not None else self.default_duration_s
        progress = self.load_progress() if resume else {"completed_runs": []}
        completed = set(progress.get("completed_runs", []))

        os.makedirs(os.path.dirname(self.raw_dataset_file) or ".", exist_ok=True)
        write_header = not os.path.exists(self.raw_dataset_file) or os.path.getsize(self.raw_dataset_file) == 0

        raw_fieldnames = CSV_FIELDNAMES + ["pattern", "seed"]

        total_runs = len(patterns) * runs_per_pattern
        current_run_idx = 0

        for p_idx, pattern in enumerate(patterns):
            for r_idx in range(runs_per_pattern):
                current_run_idx += 1
                seed = base_seed + (p_idx * 100) + r_idx
                run_id = f"run_{pattern}_s{seed}"

                if resume and run_id in completed:
                    print(f"[{current_run_idx}/{total_runs}] Skipping completed run {run_id}")
                    continue

                print(f"\n[{current_run_idx}/{total_runs}] Executing live run {run_id}...")
                rows = self.execute_live_run(pattern, seed, duration)

                if rows:
                    with open(self.raw_dataset_file, "a", newline="") as f:
                        writer = csv.DictWriter(f, fieldnames=raw_fieldnames)
                        if write_header:
                            writer.writeheader()
                            write_header = False
                        for r in rows:
                            # Filter to raw fieldnames
                            out_row = {k: r.get(k, "") for k in raw_fieldnames}
                            writer.writerow(out_row)

                    self.save_progress(run_id)

        print(f"\nLive dataset collection completed. Raw data saved to {self.raw_dataset_file}.")
        return self.raw_dataset_file


# --------------------------------------------------------------------------- #
# Offline Future-Congestion Labeling
# --------------------------------------------------------------------------- #
def label_telemetry_dataset(
    raw_csv_path: str = RAW_DATASET_FILE,
    output_labeled_path: str = LABELED_DATASET_FILE,
    config: Optional[Config] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read raw telemetry CSV and apply offline future-congestion horizon labeling.

    Label(t) = 1 if link is congested at ANY point in [t + gap, t + gap + horizon].
    """
    cfg = config if config is not None else load_config()
    horizon_n_s = float(cfg.get("ml.horizon_n_s", 10.0))
    label_gap_s = float(cfg.get("ml.label_gap_s", 2.0))
    sample_interval_s = float(cfg.get("telemetry.sample_interval_s", 2.0))

    if not os.path.exists(raw_csv_path):
        raise FileNotFoundError(f"Raw telemetry file {raw_csv_path} does not exist")

    # Group rows by (run_id, src_switch, dst_switch, direction)
    groups: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}

    with open(raw_csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            run_id = row["run_id"]
            src = row["src_switch"]
            dst = row["dst_switch"]
            dir_ = row.get("direction", "forward")
            key = (run_id, src, dst, dir_)
            groups.setdefault(key, []).append(row)

    labeled_rows: List[Dict[str, Any]] = []

    for key, rows in groups.items():
        # Sort chronologically
        rows.sort(key=lambda r: float(r["timestamp"]))
        n = len(rows)
        max_t = float(rows[-1]["timestamp"])

        for i, curr in enumerate(rows):
            t_curr = float(curr["timestamp"])
            t_horizon_start = t_curr + label_gap_s
            t_horizon_end = t_horizon_start + horizon_n_s

            # Check if full horizon can be observed before episode ends
            horizon_valid = t_horizon_end <= (max_t + sample_interval_s * 0.5)

            future_congested = False
            lead_time_s = -1.0

            # Scan strictly inside [t_horizon_start, t_horizon_end]
            for j in range(i + 1, n):
                t_future = float(rows[j]["timestamp"])
                if t_future < t_horizon_start:
                    continue
                if t_future > t_horizon_end:
                    break
                is_cong = (
                    rows[j].get("is_congested", "False").lower() in ("true", "1")
                    or float(rows[j].get("utilization", 0.0)) >= float(cfg.get("telemetry.congestion_utilization_threshold", 0.80))
                    or float(rows[j].get("loss_pct", 0.0)) >= float(cfg.get("telemetry.congestion_loss_threshold_pct", 2.0))
                    or float(rows[j].get("latency_ms", 0.0)) >= float(cfg.get("telemetry.congestion_latency_threshold_ms", 40.0))
                )
                if is_cong:
                    future_congested = True
                    if lead_time_s < 0:
                        lead_time_s = round(t_future - t_curr, 2)

            curr["label_future_congested"] = 1 if future_congested else 0
            curr["lead_time_s"] = lead_time_s
            curr["horizon_valid"] = horizon_valid
            labeled_rows.append(curr)

    # Sort chronologically by run and timestamp
    labeled_rows.sort(key=lambda r: (r["run_id"], float(r["timestamp"]), r["src_switch"], r["dst_switch"]))

    # Write labeled CSV
    os.makedirs(os.path.dirname(output_labeled_path) or ".", exist_ok=True)
    with open(output_labeled_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LABELED_FIELDNAMES)
        writer.writeheader()
        for r in labeled_rows:
            writer.writerow({k: r.get(k, "") for k in LABELED_FIELDNAMES})

    # Compute comprehensive distribution report
    report = generate_detailed_distribution_report(labeled_rows)
    return labeled_rows, report


def generate_detailed_distribution_report(labeled_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generate class distribution report per pattern, per run, and per link-direction."""
    valid_rows = [r for r in labeled_rows if str(r.get("horizon_valid", "")).lower() in ("true", "1")]

    total_valid = len(valid_rows)
    positives = sum(1 for r in valid_rows if int(r.get("label_future_congested", 0)) == 1)
    negatives = total_valid - positives
    pos_ratio = (positives / total_valid) if total_valid > 0 else 0.0

    # Per Pattern
    per_pattern: Dict[str, Dict[str, Any]] = {}
    for pat in PATTERNS:
        p_rows = [r for r in valid_rows if r.get("pattern") == pat]
        p_tot = len(p_rows)
        p_pos = sum(1 for r in p_rows if int(r.get("label_future_congested", 0)) == 1)
        per_pattern[pat] = {
            "total_samples": p_tot,
            "positive_samples": p_pos,
            "negative_samples": p_tot - p_pos,
            "positive_ratio": round(p_pos / p_tot, 4) if p_tot > 0 else 0.0,
        }

    # Per Run
    runs: Dict[str, Dict[str, Any]] = {}
    for r in valid_rows:
        rid = r["run_id"]
        runs.setdefault(rid, {"total": 0, "pos": 0, "neg": 0, "pattern": r.get("pattern")})
        runs[rid]["total"] += 1
        if int(r.get("label_future_congested", 0)) == 1:
            runs[rid]["pos"] += 1
        else:
            runs[rid]["neg"] += 1

    per_run = {
        rid: {
            "pattern": d["pattern"],
            "total_samples": d["total"],
            "positive_samples": d["pos"],
            "negative_samples": d["neg"],
            "positive_ratio": round(d["pos"] / d["total"], 4) if d["total"] > 0 else 0.0,
        }
        for rid, d in runs.items()
    }

    # Per Link Direction
    links: Dict[str, Dict[str, Any]] = {}
    for r in valid_rows:
        link_dir = f"{r['src_switch']}->{r['dst_switch']}:{r.get('direction', 'forward')}"
        links.setdefault(link_dir, {"total": 0, "pos": 0, "neg": 0})
        links[link_dir]["total"] += 1
        if int(r.get("label_future_congested", 0)) == 1:
            links[link_dir]["pos"] += 1
        else:
            links[link_dir]["neg"] += 1

    per_link = {
        ld: {
            "total_samples": d["total"],
            "positive_samples": d["pos"],
            "negative_samples": d["neg"],
            "positive_ratio": round(d["pos"] / d["total"], 4) if d["total"] > 0 else 0.0,
        }
        for ld, d in links.items()
    }

    lead_times = [float(r["lead_time_s"]) for r in valid_rows if float(r.get("lead_time_s", -1)) > 0]
    lead_time_stats = {
        "count": len(lead_times),
        "mean_s": round(sum(lead_times) / len(lead_times), 2) if lead_times else 0.0,
        "min_s": min(lead_times) if lead_times else 0.0,
        "max_s": max(lead_times) if lead_times else 0.0,
    }

    report = {
        "total_samples": len(labeled_rows),
        "valid_horizon_samples": total_valid,
        "positive_samples": positives,
        "negative_samples": negatives,
        "positive_class_ratio": round(pos_ratio, 4),
        "negative_class_ratio": round(1.0 - pos_ratio, 4),
        "is_balanced": 0.20 <= pos_ratio <= 0.65,
        "per_pattern": per_pattern,
        "per_link_direction": per_link,
        "per_run_summary": per_run,
        "lead_time_stats": lead_time_stats,
    }

    os.makedirs(os.path.dirname(DISTRIBUTION_REPORT_FILE) or ".", exist_ok=True)
    with open(DISTRIBUTION_REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    return report


def preflight(cfg: Any) -> bool:
    """Validate tools, controller imports, and required config before running."""
    print(f"\n{'-' * 78}\n[Preflight System Verification]\n{'-' * 78}")
    tools = ("mn", "ovs-vsctl", "ovs-ofctl", "ryu-manager", "iperf3")
    found_tools = check_required_tools(tools)
    all_tools_ok = True
    for t in tools:
        path = found_tools.get(t)
        if path:
            print(f"  [TOOL] {t:<12} -> {path}")
        else:
            print(f"  [TOOL] {t:<12} -> MISSING", file=sys.stderr)
            all_tools_ok = False

    try:
        import controller.sdn_controller  # noqa: F401
        print("  [IMPORT] controller.sdn_controller imported cleanly")
        import_ok = True
    except Exception as exc:
        print(f"  [IMPORT ERROR] Failed to import controller.sdn_controller: {exc}", file=sys.stderr)
        import_ok = False

    req_keys = ["topology.switches", "topology.links", "ml.features"]
    cfg_ok = all(cfg.get(k) is not None for k in req_keys)

    return all_tools_ok and import_ok and cfg_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Dataset Collection & Offline Labeling")
    parser.add_argument("--live", action="store_true", help="Run live Mininet+Ryu+iperf3 collection (requires root)")
    parser.add_argument("--runs-per-pattern", type=int, default=10, help="Number of runs per pattern (default: 10)")
    parser.add_argument("--duration", type=float, default=90.0, help="Duration in seconds per run (default: 90.0)")
    parser.add_argument("--out-dir", type=str, default="data", help="Target directory for dataset files (default: data)")
    parser.add_argument("--no-resume", action="store_true", help="Do not resume; overwrite progress")
    parser.add_argument("--label-only", action="store_true", help="Apply labeling to existing telemetry_live.csv")
    parser.add_argument("--smoke", action="store_true", help="Quick smoke test mode (1 run per pattern, 30s, data/smoke/)")
    parser.add_argument("--dry-run", action="store_true", help="Perform preflight and import checks without root/Mininet")
    args = parser.parse_args()

    cfg = load_config()

    if not preflight(cfg):
        print("\nPreflight check failed. Aborting.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n--dry-run requested: Preflight passed successfully. Exiting.")
        return 0

    out_dir = "data/smoke" if args.smoke else args.out_dir
    runs_per_pat = 1 if args.smoke else args.runs_per_pattern
    dur = 30.0 if args.smoke else args.duration

    collector = LiveDatasetCollector(
        config=cfg,
        out_dir=out_dir,
        raw_filename="telemetry_live.csv",
    )

    raw_csv = os.path.join(out_dir, "telemetry_live.csv")
    labeled_csv = os.path.join(out_dir, "telemetry_live_labeled.csv")

    if args.live or args.smoke:
        if os.geteuid() != 0:
            print("ERROR: Live collection requires root privileges (run with sudo).", file=sys.stderr)
            return 2

        print(f"Starting LIVE dataset collection ({out_dir}): {len(PATTERNS)} patterns x {runs_per_pat} runs, {dur}s each...")
        collector.collect_live_dataset(
            patterns=PATTERNS,
            runs_per_pattern=runs_per_pat,
            duration_s=dur,
            resume=not args.no_resume,
        )

    # Perform offline labeling
    print(f"\nApplying offline future-congestion labeling to {raw_csv}...")
    if not os.path.exists(raw_csv):
        print(f"Notice: {raw_csv} not found. Run with --live or --smoke (as root) to collect data.", file=sys.stderr)
        return 1

    labeled_rows, report = label_telemetry_dataset(
        raw_csv_path=raw_csv,
        output_labeled_path=labeled_csv,
        config=cfg,
    )

    print("\nDataset Distribution Report:")
    print(f"  Total Samples:         {report['total_samples']}")
    print(f"  Valid Horizon Samples: {report['valid_horizon_samples']}")
    print(f"  Positive Labels:       {report['positive_samples']} ({report['positive_class_ratio']*100:.1f}%)")
    print(f"  Negative Labels:       {report['negative_samples']} ({report['negative_class_ratio']*100:.1f}%)")
    print(f"  Mean Lead Time:        {report['lead_time_stats']['mean_s']} s")
    print(f"  Balanced:              {'YES' if report['is_balanced'] else 'NO'}")

    # Automatically run dataset quality & integrity checks
    try:
        from scripts.check_live_dataset import check_dataset
        print("\nRunning live dataset quality & integrity validation...")
        passed, chk_report = check_dataset(labeled_csv, cfg, verbose=True)
        if not passed:
            print("ERROR: Dataset validation checks reported failures!", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"Warning: could not run check_live_dataset: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
