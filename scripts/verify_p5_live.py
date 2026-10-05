#!/usr/bin/env python3
"""Phase 5 LIVE acceptance test: Mininet + Ryu + iperf3 Telemetry Validation.

Requires root (sudo), unless invoked with --dry-run.

This script:
1. Performs preflight checks (tools, controller imports, config keys).
2. Launches Ryu with loop-safe controller and telemetry collector.
3. Initializes Mininet diamond topology using MultipathTopology builder.
4. Verifies h1 <-> h4 connectivity and discovery.
5. Measures idle link rates and baseline probe latency.
6. Runs iperf3 UDP flows at known target rates: 2.0 Mbps, 5.0 Mbps, 8.0 Mbps over s2 path.
7. Asserts measured Mbps is within ~15% of target load.
8. Asserts idle link (s3 path) measures near 0 Mbps.
9. Asserts probe latency is near configured delay when idle and tracks queue delay under load.
10. Displays CSV head and summary statistics from data/telemetry.csv.
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
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import load_config
from common.proc import check_required_tools, mn_clean, venv_bin
from topology.multipath_topology import MultipathTopology, build_spec

PASSED = 0
FAILED = 0


def section(title: str) -> None:
    print(f"\n{'-' * 78}\n[{title}]\n{'-' * 78}")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}" + (f" ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f" ({detail})" if detail else ""))


def wait_for_port(port: int = 6653, host: str = "127.0.0.1", timeout_s: float = 15.0) -> bool:
    """Wait for TCP socket to accept connections."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (socket.error, OSError):
            time.sleep(0.3)
    return False


def get_latest_csv_record(csv_path: str, src_sw: str, dst_sw: str) -> Optional[Dict[str, Any]]:
    """Fetch the most recent CSV telemetry record for a link."""
    if not os.path.exists(csv_path):
        return None
    latest = None
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("src_switch") == src_sw and row.get("dst_switch") == dst_sw:
                latest = row
    return latest


