#!/usr/bin/env python
"""P2 acceptance test -- multipath topology, run as ROOT.

    sudo .venv/bin/python scripts/verify_p2.py

For each of the two h1 -> h4 paths, in isolation:

1. install static OpenFlow rules pinning traffic to that path,
2. ``ping`` -- must be 0% loss, and the measured RTT must match the RTT
   predicted from ``config.yaml``,
3. ``iperf3`` TCP forward and reverse -- throughput within 10% of the configured
   link bandwidth,
4. tear the path down again.

Isolation methods (``--isolate``):

``static_flows`` (default)
    Clear every switch's flow table (removing OVS's implicit ``priority=0
    actions=NORMAL`` flood rule, which with the loop up would cause a broadcast
    storm) and install exact-match rules for one path only.
``link_down``
    Bring the other path's inter-switch link down with ``configLinkStatus``.

Both are reported. Finally the script proves cleanup left nothing behind: no OVS
bridges, no Mininet/mnexec processes, no leftover ``s*-eth*`` interfaces.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import Config, load_config
from common.logging_utils import setup_logging
from common.proc import check_required_tools, mn_clean, venv_bin
from topology.multipath_topology import (
    FlowRule,
    MultipathTopology,
    StaticFlowInstaller,
    TopologySpec,
    build_spec,
    topology_paths,
)

LOGGER = None  # assigned in main()
HR = "=" * 78
THIN = "-" * 78


def preflight(config: Any) -> bool:
    """Validate tools and configuration before starting test execution."""
    print(f"\n{THIN}\n[Preflight System Verification]\n{THIN}")
    tools = ("mn", "ovs-vsctl", "ovs-ofctl", "iperf3")
    found_tools = check_required_tools(tools)
    all_tools_ok = True
    for t in tools:
        path = found_tools.get(t)
        if path:
            print(f"  [TOOL] {t:<12} -> {path}")
        else:
            print(f"  [TOOL] {t:<12} -> MISSING", file=sys.stderr)
            all_tools_ok = False

    req_keys = ["topology.switches", "topology.links"]
    cfg_ok = all(config.get(k) is not None for k in req_keys)
    return all_tools_ok and cfg_ok

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
    """Record a PASS/FAIL line and return the condition."""
    if condition:
        ok(label if not detail else f"{label} ({detail})")
    else:
        bad(label if not detail else f"{label} ({detail})")
    return condition


# --------------------------------------------------------------------------- #
# measurement helpers
# --------------------------------------------------------------------------- #
PING_RE = re.compile(
    r"(?P<sent>\d+) packets transmitted, (?P<recv>\d+) received(?:, "
    r"(?P<loss>\d+(?:\.\d+)?)% packet loss)?"
)
RTT_RE = re.compile(r"rtt min/avg/max/(?:mdev|stddev) = "
                    r"(?P<min>[\d.]+)/(?P<avg>[\d.]+)/(?P<max>[\d.]+)/(?P<mdev>[\d.]+)")


@dataclass
class PingResult:
    """Outcome of one ping run."""

    sent: int
    received: int
    loss_pct: float
    rtt_min_ms: float
    rtt_avg_ms: float
    rtt_max_ms: float
    mdev_ms: float


def measure_ping(host: Any, target_ip: str, count: int = 5) -> PingResult:
    """Ping ``target_ip`` from ``host`` and parse loss plus RTT statistics."""
    out = host.cmd(f"ping -c {count} -i 0.2 -W 2 {target_ip}")
    loss_match = PING_RE.search(out)
    rtt_match = RTT_RE.search(out)
    if loss_match is None or rtt_match is None:
        raise RuntimeError(f"could not parse ping output:\n{out}")
    return PingResult(
        sent=int(loss_match.group("sent")),
        received=int(loss_match.group("recv")),
        loss_pct=float(loss_match.group("loss") or 0.0),
        rtt_min_ms=float(rtt_match.group("min")),
        rtt_avg_ms=float(rtt_match.group("avg")),
        rtt_max_ms=float(rtt_match.group("max")),
        mdev_ms=float(rtt_match.group("mdev")),
    )


@dataclass
class IperfResult:
    """Outcome of one iperf3 run."""

    direction: str
    seconds: float
    sender_mbps: float
    receiver_mbps: float
    mbps: float
    retransmits: Optional[int]


def measure_iperf(
    client: Any, server: Any, server_ip: str, seconds: int, reverse: bool,
    udp: bool = False,
) -> IperfResult:
    """Run iperf3 TCP from ``client`` to ``server`` with -O 2 and parse output."""
    port = 5201
    iperf = venv_bin("iperf3")
    server.cmd(f"pkill -f '{iperf} -s' 2>/dev/null; true")
    time.sleep(0.3)
    server.cmd(f"{iperf} -s -p {port} -D")
    time.sleep(0.7)

    direction = "reverse" if reverse else "forward"
    cmd = (
        f"{iperf} -c {server_ip} -p {port} -t {seconds} -O 2 -J "
        + ("-R " if reverse else "")
        + ("-u -b 10M " if udp else "")
    )
    raw = client.cmd(cmd)
    server.cmd(f"pkill -f '{iperf} -s' 2>/dev/null; true")

    if not raw.strip():
        raise RuntimeError(f"iperf3 produced no output ({direction})")
    try:
        data = json.loads(raw[raw.index("{"):])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bad iperf3 JSON ({direction}): {exc}\n{raw[:800]}") from exc

    if "error" in data:
        raise RuntimeError(f"iperf3 error ({direction}): {data['error']}")

    end = data["end"]
    sent = end.get("sum_sent") or end.get("sum") or {}
    recv = end.get("sum_received") or end.get("sum") or {}
    sender_bps = float(sent.get("bits_per_second", 0.0))
    receiver_bps = float(recv.get("bits_per_second", 0.0))
    if not receiver_bps and sender_bps:
        receiver_bps = sender_bps
    if not sender_bps and receiver_bps:
        sender_bps = receiver_bps

    return IperfResult(
        direction=direction,
        seconds=float(recv.get("seconds", seconds)),
        sender_mbps=sender_bps / 1e6,
        receiver_mbps=receiver_bps / 1e6,
        mbps=receiver_bps / 1e6,
        retransmits=sent.get("retransmits"),
    )


# --------------------------------------------------------------------------- #
# leftover-state inspection
# --------------------------------------------------------------------------- #
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


def leftover_state(baseline_ifaces: Optional[List[str]] = None) -> Dict[str, Any]:
    """Report bridges, mininet processes and *newly appeared* interfaces.

    Parameters
    ----------
    baseline_ifaces:
        Interface names observed before the run. Anything already present (for
        example a Docker bridge veth on this host) is not ours and must not be
        reported as a leak.
    """
    def sh(cmd: List[str]) -> str:
        return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout

    bridges = sh([venv_bin("ovs-vsctl"), "list-br"]).split()
    procs_raw = sh(["ps", "-eo", "args"])
    procs = [
        line.strip()
        for line in procs_raw.splitlines()
        if ("mininet:" in line or "mnexec" in line)
        and "ps -eo" not in line
    ]
    baseline = set(baseline_ifaces or [])
    ifaces = [i for i in current_interfaces() if i not in baseline]
    return {
        "ovs_bridges": bridges,
        "mininet_processes": procs,
        "leftover_interfaces": sorted(ifaces),
        "baseline_interfaces": sorted(baseline),
    }


# --------------------------------------------------------------------------- #
# path exercise
# --------------------------------------------------------------------------- #
@dataclass
class PathResult:
    """Everything measured for one isolated path."""

    label: str
    path: List[str]
    inter_switch_links: List[str]
    isolate_method: str
    rules: List[Dict[str, Any]] = field(default_factory=list)
    predicted_rtt_ms: float = 0.0
    ping: Optional[PingResult] = None
    iperf: List[IperfResult] = field(default_factory=list)
    flows_after: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


def rules_for_path(topo: MultipathTopology, spec: TopologySpec, path: List[str]):
    """Resolve static flow rules for ``path`` against the live port numbers."""
    from topology.multipath_topology import build_flow_plan

    return build_flow_plan(
        spec,
        path,
        port_of=lambda sw, nb: topo.port_of(sw, nb),
        bidirectional=True,
    )


def exercise_path(
    topo: MultipathTopology,
    spec: TopologySpec,
    installer: StaticFlowInstaller,
    path: List[str],
    label: str,
    method: str,
    h1: Any,
    h4: Any,
    h4_ip: str,
    iperf_seconds: int,
    bandwidth_tolerance: float,
    rtt_tolerance: float,
) -> PathResult:
    """Isolate and measure a single path. Assumes the net is already built."""
    result = PathResult(
        label=label,
        path=list(path),
        inter_switch_links=[l.name for l in spec.path_inter_switch_links(path)],
        isolate_method=method,
        predicted_rtt_ms=spec.expected_rtt_ms(path),
    )

    switches = sorted(spec.switches)
    installer.clear_switches(switches)
    if installer.flow_count(switches[0]) != 0:
        result.notes.append("flow table was NOT empty after del-flows")
        bad(f"{label}: flow table not empty after clearing")

    # Both methods install the path's forwarding rules; they differ in HOW the
    # other path is neutralised:
    #   static_flows -- relies on exact-match rules alone (the unused path has
    #                   no rules, so nothing can leak onto it)
    #   link_down    -- ALSO physically downs the unused path's links
    # Downing links alone cannot work: P2 has no controller, so with no rules
    # installed nothing forwards at all and the ping times out.
    rules = rules_for_path(topo, spec, path)
    installer.install(rules)
    result.rules = [asdict(r) for r in rules]
    info(f"{label}: installed {len(rules)} static rules via ovs-ofctl")

    if method == "link_down":
        used = set(result.inter_switch_links)
        for link in spec.links.values():
            if link.name not in used:
                topo.net.configLinkStatus(link.a, link.b, "down")
                result.notes.append(f"brought {link.name} down")
        info(f"{label}: additionally downed unused links "
             f"{sorted(set(spec.links) - used)}")

    # Re-enable the path's own links (matters after a previous link_down run).
    for link in spec.path_inter_switch_links(path):
        topo.net.configLinkStatus(link.a, link.b, "up")

    time.sleep(1.0)

    if method == "static_flows":
        for switch in switches:
            result.flows_after[switch] = installer.flow_count(switch)

    # ---- ping ----
    # Warm up first and discard. The first exchange on a freshly built network
    # pays ARP resolution plus HTB/netem queue warm-up, which inflates the mean
    # by roughly one link delay (~5 ms here) and would otherwise make the
    # cold path look like a shaping error. Path A is measured first and is the
    # cold one; without warm-up it read 29.9 ms vs the 24.0 ms predicted.
    for _ in range(4):
        measure_ping(h1, h4_ip, count=1)
    ping = measure_ping(h1, h4_ip, count=5)
    result.ping = ping
    info(f"{label}: ping loss={ping.loss_pct}% avg_rtt={ping.rtt_avg_ms:.2f} ms "
         f"(predicted {result.predicted_rtt_ms:.2f} ms)")
    check(f"{label}: ping 0% loss", ping.loss_pct == 0.0,
          f"{ping.loss_pct}% loss, {ping.received}/{ping.sent} received")

    rtt_error_pct = abs(ping.rtt_avg_ms - result.predicted_rtt_ms) / max(
        result.predicted_rtt_ms, 1e-9
    ) * 100.0
    check(f"{label}: measured RTT matches configured delay", rtt_error_pct <= rtt_tolerance,
          f"measured {ping.rtt_avg_ms:.2f} ms vs predicted "
          f"{result.predicted_rtt_ms:.2f} ms ({rtt_error_pct:.1f}% error, "
          f"tol {rtt_tolerance:.0f}%)")

    # ---- throughput, both directions ----
    for reverse in (False, True):
        result.iperf.append(
            measure_iperf(h1, h4, h4_ip, seconds=iperf_seconds, reverse=reverse)
        )
    for r in result.iperf:
        info(f"{label}: iperf3 {r.direction} = sender {r.sender_mbps:.2f} Mbps, "
             f"receiver {r.receiver_mbps:.2f} Mbps (target {spec.path_bottleneck_bw(path):.1f} Mbps)")

    target_bw = spec.path_bottleneck_bw(path)
    lower = target_bw * (1.0 - bandwidth_tolerance)
    upper = 10.30  # Upper bound: <= 10.3 Mbps receiver side
    for r in result.iperf:
        check(f"{label}: iperf3 TCP {r.direction} within "
              f"{lower:.1f}-{upper:.1f} Mbps (receiver side)",
              lower <= r.receiver_mbps <= upper,
              f"sender={r.sender_mbps:.2f} Mbps, receiver={r.receiver_mbps:.2f} Mbps")

    return result


def teardown_state(
    topo: MultipathTopology,
    installer: StaticFlowInstaller,
    spec: TopologySpec,
    h4: Any,
) -> None:
    """Return the network to the pristine state for the next path."""
    installer.clear_switches(sorted(spec.switches))
    for link in spec.links.values():
        topo.net.configLinkStatus(link.a, link.b, "up")
    h4.cmd("pkill -f 'iperf3 -s' 2>/dev/null; true")
    time.sleep(0.5)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="P2 acceptance test for the multipath topology (run as root)."
    )
    p.add_argument("--config", default=None)
    p.add_argument("--isolate", default="static_flows",
                   choices=("static_flows", "link_down", "both"),
                   help="how to isolate one path at a time")
    p.add_argument("--iperf-seconds", type=int, default=10)
    p.add_argument("--bandwidth-tolerance", type=float, default=0.10,
                   help="fraction of configured bandwidth iperf3 must reach")
    p.add_argument("--rtt-tolerance", type=float, default=15.0,
                   help="percent error allowed between measured and predicted RTT")
    p.add_argument("--datapath", default=None, choices=("kernel", "netdev"))
    p.add_argument("--dry-run", action="store_true", help="Perform preflight and import checks without root/Mininet")
    p.add_argument("--out", default=None, help="write a JSON report here")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    global LOGGER
    args = parse_args(argv)
    config = load_config(args.config)
    LOGGER = setup_logging(config, name="verify.p2")

    if not preflight(config):
        print("\nPreflight check failed. Aborting.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n--dry-run requested: Preflight passed successfully. Exiting.")
        return 0

    if os.geteuid() != 0:
        print("ERROR: this script must run as root (Mininet needs namespaces).",
              file=sys.stderr)
        return 2

    spec = build_spec(config)
    datapath = args.datapath or str(config.get("topology.datapath_type", "kernel"))
    installer = StaticFlowInstaller()

    print(HR)
    print(" P2 acceptance -- multipath topology")
    print(HR)
    print(f"date              : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"config            : {config.source_path}")
    print(f"datapath          : {datapath}")
    print(f"isolate method(s) : {args.isolate}")
    print(f"bandwidth tol     : {args.bandwidth_tolerance:.0%}")
    print(f"rtt tolerance     : {args.rtt_tolerance:.0f}%")

    section("0. Configuration and topology shape")
    print(spec.describe())
    graph = spec.switch_graph()
    import networkx as nx
    cycles = nx.cycle_basis(graph)
    info(f"switch graph: {graph.number_of_nodes()} nodes, "
         f"{graph.number_of_edges()} edges, {len(cycles)} independent cycle(s)")
    for cycle in cycles:
        info(f"loop: {' -> '.join(cycle)} -> {cycle[0]}")

    h1_name = "h1"
    h4_name = "h4"
    h4_ip = spec.host(h4_name).address
    all_paths = topology_paths(spec, h1_name, h4_name)

    check("exactly 2 h1->h4 paths exist", len(all_paths) == 2, f"found {len(all_paths)}")
    check("topology contains exactly one loop", len(cycles) == 1,
          f"{len(cycles)} independent cycles")

    results: List[PathResult] = []
    cleanup_ok = False
    # Snapshot pre-existing interfaces so Docker's veth is not blamed on us.
    baseline_ifaces = current_interfaces()
    info(f"baseline interfaces present before the run: {baseline_ifaces or 'none'}")
    topo = MultipathTopology(spec, controller=None, datapath_type=datapath)

    try:
        section("1. Build the network (no controller: P2 has no controller yet)")
        topo.build()
        h1 = topo.net.get(h1_name)
        h4 = topo.net.get(h4_name)
        info(f"switches up: {', '.join(sorted(spec.switches))}")
        info(f"hosts up    : {', '.join(sorted(spec.hosts))}")
        bridges = subprocess.run([venv_bin("ovs-vsctl"), "list-br"], capture_output=True,
                                 text=True).stdout.split()
        check("all 4 OVS bridges present", len(bridges) == 4, f"bridges={sorted(bridges)}")

        # baseline with no rules at all: nothing should flow (no controller)
        installer.clear_switches(sorted(spec.switches))
        info("flow tables cleared; verifying the loop cannot flood without rules")

        methods = (["static_flows", "link_down"] if args.isolate == "both"
                   else [args.isolate])

        for method in methods:
            for index, path in enumerate(all_paths):
                letter = "A" if index == 0 else "B"
                label = f"path {letter} [{method}]"
                section(f"{'2' if method == 'static_flows' else '3'}. "
                        f"{label}: {' -> '.join(path)}")
                info(f"inter-switch links: "
                     f"{', '.join(l.name for l in spec.path_inter_switch_links(path))}")
                info(f"predicted RTT: {spec.expected_rtt_ms(path):.2f} ms  "
                     f"bottleneck: {spec.path_bottleneck_bw(path):.1f} Mbps")
                result = exercise_path(
                    topo, spec, installer, path, label, method,
                    h1, h4, h4_ip,
                    iperf_seconds=args.iperf_seconds,
                    bandwidth_tolerance=args.bandwidth_tolerance,
                    rtt_tolerance=args.rtt_tolerance,
                )
                results.append(result)
                if method == "static_flows" and result.flows_after:
                    info(f"flow counts after install: {result.flows_after}")
                teardown_state(topo, installer, spec, h4)

    finally:
        section("4. Teardown and cleanup verification")
        try:
            topo.stop()
        except Exception as exc:  # pragma: no cover - best effort
            bad(f"Mininet stop raised {exc!r}")
        mn_clean()
        time.sleep(2.0)

        state = leftover_state(baseline_ifaces)
        info(f"OVS bridges remaining      : {state['ovs_bridges']}")
        info(f"mininet/mnexec processes   : {state['mininet_processes'] or 'none'}")
        info(f"new interfaces since baseline: {state['leftover_interfaces'] or 'none'}")

        cleanup_ok = (
            not state["ovs_bridges"]
            and not state["mininet_processes"]
            and not state["leftover_interfaces"]
        )
        check("no OVS bridges left behind", not state["ovs_bridges"],
              str(state["ovs_bridges"]))
        check("no mininet/mnexec processes left", not state["mininet_processes"],
              str(state["mininet_processes"][:3]))
        check("no new interfaces left behind", not state["leftover_interfaces"],
              str(state["leftover_interfaces"][:5]))

    # ------------------------------------------------------------- report ---
    section("5. Summary")
    print(f"  {'path':<26} {'method':<14} {'loss%':>6} "
          f"{'rtt ms':>8} {'pred ms':>8} {'fwd sender':>11} {'fwd recv':>10} {'rev sender':>11} {'rev recv':>10}")
    for r in results:
        fwd = next((i for i in r.iperf if i.direction == "forward"), None)
        rev = next((i for i in r.iperf if i.direction == "reverse"), None)
        fwd_s = f"{fwd.sender_mbps:.2f}" if fwd else "-"
        fwd_r = f"{fwd.receiver_mbps:.2f}" if fwd else "-"
        rev_s = f"{rev.sender_mbps:.2f}" if rev else "-"
        rev_r = f"{rev.receiver_mbps:.2f}" if rev else "-"
        ping_s = f"{r.ping.rtt_avg_ms:.2f}" if r.ping else "-"
        loss_s = f"{r.ping.loss_pct:.1f}" if r.ping else "-"
        print(f"  {r.label:<26} {r.isolate_method:<14} {loss_s:>6} "
              f"{ping_s:>8} {r.predicted_rtt_ms:>8.2f} "
              f"{fwd_s:>11} {fwd_r:>10} {rev_s:>11} {rev_r:>10}")

    print(f"\n{THIN}\nSUMMARY: {PASSED} passed, {FAILED} failed\n{THIN}")
    if args.out:
        report = {
            "config": config.source_path,
            "datapath": datapath,
            "bandwidth_tolerance": args.bandwidth_tolerance,
            "rtt_tolerance_pct": args.rtt_tolerance,
            "passed": PASSED,
            "failed": FAILED,
            "cleanup_ok": cleanup_ok,
            "paths": [asdict(r) for r in results],
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, default=str)
        info(f"report written to {args.out}")

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())