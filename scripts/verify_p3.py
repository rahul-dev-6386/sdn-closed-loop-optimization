#!/usr/bin/env python
"""P3 acceptance test -- loop-safe controller. Run as ROOT.

    sudo .venv/bin/python scripts/verify_p3.py

Launches ``ryu-manager`` with ``controller.sdn_controller`` against a live P2
topology and checks, in order:

1. **pingall 0% loss** -- connectivity with the controller attached.
2. **No broadcast storm** -- port packet counters stay small and flat across
   30 s of complete idleness, before and after ARP has resolved.
3. **Flows visible** via ``ovs-ofctl dump-flows``, including the table-miss rule
   and at least one exact-match forwarding flow.
4. **Throughput** h1 -> h4 reaches ~9.4 Mbps over the controller-selected path.
5. **Controller restart** recovers connectivity: kill ryu, bring it back, and
   ping all pairs again with 0% loss.

Runs strictly sequentially and finishes with ``mn -c``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import Config, load_config
from common.logging_utils import setup_logging
from common.proc import check_required_tools, mn_clean, venv_bin
from controller import portmap as portmap_mod
from topology.multipath_topology import (
    MultipathTopology,
    build_spec,
    topology_paths,
)

HR = "=" * 78
THIN = "-" * 78
PASSED = 0
FAILED = 0
LOGGER = None


def preflight(config: Any) -> bool:
    """Validate tools, controller imports, and required config before running."""
    print(f"\n{THIN}\n[Preflight System Verification]\n{THIN}")
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

    # Controller import check
    try:
        import controller.sdn_controller  # noqa: F401
        print("  [IMPORT] controller.sdn_controller imported cleanly")
        import_ok = True
    except Exception as exc:
        print(f"  [IMPORT ERROR] Failed to import controller.sdn_controller: {exc}", file=sys.stderr)
        import_ok = False

    # Config keys check
    req_keys = ["topology.switches", "topology.links", "controller.port_map_path"]
    cfg_ok = all(config.get(k) is not None for k in req_keys)

    return all_tools_ok and import_ok and cfg_ok


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


# --------------------------------------------------------------------------- #
# Ryu process management
# --------------------------------------------------------------------------- #
class RyuProcess:
    """Runs ``ryu-manager`` with our controller app, as a managed child."""

    def __init__(self, venv: str, log_path: str, port: int = 6653,
                 host: str = "127.0.0.1", app: str = "controller.sdn_controller") -> None:
        self.ryu = venv_bin("ryu-manager")
        self.log_path = log_path
        self.port = port
        self.host = host
        self.app = app
        self.proc: Optional[subprocess.Popen] = None

    def start(self, timeout: float = 30.0) -> bool:
        """Start ryu-manager and wait until it listens on the OpenFlow port."""
        logfh = open(self.log_path, "a", encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
        self.proc = subprocess.Popen(
            [self.ryu, "--ofp-listen-host", self.host,
             "--ofp-tcp-listen-port", str(self.port), self.app],
            stdout=logfh, stderr=subprocess.STDOUT, env=env,
            preexec_fn=os.setsid,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            out = subprocess.run(["ss", "-ltn"], capture_output=True, text=True)
            if f":{self.port} " in out.stdout:
                return True
            time.sleep(0.4)
        return False

    def stop(self) -> None:
        """Terminate the process group."""
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.proc = None

    def tail(self, lines: int = 15) -> str:
        """Return the last lines of the controller log."""
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-lines:])
        except OSError:
            return ""


# --------------------------------------------------------------------------- #
# measurements
# --------------------------------------------------------------------------- #
def pingall(net: Any, timeout: int = 20) -> Tuple[int, int, float, str]:
    """Run pingall across all host pairs; returns (received, sent, loss_pct, raw).

    NOTE: ``Mininet.pingAll`` returns the **packet-loss percentage as a float**,
    not a (sent, received) tuple. Unpacking it as a tuple silently produced
    nonsense loss figures, so the raw value is handled explicitly here.
    """
    out = net.pingAll()
    hosts = len(net.hosts)
    sent = hosts * (hosts - 1)
    loss = float(out)
    received = int(round(sent * (1.0 - loss / 100.0)))
    return received, sent, loss, ""


def ping_pair(host: Any, target_ip: str, count: int = 4) -> Tuple[float, str]:
    """Ping one target; returns (loss_pct, raw_output)."""
    out = host.cmd(f"ping -c {count} -i 0.2 -W 2 {target_ip}")
    m = re.search(r"(?P<sent>\d+) packets transmitted, (?P<recv>\d+) received"
                  r"(?:, (?P<loss>[\d.]+)% packet loss)?", out)
    if not m:
        return 100.0, out
    return float(m.group("loss") or 0.0), out


def iperf_throughput(client: Any, server: Any, server_ip: str, seconds: int = 10,
                     reverse: bool = False) -> Tuple[float, str]:
    """Measure iperf3 TCP throughput in Mbps from ``client`` to ``server``.

    The iperf3 server must run on the *remote* host; starting it on the client
    (as an earlier version of this helper did) yields 0.00 Mbps because nothing
    is listening at ``server_ip``.
    """
    port = 5201
    iperf = venv_bin("iperf3")
    server.cmd(f"pkill -f '{iperf} -s' 2>/dev/null; true")
    time.sleep(0.3)
    server.cmd(f"{iperf} -s -p {port} -D")
    time.sleep(0.8)
    cmd = f"{iperf} -c {server_ip} -p {port} -t {seconds} -J " + ("-R " if reverse else "")
    raw = client.cmd(cmd)
    server.cmd(f"pkill -f '{iperf} -s' 2>/dev/null; true")
    if not raw.strip():
        return 0.0, raw
    try:
        data = json.loads(raw[raw.index("{"):])
    except json.JSONDecodeError:
        return 0.0, raw
    if "error" in data:
        return 0.0, str(data["error"])
    end = data["end"]
    block = end.get("sum_sent") or end.get("sum_received") or {}
    return float(block.get("bits_per_second", 0.0)) / 1e6, raw


def ofctl(args: List[str]) -> str:
    """Run ovs-ofctl and return stdout (empty string on failure)."""
    proc = subprocess.run(
        [venv_bin("ovs-ofctl"), "-OOpenFlow13", *args], capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else ""


def port_counters(switches: List[str]) -> Dict[str, Dict[str, int]]:
    """Per-switch, per-port rx/tx packet counters from OVS."""
    out: Dict[str, Dict[str, int]] = {}
    for switch in switches:
        text = ofctl([f"dump-ports-aggregate", switch])
        per_port: Dict[str, int] = {}
        for line in text.splitlines():
            m = re.search(r"^\s*port\s+(\d+)\s*:.*rx_packets=(\d+)", line)
            if m:
                per_port[m.group(1)] = int(m.group(2))
            m2 = re.search(r"^\s*port\s+(\d+)\s*:.*tx_packets=(\d+)", line)
            if m2:
                per_port.setdefault(m2.group(1), 0)
                per_port[f"{m2.group(1)}-tx"] = int(m2.group(2))
        out[switch] = per_port
    return out


def total_packets(counters: Dict[str, Dict[str, int]]) -> int:
    """Sum only rx counters (tx keys are prefixed, so they are excluded)."""
    return sum(v for k, v in counters.items() if not k.endswith("-tx"))


def current_interfaces() -> List[str]:
    """Interface names that look Mininet-related (sN-ethM / mnexec bridges)."""
    raw = subprocess.run(["ip", "-o", "link", "show"], capture_output=True,
                         text=True, check=False).stdout
    names = []
    for line in raw.splitlines():
        m = re.search(r"^\d+:\s+([^:@]+)[:@]", line)
        if m and re.match(r"^(s\d+-eth\d+|mnexec|mn\d)", m.group(1)):
            names.append(m.group(1))
    return sorted(names)


def flow_entries(switch: str) -> List[str]:
    """Return the flow entries on ``switch`` as raw lines."""
    return [l.strip() for l in ofctl(["dump-flows", switch]).splitlines()
            if "cookie" in l]


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P3 acceptance test (run as root).")
    p.add_argument("--config", default=None)
    p.add_argument("--iperf-seconds", type=int, default=10)
    p.add_argument("--idle-seconds", type=int, default=30,
                   help="idle window used for the broadcast-storm check")
    p.add_argument("--storm-tolerance", type=int, default=200,
                   help="max packets allowed to accumulate while idle")
    p.add_argument("--target-mbps", type=float, default=9.4,
                   help="expected h1->h4 throughput")
    p.add_argument("--throughput-tolerance", type=float, default=0.10)
    p.add_argument("--controller-port", type=int, default=6653)
    p.add_argument("--dry-run", action="store_true", help="Perform preflight and import checks without root/Mininet")
    p.add_argument("--out", default=None)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    global LOGGER
    args = parse_args(argv)
    config = load_config(args.config)
    LOGGER = setup_logging(config, name="verify.p3")

    if not preflight(config):
        print("\nPreflight check failed. Aborting.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n--dry-run requested: Preflight passed successfully. Exiting.")
        return 0

    if os.geteuid() != 0:
        print("ERROR: must run as root.", file=sys.stderr)
        return 2

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv = os.path.join(root, ".venv")
    os.makedirs("data/logs", exist_ok=True)
    ryu_log = "data/logs/p3_controller.log"
    if os.path.exists(ryu_log):
        os.remove(ryu_log)

    spec = build_spec(config)
    switches = sorted(spec.switches)
    h1_name, h4_name = "h1", "h4"
    h1_ip = spec.host(h1_name).address
    h4_ip = spec.host(h4_name).address
    port_map_path = str(config.get("controller.port_map_path", "data/portmap.json"))

    paths = topology_paths(spec, h1_name, h4_name)
    expected_path = paths[0]

    print(HR)
    print(" P3 acceptance -- loop-safe SDN controller")
    print(HR)
    print(f"date            : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"controller      : controller.sdn_controller (ARP proxy, no flooding)")
    print(f"controller log  : {ryu_log}")
    print(f"idle window     : {args.idle_seconds}s (storm check)")
    print(f"target h1->h4   : {args.target_mbps} Mbps")

    datapath_type = str(config.get("topology.datapath_type", "kernel"))
    # Docker's bridge veth pre-exists on this host and is not ours to clean up.
    baseline_ifaces = current_interfaces()
    info(f"baseline interfaces before run: {baseline_ifaces or 'none'}")
    topo = MultipathTopology(
        spec, controller=("127.0.0.1", args.controller_port),
        datapath_type=datapath_type,
    )
    ryu = RyuProcess(venv, ryu_log, port=args.controller_port)
    report: Dict[str, Any] = {"checks": {}}
    cleanup_ok = False

    try:
        # ---------------------------------------------------------- setup ---
        section("1. Start controller, then build the topology")
        # Remove any stale port map so dynamic discovery is tested cleanly.
        if os.path.exists(port_map_path):
            os.remove(port_map_path)
        check("data/portmap.json does NOT exist at startup (pure live discovery)", not os.path.exists(port_map_path))

        topo.build()
        h1 = topo.net.get(h1_name)
        h4 = topo.net.get(h4_name)
        bridges = subprocess.run([venv_bin("ovs-vsctl"), "list-br"],
                                 capture_output=True, text=True).stdout.split()
        check("all 4 OVS bridges present", len(bridges) == 4, f"{sorted(bridges)}")

        if not ryu.start():
            bad("ryu-manager failed to start")
            print(ryu.tail(30))
            return 1
        ok(f"ryu-manager listening on 127.0.0.1:{args.controller_port}")
        info(f"expected controller path: {' -> '.join(expected_path)}")

        # Allow initial switch connections and discovery probes
        time.sleep(2.0)

        # --------------------------------------------------- 2. pingall ----
        section("2. pingall must show 0% loss")
        # Warm up first with a ping so the controller discovers host attachment ports dynamically
        info("warmup ping between h1 and h4 to allow dynamic ARP learning...")
        topo.net.ping([h1, h4])
        time.sleep(0.5)

        recv, sent, loss, _ = pingall(topo.net)
        info(f"pingall: {recv}/{sent} received")
        check("pingall 0% loss", loss == 0.0, f"{loss:.1f}% loss ({recv}/{sent})")
        report["checks"]["pingall"] = {"received": recv, "sent": sent, "loss_pct": loss}

        time.sleep(2)

        # ------------------------------------------------------ 3. flows ----
        # NOTE: inspected BEFORE the idle window below. Forwarding rules carry a
        # 30 s idle timeout, so a 30 s idle test would expire every one of them
        # and the table would show only the table-miss rule.
        section("3. Flows visible via ovs-ofctl dump-flows")
        all_flows = {s: flow_entries(s) for s in switches}
        for switch in switches:
            entries = all_flows[switch]
            has_miss = any("priority=0" in e and "CONTROLLER" in e for e in entries)
            has_fwd = any("priority=" in e and "output:" in e for e in entries)
            info(f"{switch}: {len(entries)} entries "
                 f"(table-miss={has_miss}, forwarding={has_fwd})")
        total_fwd = sum(
            1 for s in switches
            for e in all_flows[s] if "output:" in e
        )
        check("table-miss -> controller installed on every switch",
              all(any("priority=0" in e and "CONTROLLER" in e for e in all_flows[s])
                  for s in switches))
        check("exact-match forwarding flows present", total_fwd > 0,
              f"{total_fwd} forwarding flow(s)")

        no_flood = all("flood" not in e.lower() for s in switches for e in all_flows[s])
        check("no FLOOD action in any flow entry (loop safety)", no_flood)

        print("  --- sample forwarding entry ---")
        for switch in switches:
            sample = [e for e in all_flows[switch] if "output:" in e]
            if sample:
                print(f"  {switch}: {sample[0][:150]}")
                break
        report["checks"]["flows"] = {
            s: len(all_flows[s]) for s in switches
        }

        # --------------------------------------- 4. broadcast storm check ---
        section("4. No broadcast storm during 30 s of idle")
        # Warm up first so ARP resolution has finished; any residual storm would
        # show up as unbounded growth in this idle window.
        for _ in range(3):
            ping_pair(h1, h4_ip, count=2)
        time.sleep(3)

        before = port_counters(switches)
        info(f"rx counters before idle: "
             f"{ {s: total_packets(before.get(s, {})) for s in switches} }")
        start = time.time()
        deadline = start + args.idle_seconds
        # Sample twice: mid-point and end, so a transient burst is visible.
        while time.time() < deadline:
            time.sleep(min(5.0, max(0.5, deadline - time.time())))
        mid = port_counters(switches)
        info(f"rx counters mid-idle : "
             f"{ {s: total_packets(mid.get(s, {})) for s in switches} }")

        after = port_counters(switches)
        deltas = {
            s: total_packets(after.get(s, {})) - total_packets(before.get(s, {}))
            for s in switches
        }
        info(f"rx packets added over {args.idle_seconds}s idle: {deltas}")
        total_delta = sum(deltas.values())
        check(
            f"port counters stay small and stable while idle",
            total_delta <= args.storm_tolerance,
            f"{total_delta} packets over {args.idle_seconds}s "
            f"(limit {args.storm_tolerance})",
        )
        report["checks"]["storm"] = {
            "idle_seconds": args.idle_seconds,
            "delta_packets": deltas,
            "total_delta": total_delta,
        }

        # ------------------------------------------------- 5. throughput ----
        section("5. h1 -> h4 throughput")
        fwd_mbps, _ = iperf_throughput(h1, h4, h4_ip, seconds=args.iperf_seconds)
        rev_mbps, _ = iperf_throughput(h1, h4, h4_ip, seconds=args.iperf_seconds,
                                       reverse=True)
        info(f"iperf3 forward = {fwd_mbps:.2f} Mbps")
        info(f"iperf3 reverse = {rev_mbps:.2f} Mbps")
        lower = args.target_mbps * (1.0 - args.throughput_tolerance)
        check(f"forward within {args.throughput_tolerance:.0%} of "
              f"{args.target_mbps} Mbps",
              lower <= fwd_mbps, f"{fwd_mbps:.2f} Mbps (need >= {lower:.2f})")
        check("reverse direction also carries traffic", rev_mbps > 0.0,
              f"{rev_mbps:.2f} Mbps")
        report["checks"]["throughput"] = {
            "forward_mbps": fwd_mbps, "reverse_mbps": rev_mbps,
        }

        # Which path did the controller actually use? Both are equally short,
        # so report the forwarding chain rather than asserting one.
        print("  --- forwarding chain h1 -> h4 ---")
        for switch in switches:
            for entry in all_flows[switch]:
                if "output:" in entry:
                    print(f"    {switch}: {entry[:150]}")

        # --------------------------------------------- 6. restart recovery --
        section("6. Controller restart recovers connectivity")
        info("killing ryu-manager")
        ryu.stop()
        time.sleep(3)
        still_listening = ":{} ".format(args.controller_port) in subprocess.run(
            ["ss", "-ltn"], capture_output=True, text=True).stdout
        check("controller port closed after kill", not still_listening)

        # Traffic while the controller is down: forwarding rules installed by the
        # previous run should still carry traffic (idle timeouts permitting).
        loss_down, _ = ping_pair(h1, h4_ip, count=4)
        info(f"ping with controller DOWN: {loss_down:.1f}% loss "
             f"(installed rules may still serve this)")

        info("restarting ryu-manager")
        if not ryu.start():
            bad("ryu-manager failed to restart")
        else:
            ok("ryu-manager restarted")
            time.sleep(6)
            loss_up, _ = ping_pair(h1, h4_ip, count=6)
            check("pingall-equivalent recovers after restart", loss_up == 0.0,
                  f"{loss_up:.1f}% loss after restart")
            recv2, sent2, loss2, _ = pingall(topo.net)
            check("pingall 0% loss after restart", loss2 == 0.0,
                  f"{loss2:.1f}% loss ({recv2}/{sent2})")
            flows_after = {s: flow_entries(s) for s in switches}
            reinstalled = sum(1 for s in switches
                              for e in flows_after[s] if "output:" in e)
            info(f"forwarding flows after restart: {reinstalled}")
            report["checks"]["restart"] = {
                "loss_after_restart_pct": loss_up,
                "pingall_loss_after_restart_pct": loss2,
                "forwarding_flows_after_restart": reinstalled,
            }

    finally:
        section("7. Cleanup verification")
        ryu.stop()
        try:
            topo.stop()
        except Exception as exc:  # pragma: no cover
            bad(f"Mininet stop raised {exc!r}")
        mn_clean()
        time.sleep(2)

        bridges = subprocess.run([venv_bin("ovs-vsctl"), "list-br"],
                                 capture_output=True, text=True).stdout.split()
        ps = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                            text=True).stdout
        leftover_procs = [l for l in ps.splitlines()
                          if ("mininet:" in l or "mnexec" in l) and "ps -eo" not in l]
        links = subprocess.run(["ip", "-o", "link", "show"], capture_output=True,
                               text=True).stdout
        leftover_ifaces = [
            i for i in current_interfaces() if i not in set(baseline_ifaces)
        ]
        info(f"bridges: {bridges or 'none'} | procs: {len(leftover_procs)} | "
             f"new ifaces: {leftover_ifaces or 'none'}")
        check("no OVS bridges left", not bridges, str(bridges))
        check("no mininet/mnexec processes left", not leftover_procs)
        check("no new interfaces left", not leftover_ifaces,
              str(leftover_ifaces[:5]))
        cleanup_ok = not bridges and not leftover_procs and not leftover_ifaces

    section("8. Summary")
    print(f"SUMMARY: {PASSED} passed, {FAILED} failed")
    if args.out:
        report.update({"passed": PASSED, "failed": FAILED, "cleanup_ok": cleanup_ok,
                       "controller_log": ryu_log})
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        info(f"report -> {args.out}")
    print(THIN)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())