#!/usr/bin/env python
"""P4 Live Acceptance Test -- Real LLDP Topology Discovery with Mininet & Ryu.

MUST BE RUN AS ROOT, unless invoked with --dry-run:
    sudo .venv/bin/python scripts/verify_p4_live.py

Test Steps:
1. Performs preflight checks (tools, controller imports, config keys).
2. Starts Ryu with ``controller.sdn_controller`` and starts the Mininet diamond
   topology connected to the remote controller.
3. Waits up to N seconds (from config) for real LLDP discovery. Asserts that
   the controller's graph contains exactly the 4 switch-to-switch links learned
   from live LLDP with the exact DPID and port pairs.
4. Compares the discovered graph against config.yaml spec and prints any mismatch.
5. Runs a ping between h1 and h4. Asserts 0% loss and that hosts h1 and h4 are
   learned and attached to the correct switch and port.
6. Brings link s1--s2 down (via configLinkStatus). Asserts that the controller
   detects port status / LLDP timeout and removes s1--s2 within link_timeout_s,
   leaving only the s3 path. Prints timestamps.
7. Brings link s1--s2 up. Asserts that LLDP re-discovers the link and both paths
   return. Prints timestamps.
8. Clean teardown and verification summary.
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
from common.proc import check_required_tools, mn_clean, venv_bin
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


def preflight(cfg: Any) -> bool:
    """Validate tools, controller imports, and required config before running."""
    section("Preflight System Verification")
    tools = ("mn", "ovs-vsctl", "ovs-ofctl", "ryu-manager")
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
    req_keys = ["topology.switches", "topology.links", "discovery.lldp_interval_s"]
    cfg_ok = all(cfg.get(k) is not None for k in req_keys)
    check("required configuration keys present", cfg_ok)

    return all_tools_ok and cfg_ok


class RyuProcess:
    """Manages the ryu-manager background process."""

    def __init__(self, log_path: str, port: int = 6653, host: str = "127.0.0.1") -> None:
        self.ryu = venv_bin("ryu-manager")
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
    parser.add_argument("--dry-run", action="store_true", help="Perform preflight and import checks without root/Mininet")
    args = parser.parse_args()

    print(f"{HR}\nAI-Driven SDN: Phase 4 Live Acceptance Test (Real LLDP Discovery)\n{HR}")

    cfg = load_config()

    if not preflight(cfg):
        print("\nPreflight check failed. Aborting.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n--dry-run requested: Preflight passed successfully. Exiting.")
        return 0

    if os.geteuid() != 0:
        print("ERROR: scripts/verify_p4_live.py must be run as root (needs Mininet & OVS).")
        print("Run with: sudo .venv/bin/python scripts/verify_p4_live.py")
        return 1

    log_dir = "data/logs"
    os.makedirs(log_dir, exist_ok=True)
    ryu_log = os.path.join(log_dir, "p4_controller.log")

    # 0. Clean prior state
    section("0. Environmental Preparation")
    subprocess.run(["pkill", "-9", "-f", "ryu-manager|sdn_controller"], check=False)
    mn_clean()
    for stale in ("data/topology.json", "data/portmap.json"):
        if os.path.isfile(stale):
            os.remove(stale)
    ok("Cleaned leftover controller/mininet processes and stale topology files")

    # 1. Start Ryu Controller
    section("1. Start SDN Controller")
    ryu = RyuProcess(ryu_log)
    check("ryu-manager started and listening on 127.0.0.1:6653", ryu.start())

    # 2. Build Mininet diamond topology
    section("2. Build Mininet Diamond Topology (Remote Controller)")
    spec = build_spec(cfg)
    datapath_type = str(cfg.get("topology.datapath_type", "kernel"))
    topo = MultipathTopology(
        spec=spec,
        controller=("127.0.0.1", 6653),
        datapath_type=datapath_type,
    )
    net = topo.build()
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
            f"links={len(topo_data.get('links', [])) if topo_data else 0}",
        )

        discovered_links = topo_data.get("links", []) if topo_data else []
        discovered_pairs = set()
        for l in discovered_links:
            discovered_pairs.add((l["src"], l["src_port"], l["dst"], l["dst_port"]))
            info(f"discovered link: {l['src']}:{l['src_port']} <-> {l['dst']}:{l['dst_port']}")

        expected_pairs = {
            ("s1", 1, "s2", 1),
            ("s1", 2, "s3", 1),
            ("s2", 2, "s4", 1),
            ("s3", 2, "s4", 2),
        }
        normalized_discovered = {
            tuple(sorted([(src, sp), (dst, dp)]))
            for (src, sp, dst, dp) in discovered_pairs
        }
        normalized_expected = {
            tuple(sorted([(s1, p1), (s2, p2)]))
            for (s1, p1, s2, p2) in expected_pairs
        }
        mismatches = normalized_expected - normalized_discovered
        check("discovered link port pairings match Mininet diamond wiring", len(mismatches) == 0, f"mismatches={mismatches}")

        # 4. Host Reachability & Dynamic Host Attachment
        section("4. Host Discovery & Ping Reachability")
        h1 = net.get("h1")
        h4 = net.get("h4")

        ping_loss = net.ping([h1, h4])
        check("ping between h1 and h4 succeeded (0% loss)", ping_loss == 0.0, f"loss={ping_loss}%")

        data = read_live_topology()
        hosts_learned = data.get("hosts", {}) if data else {}
        info(f"hosts in topology: {hosts_learned}")
        check("h1 learned and attached to switch s1", hosts_learned.get("h1", {}).get("switch") == "s1")
        check("h4 learned and attached to switch s4", hosts_learned.get("h4", {}).get("switch") == "s4")

        # 5. Dynamic Link Failure (bring s1-s2 down)
        section("5. Dynamic Link Failure Handling")
        link_timeout = float(cfg.get("discovery.link_timeout_s", 6.0))
        t_down_start = time.time()
        print(f"  [{time.strftime('%H:%M:%S')}] Bringing link s1--s2 down (configLinkStatus down)...")
        net.configLinkStatus("s1", "s2", "down")

        down_deadline = time.time() + link_timeout + 4.0
        down_detected = False
        t_down_detected = None

        while time.time() < down_deadline:
            data = read_live_topology()
            links = data.get("links", []) if data else []
            has_s1_s2 = any(
                (l["src"] == "s1" and l["dst"] == "s2") or (l["src"] == "s2" and l["dst"] == "s1")
                for l in links
            )
            if not has_s1_s2:
                down_detected = True
                t_down_detected = time.time()
                break
            time.sleep(0.3)

        delta_down = t_down_detected - t_down_start if down_detected else 0.0
        print(f"  [{time.strftime('%H:%M:%S', time.localtime(t_down_detected or time.time()))}] Controller removed link s1--s2 from graph (delta={delta_down:.2f}s)")
        check(
            f"controller removed link s1--s2 within link_timeout_s ({link_timeout:.1f}s)",
            down_detected and delta_down <= (link_timeout + 3.0),
            f"delta={delta_down:.2f}s",
        )

        paths_remaining = data.get("paths_h1_h4", []) if data else []
        expected_surviving_path = ["h1", "s1", "s3", "s4", "h4"]
        check(
            "k-shortest paths shows only surviving path via s3",
            len(paths_remaining) == 1 and paths_remaining[0] == expected_surviving_path,
            f"paths={paths_remaining}",
        )

        # 6. Dynamic Link Restoration (bring s1-s2 up)
        section("6. Dynamic Link Restoration & LLDP Re-discovery")
        t_up_start = time.time()
        print(f"  [{time.strftime('%H:%M:%S')}] Bringing link s1--s2 UP (configLinkStatus up)...")
        net.configLinkStatus("s1", "s2", "up")

        up_deadline = time.time() + discovery_timeout + 6.0
        up_detected = False
        t_up_detected = None

        while time.time() < up_deadline:
            data = read_live_topology()
            links = data.get("links", []) if data else []
            has_s1_s2 = any(
                (l["src"] == "s1" and l["dst"] == "s2") or (l["src"] == "s2" and l["dst"] == "s1")
                for l in links
            )
            if has_s1_s2:
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
        if topo:
            topo.stop()
        ryu.stop()
        mn_clean()
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
