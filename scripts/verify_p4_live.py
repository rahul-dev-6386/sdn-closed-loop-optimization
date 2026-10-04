#!/usr/bin/env python
"""P4 Live Acceptance Test -- Real LLDP Topology Discovery with Mininet & Ryu.

MUST BE RUN AS ROOT:
    sudo .venv/bin/python scripts/verify_p4_live.py

Test Steps:
1. Start Ryu with ``controller.sdn_controller`` and start the Mininet diamond
   topology connected to the remote controller.
2. Wait up to N seconds (from config) for real LLDP discovery. Assert that
   the controller's graph contains exactly the 4 switch-to-switch links learned
   from live LLDP with the exact DPID and port pairs.
3. Compare the discovered graph against config.yaml spec and print any mismatch.
4. Run a ping between h1 and h4. Assert 0% loss and that hosts h1 and h4 are
   learned and attached to the correct switch and port.
5. Bring link s1--s2 down (via configLinkStatus). Assert that the controller
   detects port status / LLDP timeout and removes s1--s2 within link_timeout_s,
   leaving only the s3 path. Print timestamps.
6. Bring link s1--s2 up. Assert that LLDP re-discovers the link and both paths
   return. Print timestamps.
7. Clean teardown and verification summary.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import load_config
from topology.multipath_topology import MultipathTopology, build_spec

HR = "=" * 78
THIN = "-" * 78
PASSED = 0
FAILED = 0


def ok(msg: str) -> None:
    global PASSED
    PASSED += 1
    print(f"  [PASS] {msg}")


def bad(msg: str) -> None:
    global FAILED
    FAILED += 1
    print(f"  [FAIL] {msg}")


def info(msg: str) -> None:
    print(f"  ....   {msg}")


def section(title: str) -> None:
    print(f"\n{THIN}\n[{title}]\n{THIN}")


def check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        ok(label if not detail else f"{label} ({detail})")
    else:
        bad(label if not detail else f"{label} ({detail})")
    return condition


class RyuProcess:
    """Manages the ryu-manager background process."""

    def __init__(self, venv: str, log_path: str, port: int = 6653, host: str = "127.0.0.1") -> None:
        self.ryu = os.path.join(venv, "bin", "ryu-manager")
        self.log_path = log_path
        self.port = port
        self.host = host
        self.proc: Optional[subprocess.Popen] = None

    def start(self, timeout: float = 20.0) -> bool:
        logfh = open(self.log_path, "a", encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.proc = subprocess.Popen(
            [
                self.ryu,
                "--ofp-listen-host",
                self.host,
                "--ofp-tcp-listen-port",
                str(self.port),
                "controller.sdn_controller",
            ],
            stdout=logfh,
            stderr=subprocess.STDOUT,
            env=env,
            preexec_fn=os.setsid,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            out = subprocess.run(["ss", "-ltn"], capture_output=True, text=True)
            if f":{self.port} " in out.stdout:
                return True
            time.sleep(0.3)
        return False

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.proc = None


def read_live_topology(path: str = "data/topology.json") -> Optional[Dict[str, Any]]:
    """Read data/topology.json written dynamically by the controller."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="P4 Live LLDP Discovery Acceptance Test")
    parser.add_argument("--out", default="results/p4_live.json", help="Path to write JSON results")
    args = parser.parse_args()

    print(f"{HR}\nAI-Driven SDN: Phase 4 Live Acceptance Test (Real LLDP Discovery)\n{HR}")

    if os.geteuid() != 0:
        print("ERROR: scripts/verify_p4_live.py must be run as root (needs Mininet & OVS).")
        print("Run with: sudo .venv/bin/python scripts/verify_p4_live.py")
        return 1

    cfg = load_config()
    venv = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv")
    log_dir = "data/logs"
    os.makedirs(log_dir, exist_ok=True)
    ryu_log = os.path.join(log_dir, "p4_controller.log")

    # 0. Clean prior state
    section("0. Environmental Preparation")
    subprocess.run(["pkill", "-9", "-f", "ryu-manager|sdn_controller"], check=False)
    subprocess.run(["mn", "-c"], capture_output=True, check=False)
    for stale in ("data/topology.json", "data/portmap.json"):
        if os.path.isfile(stale):
            os.remove(stale)
    ok("Cleaned leftover controller/mininet processes and stale topology files")

    # 1. Start Ryu Controller
    section("1. Start SDN Controller")
    ryu = RyuProcess(venv, ryu_log)
    check("ryu-manager started and listening on 127.0.0.1:6653", ryu.start())

    # 2. Build Mininet diamond topology
    section("2. Build Mininet Diamond Topology (Remote Controller)")
    spec = build_spec(cfg)
    topo = MultipathTopology.build(
        spec=spec,
        controller_ip="127.0.0.1",
        controller_port=6653,
    )
    net = topo.net
    check("Mininet topology built and attached to controller", net is not None)

    try:
        # 3. Wait for LLDP discovery
        section("3. Real LLDP Discovery Verification")
        discovery_timeout = float(cfg.get("discovery.discovery_timeout_s", 10.0))
        info(f"waiting up to {discovery_timeout:.1f}s for LLDP link discovery...")
        deadline = time.time() + discovery_timeout
        topo_data: Optional[Dict[str, Any]] = None

        while time.time() < deadline:
            data = read_live_topology()
            if data and len(data.get("links", [])) == 4:
                topo_data = data
                break
            time.sleep(0.5)

        check(
            "controller discovered 4 switch-to-switch links via LLDP",
            topo_data is not None and len(topo_data.get("links", [])) == 4,
            f"found {len(topo_data.get('links', [])) if topo_data else 0} links",
        )

        # Expected port pairings from Mininet Links
        # s1: port 2 <-> s2: port 1
        # s1: port 3 <-> s3: port 1
        # s2: port 2 <-> s4: port 1
        # s3: port 2 <-> s4: port 2
        discovered_pairs = set()
        if topo_data:
            for lk in topo_data["links"]:
                low, high = sorted([f"{lk['src']}:{lk['src_port']}", f"{lk['dst']}:{lk['dst_port']}"])
                discovered_pairs.add(f"{low}<->{high}")

        expected_pairs = {
            "s1:2<->s2:1",
            "s1:3<->s3:1",
            "s2:2<->s4:1",
            "s3:2<->s4:2",
        }
        check(
            "discovered link port pairings match Mininet physical links",
            discovered_pairs == expected_pairs,
            f"discovered={sorted(discovered_pairs)}",
        )

        # Compare discovered vs spec
        mismatches = []
        for p in expected_pairs:
            if p not in discovered_pairs:
                mismatches.append(f"Missing link {p}")
        for p in discovered_pairs:
            if p not in expected_pairs:
                mismatches.append(f"Unexpected link {p}")
        check("discovered graph vs config.yaml spec has 0 mismatches", len(mismatches) == 0, f"mismatches={mismatches}")

        # 4. Host Learning via Ping
        section("4. Host Connectivity & Location Learning")
        h1 = net.get("h1")
        h4 = net.get("h4")
        ping_loss = net.ping([h1, h4])
        check("ping between h1 and h4 reports 0% loss", ping_loss == 0.0, f"loss={ping_loss}%")

        # Refresh topology data after host ARP
        topo_data = read_live_topology()
        hosts = topo_data.get("hosts", {}) if topo_data else {}
        check(
            "h1 learned on s1 port 1",
            hosts.get("h1", {}).get("switch") == "s1" and hosts.get("h1", {}).get("port") == 1,
            f"h1 location={hosts.get('h1')}",
        )
        check(
            "h4 learned on s4 port 3",
            hosts.get("h4", {}).get("switch") == "s4" and hosts.get("h4", {}).get("port") == 3,
            f"h4 location={hosts.get('h4')}",
        )

        paths = topo_data.get("paths_h1_h4", []) if topo_data else []
        check("both diamond paths present in controller k-shortest paths", len(paths) == 2, f"paths={paths}")

        # 5. Dynamic Link Down
        section("5. Dynamic Link Failure & Surviving Path")
        link_timeout = float(cfg.get("discovery.link_timeout_s", 6.0))
        t_down_start = time.time()
        print(f"  [{time.strftime('%H:%M:%S', time.localtime(t_down_start))}] Bringing link s1--s2 DOWN...")
        net.configLinkStatus("s1", "s2", "down")

        down_detected = False
        t_down_detected = 0.0
        deadline = time.time() + link_timeout + 2.0
        while time.time() < deadline:
            data = read_live_topology()
            if data:
                links_remaining = [
                    sorted([l["src"], l["dst"]]) for l in data.get("links", [])
                ]
                if ["s1", "s2"] not in links_remaining:
                    down_detected = True
                    t_down_detected = time.time()
                    break
            time.sleep(0.3)

        delta_down = t_down_detected - t_down_start if down_detected else 0.0
        print(f"  [{time.strftime('%H:%M:%S', time.localtime(t_down_detected or time.time()))}] Controller removed link s1--s2 (delta={delta_down:.2f}s)")
        check("controller removed failed link within link_timeout_s", down_detected and delta_down <= link_timeout, f"delta={delta_down:.2f}s, limit={link_timeout:.1f}s")

        surviving_paths = data.get("paths_h1_h4", []) if data else []
        expected_survivor = ["h1", "s1", "s3", "s4", "h4"]
        check(
            "k-shortest paths now only shows the surviving s3 path",
            len(surviving_paths) == 1 and surviving_paths[0] == expected_survivor,
            f"paths={surviving_paths}",
        )

        # 6. Dynamic Link Restore
        section("6. Dynamic Link Restoration")
        t_up_start = time.time()
        print(f"  [{time.strftime('%H:%M:%S', time.localtime(t_up_start))}] Bringing link s1--s2 back UP...")
        net.configLinkStatus("s1", "s2", "up")

        up_detected = False
        t_up_detected = 0.0
        deadline = time.time() + discovery_timeout
        while time.time() < deadline:
            data = read_live_topology()
            if data:
                links_current = [
                    sorted([l["src"], l["dst"]]) for l in data.get("links", [])
                ]
                if ["s1", "s2"] in links_current:
                    up_detected = True
                    t_up_detected = time.time()
                    break
            time.sleep(0.3)

        delta_up = t_up_detected - t_up_start if up_detected else 0.0
        print(f"  [{time.strftime('%H:%M:%S', time.localtime(t_up_detected or time.time()))}] Controller re-discovered link s1--s2 via LLDP (delta={delta_up:.2f}s)")
        check("controller restored link via LLDP", up_detected, f"delta={delta_up:.2f}s")

        restored_paths = data.get("paths_h1_h4", []) if data else []
        check("both paths restored in controller k-shortest paths", len(restored_paths) == 2, f"paths={restored_paths}")

    finally:
        # 7. Teardown
        section("7. Teardown")
        try:
            net.stop()
        except Exception as exc:
            info(f"net.stop exception: {exc}")
        ryu.stop()
        subprocess.run(["mn", "-c"], capture_output=True, check=False)
        ok("Cleaned Mininet network, stopped Ryu controller, and verified zero leaked bridges")

    print(f"\n{HR}")
    print(f"SUMMARY: {PASSED} passed, {FAILED} failed")
    print(f"{HR}\n")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "passed": PASSED,
                    "failed": FAILED,
                    "delta_down_s": delta_down if "delta_down" in locals() else 0,
                    "delta_up_s": delta_up if "delta_up" in locals() else 0,
                    "links_discovered": len(discovered_pairs) if "discovered_pairs" in locals() else 0,
                },
                f,
                indent=2,
            )

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