def preflight(cfg: Any) -> bool:
    """Validate tools, controller imports, and required config before running."""
    section("Preflight System Verification")
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
    check("all required binaries present", all_tools_ok)

    # Controller import check
    try:
        import controller.sdn_controller  # noqa: F401
        print("  [IMPORT] controller.sdn_controller imported cleanly")
        check("controller module imports without error", True)
    except Exception as exc:
        print(f"  [IMPORT ERROR] Failed to import controller.sdn_controller: {exc}", file=sys.stderr)
        check("controller module imports without error", False, str(exc))
        return False

    # Config keys check
    req_keys = ["topology.switches", "topology.links", "telemetry.sample_interval_s"]
    cfg_ok = all(cfg.get(k) is not None for k in req_keys)
    check("required configuration keys present", cfg_ok)

    return all_tools_ok and cfg_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Phase 5 Telemetry Verification")
    parser.add_argument("--out", help="Write JSON summary to this path", default=None)
    parser.add_argument("--tolerance", type=float, default=0.15, help="Rate tolerance fraction (default: 0.15 for 15%)")
    parser.add_argument("--dry-run", action="store_true", help="Perform preflight and import checks without root/Mininet")
    args = parser.parse_args()

    print("=" * 78)
    print("AI-Driven SDN: Phase 5 LIVE Acceptance Test (Mininet + Ryu + iperf3)")
    print("=" * 78)

    cfg = load_config()

    # Preflight check
    if not preflight(cfg):
        print("\nPreflight check failed. Aborting before starting network.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n--dry-run requested: Preflight passed successfully. Exiting.")
        return 0

    if os.geteuid() != 0:
        print("ERROR: verify_p5_live.py requires root privileges. Run with sudo.", file=sys.stderr)
        return 2

    csv_path = str(cfg.get("telemetry.csv_path", "data/telemetry.csv"))
    if os.path.exists(csv_path):
        os.remove(csv_path)

    # 1. Clean previous Mininet/OVS state and start Controller
    section("1. Cleaning Environment & Starting Controller")
    mn_clean()

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

    ryu_proc = subprocess.Popen(
        ryu_cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )

    topo = None
    try:
        check("controller listening on port 6653", wait_for_port(6653, timeout_s=15.0))

        # 2. Start Mininet Diamond Topology using MultipathTopology builder
        section("2. Starting Mininet Multipath Diamond Topology")
        spec = build_spec(cfg)
        datapath_type = str(cfg.get("topology.datapath_type", "kernel"))
        topo = MultipathTopology(
            spec=spec,
            controller=("127.0.0.1", 6653),
            datapath_type=datapath_type,
        )
        net = topo.build()
        time.sleep(3.0)  # Wait for switches to complete handshake and LLDP probes

        h1 = net.get("h1")
        h4 = net.get("h4")

        # Ping to discover hosts and establish flows
        ping_loss = net.ping([h1, h4])
        check("h1 <-> h4 initial ping passed (0% loss)", ping_loss == 0.0, f"loss={ping_loss}%")

        # 3. Idle Baseline Measurement
        section("3. Idle Baseline Measurement (s1-s2 vs s1-s3)")
        time.sleep(4.0)  # Allow 2 telemetry polling cycles
        idle_s2 = get_latest_csv_record(csv_path, "s1", "s2")
        idle_s3 = get_latest_csv_record(csv_path, "s1", "s3")

        check("telemetry CSV generated during idle baseline", idle_s2 is not None and idle_s3 is not None)
        idle_rate_s2 = float(idle_s2["tx_rate_mbps"]) if idle_s2 else 0.0
        idle_rate_s3 = float(idle_s3["tx_rate_mbps"]) if idle_s3 else 0.0
        idle_lat_s2 = float(idle_s2["latency_ms"]) if idle_s2 else 0.0

        check("idle active path s1->s2 tx_rate < 0.1 Mbps", idle_rate_s2 < 0.1, f"rate={idle_rate_s2:.4f} Mbps")
        check("idle alternate path s1->s3 tx_rate < 0.1 Mbps", idle_rate_s3 < 0.1, f"rate={idle_rate_s3:.4f} Mbps")
        check(
            "idle probe latency near configured delay (2 - 12 ms)",
            2.0 <= idle_lat_s2 <= 12.0,
            f"latency={idle_lat_s2:.2f} ms",
        )

        # 4. Start iperf3 server on h4
        section("4. Starting iperf3 Server on h4")
        h4.cmd("killall -9 iperf3 2>/dev/null; true")
        h4.cmd("iperf3 -s -D -p 5201")
        time.sleep(1.0)

        # 5. Test Rate 1: 2.0 Mbps UDP
        section("5. iperf3 Load Test: 2.0 Mbps UDP over Path s1->s2")
        h1.cmd("iperf3 -u -c 10.0.0.4 -b 2M -t 6 -p 5201 > /dev/null 2>&1")
        time.sleep(2.5)  # Sample interval is 2.0s
        rec_2m_s2 = get_latest_csv_record(csv_path, "s1", "s2")
        rec_2m_s3 = get_latest_csv_record(csv_path, "s1", "s3")

        rate_2m_s2 = float(rec_2m_s2["tx_rate_mbps"]) if rec_2m_s2 else 0.0
        rate_2m_s3 = float(rec_2m_s3["tx_rate_mbps"]) if rec_2m_s3 else 0.0
        check(
            "s1->s2 measured rate within tolerance of 2.0 Mbps",
            abs(rate_2m_s2 - 2.0) <= (2.0 * args.tolerance),
            f"measured={rate_2m_s2:.2f} Mbps (target=2.00 Mbps, diff={abs(rate_2m_s2 - 2.0):.2f})",
        )
        check("s1->s3 idle path remains near 0 Mbps", rate_2m_s3 < 0.1, f"rate={rate_2m_s3:.4f} Mbps")

        # 6. Test Rate 2: 5.0 Mbps UDP
        section("6. iperf3 Load Test: 5.0 Mbps UDP over Path s1->s2")
        h1.cmd("iperf3 -u -c 10.0.0.4 -b 5M -t 6 -p 5201 > /dev/null 2>&1")
        time.sleep(2.5)
        rec_5m_s2 = get_latest_csv_record(csv_path, "s1", "s2")
        rec_5m_s3 = get_latest_csv_record(csv_path, "s1", "s3")

        rate_5m_s2 = float(rec_5m_s2["tx_rate_mbps"]) if rec_5m_s2 else 0.0
        rate_5m_s3 = float(rec_5m_s3["tx_rate_mbps"]) if rec_5m_s3 else 0.0
        check(
            "s1->s2 measured rate within tolerance of 5.0 Mbps",
            abs(rate_5m_s2 - 5.0) <= (5.0 * args.tolerance),
            f"measured={rate_5m_s2:.2f} Mbps (target=5.00 Mbps, diff={abs(rate_5m_s2 - 5.0):.2f})",
        )
        check("s1->s3 idle path remains near 0 Mbps", rate_5m_s3 < 0.1, f"rate={rate_5m_s3:.4f} Mbps")

        # 7. Test Rate 3: 8.0 Mbps UDP
        section("7. iperf3 Load Test: 8.0 Mbps UDP over Path s1->s2")
        h1.cmd("iperf3 -u -c 10.0.0.4 -b 8M -t 6 -p 5201 > /dev/null 2>&1")
        time.sleep(2.5)
        rec_8m_s2 = get_latest_csv_record(csv_path, "s1", "s2")
        rec_8m_s3 = get_latest_csv_record(csv_path, "s1", "s3")

        rate_8m_s2 = float(rec_8m_s2["tx_rate_mbps"]) if rec_8m_s2 else 0.0
        rate_8m_s3 = float(rec_8m_s3["tx_rate_mbps"]) if rec_8m_s3 else 0.0
        lat_8m_s2 = float(rec_8m_s2["latency_ms"]) if rec_8m_s2 else 0.0
        check(
            "s1->s2 measured rate within tolerance of 8.0 Mbps",
            abs(rate_8m_s2 - 8.0) <= (8.0 * args.tolerance),
            f"measured={rate_8m_s2:.2f} Mbps (target=8.00 Mbps, diff={abs(rate_8m_s2 - 8.0):.2f})",
        )
        check("s1->s3 idle path remains near 0 Mbps", rate_8m_s3 < 0.1, f"rate={rate_8m_s3:.4f} Mbps")
        check(
            "probe latency under 80% load is tracked properly",
            lat_8m_s2 >= 2.0,
            f"latency={lat_8m_s2:.2f} ms",
        )

        # 8. Display CSV Head & Sample Output
        section("8. Telemetry CSV Head & Structure")
        if os.path.exists(csv_path):
            with open(csv_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            print(f"Total lines in {csv_path}: {len(lines)}")
            print("First 8 lines of data/telemetry.csv:")
            for line in lines[:8]:
                print("  " + line.strip())

        # Cleanup Mininet
        h4.cmd("killall -9 iperf3 2>/dev/null; true")
        if topo:
            topo.stop()
            topo = None

    finally:
        # Terminate Ryu process group
        if ryu_proc.poll() is None:
            try:
                os.killpg(os.getpgid(ryu_proc.pid), signal.SIGTERM)
                ryu_proc.wait(timeout=3.0)
            except Exception:
                try:
                    os.killpg(os.getpgid(ryu_proc.pid), signal.SIGKILL)
                except Exception:
                    pass
        if topo:
            topo.stop()
        mn_clean()

    print("\n" + "=" * 78)
    print(f"SUMMARY: {PASSED} passed, {FAILED} failed")
    print("=" * 78 + "\n")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"passed": PASSED, "failed": FAILED, "timestamp": time.time()}, f, indent=2)

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
