#!/usr/bin/env python3
"""Diagnose why the continuous-load flows carry no traffic (P6 triage).

Reuses the project's own topology builder -- the one the collector uses and
which is known to work -- then launches the iperf3 servers and clients exactly
as ``run_profiled_load_on_hosts`` does, checking at each stage whether sockets
exist and bytes actually move.

Each stage prints PASS or FAIL, so the failing one is unambiguous.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.config import load_config  # noqa: E402
from common.proc import venv_bin  # noqa: E402

FAILURES: list = []


def stage(name: str, ok: bool, detail: str = "") -> None:
    """Record and print one stage verdict."""
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    """Run the triage and return an exit code."""
    from topology.multipath_topology import (
        MultipathTopology,
        StaticFlowInstaller,
        build_flow_plan,
        build_spec,
    )

    cfg = load_config()
    iperf = venv_bin("iperf3")
    print(f"iperf3: {iperf}")

    print("\n[0] building the project topology (controller-less)")
    topo = MultipathTopology(build_spec(cfg), controller=None,
                             datapath_type=str(cfg.get(
                                 "topology.datapath_type", "kernel")))
    net = topo.build()
    time.sleep(2.0)
    h1, h4 = net.get("h1"), net.get("h4")

    try:
        # Route traffic the way the collector does, so ARP is resolved.
        chosen = ["h1", "s1", "s2", "s4", "h4"]
        rules = build_flow_plan(spec=topo.spec, path=chosen,
                               port_of=lambda sw, nb: topo.port_of(sw, nb),
                               bidirectional=True)
        StaticFlowInstaller().install(rules)
        net.ping([h1, h4])

        print("\n[1] iperf3 server startup")
        h4.cmd("killall -9 iperf3 2>/dev/null; true")
        time.sleep(0.5)
        for port in (5201, 5202):
            h4.cmd(f"{iperf} -s -D -p {port}")
        time.sleep(1.2)
        procs = h4.cmd("ps ax 2>/dev/null | grep iperf3 | grep -v grep")
        print("    iperf3 processes on h4:", len(procs.splitlines()))
        for ln in procs.splitlines():
            print("     ", ln.strip()[:90])
        listening = h4.cmd("ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null")
        found = [ln.strip() for ln in listening.splitlines()
                 if "520" in ln]
        stage("two servers listening", len(found) >= 2,
              f"{len(found)} socket(s): {found}")

        print("\n[2] one client, backgrounded with &")
        h1.cmd("killall -9 iperf3 2>/dev/null; true")
        before = _rx_bytes(h4)
        h1.cmd(f"{iperf} -u -c 10.0.0.4 -b 8.0M -t 6 -p 5201 "
               f">/dev/null 2>&1 &")
        time.sleep(4.0)
        after = _rx_bytes(h4)
        stage("single client carries traffic", after > before,
              f"{before} -> {after} bytes ({(after - before) * 8 / 4e6:.1f} Mbps)")
        h1.cmd("killall -9 iperf3 2>/dev/null; true")

        print("\n[3] six servers, one per port")
        h4.cmd("killall -9 iperf3 2>/dev/null; true")
        time.sleep(0.3)
        for port in range(5201, 5207):
            h4.cmd(f"{iperf} -s -D -p {port}")
        time.sleep(1.5)
        listening = h4.cmd("ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null")
        found = [ln.strip() for ln in listening.splitlines()
                 if any(str(p) in ln for p in range(5201, 5207))]
        stage("six servers listening", len(found) >= 6,
              f"{len(found)} socket(s)")

        print("\n[4] six concurrent clients at 2 Mbps each")
        h1.cmd("killall -9 iperf3 2>/dev/null; true")
        before = _rx_bytes(h4)
        for port in range(5201, 5207):
            h1.cmd(f"{iperf} -u -c 10.0.0.4 -b 2.0M -t 8 -p {port} "
                   f">/dev/null 2>&1 &")
            time.sleep(0.4)
        time.sleep(6.0)
        after = _rx_bytes(h4)
        mbps = (after - before) * 8 / 6e6
        stage("concurrent clients carry traffic", mbps > 3.0,
              f"{mbps:.1f} Mbps measured (12 Mbps expected)")

        print("\n[5] a single long-lived client (the ramp base flow)")
        h1.cmd("killall -9 iperf3 2>/dev/null; true")
        before = _rx_bytes(h4)
        h1.cmd(f"{iperf} -u -c 10.0.0.4 -b 2.0M -t 10 -p 5201 "
               f">/dev/null 2>&1 &")
        time.sleep(6.0)
        after = _rx_bytes(h4)
        stage("long-lived client survives", after > before,
              f"{(after - before) * 8 / 6e6:.1f} Mbps")
    finally:
        try:
            topo.stop()
        except Exception:  # noqa: BLE001
            pass

    print("\n" + "=" * 62)
    if FAILURES:
        print("FAILED STAGES: " + ", ".join(FAILURES))
        return 1
    print("All stages passed.")
    return 0


def _rx_bytes(node) -> int:
    """Bytes received on the node's first interface."""
    for line in node.cmd("cat /proc/net/dev").splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 10 and parts[0].rstrip(":") == "lo":
            continue
        if len(parts) >= 10:
            try:
                return int(parts[1])
            except ValueError:
                return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
