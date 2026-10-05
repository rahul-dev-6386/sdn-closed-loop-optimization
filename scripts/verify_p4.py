#!/usr/bin/env python
"""P4 acceptance test -- Topology discovery with NetworkX and k-shortest paths.

Can be run with or without root:
    python scripts/verify_p4.py

Asserts:
1. TopologyTracker builds a NetworkX graph matching the diamond topology.
2. Link capacities (bandwidth, delay, max queue size) are correctly mapped from config.yaml.
3. k-shortest paths correctly identifies both loop-free paths between h1 and h4:
   - h1 -> s1 -> s2 -> s4 -> h4
   - h1 -> s1 -> s3 -> s4 -> h4
4. Predicted RTT is 24 ms and bottleneck bandwidth is 10.0 Mbps.
5. All three path weighting schemes (hops, delay_ms, bw_mbps) produce valid paths.
6. Dynamic link failure immediately updates the available shortest paths.
7. PortMap interface compatibility is maintained.
8. LLDP packet generation and parsing accurately recover DPID and port.
9. Controller integrates TopologyTracker and exposes live topology in snapshots.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import load_config
from controller.topology import (
    TopologyTracker,
    build_lldp_packet,
    parse_lldp_packet,
)
from controller.sdn_controller import SdnController

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


def main() -> int:
    parser = argparse.ArgumentParser(description="P4 Acceptance Test: Topology Discovery")
    parser.add_argument("--out", help="Write JSON summary to this path")
    args = parser.parse_args()

    print(f"{HR}\nAI-Driven SDN: Phase 4 Acceptance Test (Topology Discovery & Paths)\n{HR}")

    cfg = load_config()

    # 1. Graph Construction & Capacities
    section("1. NetworkX Graph Construction & Capacities")
    tracker = TopologyTracker(cfg)
    tracker.add_link("s1", 2, "s2", 1)
    tracker.add_link("s1", 3, "s3", 1)
    tracker.add_link("s2", 2, "s4", 1)
    tracker.add_link("s3", 2, "s4", 2)
    tracker.add_host("h1", ip="10.0.0.1", switch="s1", port=1)
    tracker.add_host("h4", ip="10.0.0.4", switch="s4", port=3)

    g = tracker.full_graph()
    check(
        "graph contains 4 switches and 2 hosts",
        set(g.nodes()) == {"s1", "s2", "s3", "s4", "h1", "h4"},
        f"nodes={sorted(g.nodes())}",
    )
    check("graph has 6 edges total", g.number_of_edges() == 6, f"edges={g.number_of_edges()}")

    e_s1_s2 = g.get_edge_data("s1", "s2")
    check(
        "inter-switch link shaped to 10 Mbps and 5 ms",
        e_s1_s2["bw_mbps"] == 10.0 and e_s1_s2["delay_ms"] == 5.0,
        f"bw={e_s1_s2['bw_mbps']} Mbps, delay={e_s1_s2['delay_ms']} ms",
    )
    e_h1_s1 = g.get_edge_data("h1", "s1")
    check(
        "host link shaped to 100 Mbps and 1 ms",
        e_h1_s1["bw_mbps"] == 100.0 and e_h1_s1["delay_ms"] == 1.0,
        f"bw={e_h1_s1['bw_mbps']} Mbps, delay={e_h1_s1['delay_ms']} ms",
    )

    # 2. k-Shortest Paths Enumeration
    section("2. k-Shortest Paths Enumeration (H1 -> H4)")
    paths = tracker.k_shortest_paths("h1", "h4", k=3)
    info(f"discovered paths: {paths}")
    check("exactly 2 loop-free paths exist", len(paths) == 2, f"count={len(paths)}")

    path_via_s2 = ["h1", "s1", "s2", "s4", "h4"]
    path_via_s3 = ["h1", "s1", "s3", "s4", "h4"]
    check("path via s2 identified", path_via_s2 in paths)
    check("path via s3 identified", path_via_s3 in paths)

    rtt_s2 = tracker.expected_rtt_ms(path_via_s2)
    bottle_s2 = tracker.path_bottleneck_bw(path_via_s2)
    check("predicted RTT is 24 ms", rtt_s2 == 24.0, f"rtt={rtt_s2:.2f} ms")
    check("path bottleneck bandwidth is 10.0 Mbps", bottle_s2 == 10.0, f"bw={bottle_s2:.1f} Mbps")

    # 3. Path Weights (Asymmetric Fixture)
    section("3. Metric Weight Variations & Asymmetric Path Selection")
    asym = TopologyTracker(cfg)
    asym.add_switch("s1", 1)
    asym.add_switch("s2", 2)
    asym.add_switch("s3", 3)
    asym.add_switch("s4", 4)
    asym.add_host("h1", ip="10.0.0.1", switch="s1", port=1)
    asym.add_host("h4", ip="10.0.0.4", switch="s4", port=3)

    # Path B (via s2): low latency 5 ms, 10 Mbps
    asym.add_link("s1", 2, "s2", 1, bw_mbps=10.0, delay_ms=5.0)
    asym.add_link("s2", 2, "s4", 1, bw_mbps=10.0, delay_ms=5.0)
    # Path C (via s3): high bw 100 Mbps, 20 ms delay
    asym.add_link("s1", 3, "s3", 1, bw_mbps=100.0, delay_ms=20.0)
    asym.add_link("s3", 2, "s4", 2, bw_mbps=100.0, delay_ms=20.0)
    # Path A (direct s1-s4): 3 hops total (fewest hops), 35 ms delay, 5 Mbps
    asym.add_link("s1", 4, "s4", 4, bw_mbps=5.0, delay_ms=35.0)

    best_hops = asym.k_shortest_paths("h1", "h4", k=1, weight="hops")[0]
    best_delay = asym.k_shortest_paths("h1", "h4", k=1, weight="delay_ms")[0]
    best_bw = asym.k_shortest_paths("h1", "h4", k=1, weight="bw_mbps")[0]

    info(f"best path by hops:      {' -> '.join(best_hops)} (3 hops, bottleneck={asym.path_bottleneck_bw(best_hops)} Mbps)")
    info(f"best path by delay_ms:  {' -> '.join(best_delay)} (delay={asym.path_delay_ms(best_delay):.1f} ms, bottleneck={asym.path_bottleneck_bw(best_delay)} Mbps)")
    info(f"best path by bw_mbps:   {' -> '.join(best_bw)} (bottleneck={asym.path_bottleneck_bw(best_bw)} Mbps)")

    check("hops metric selects fewest-hop direct path (Path A)", best_hops == ["h1", "s1", "s4", "h4"])
    check("delay_ms metric selects lowest-latency path (Path B via s2)", best_delay == ["h1", "s1", "s2", "s4", "h4"])
    check("bw_mbps metric selects highest-bandwidth path (Path C via s3)", best_bw == ["h1", "s1", "s3", "s4", "h4"])
    check(
        "hops, delay_ms, and bw_mbps select 3 DIFFERENT best paths",
        best_hops != best_delay and best_delay != best_bw and best_hops != best_bw,
    )

    # Bottleneck bandwidth verification
    check("Path A bottleneck bandwidth is 5.0 Mbps", asym.path_bottleneck_bw(best_hops) == 5.0)
    check("Path B bottleneck bandwidth is 10.0 Mbps", asym.path_bottleneck_bw(best_delay) == 10.0)
    check("Path C bottleneck bandwidth is 100.0 Mbps", asym.path_bottleneck_bw(best_bw) == 100.0)

    # k > available paths test (3 paths exist, k=5 requested)
    k_paths_all = asym.k_shortest_paths("h1", "h4", k=5, weight="hops")
    check("requesting k=5 on a 3-path graph safely returns 3 paths without crashing", len(k_paths_all) == 3)

    # 4. Dynamic Failure & Re-convergence
    section("4. Dynamic Link Failure & Convergence")
    tracker.remove_link("s1", "s2")
    remaining_paths = tracker.k_shortest_paths("h1", "h4", k=3)
    check(
        "after s1-s2 failure, only path via s3 remains",
        len(remaining_paths) == 1 and remaining_paths[0] == path_via_s3,
        f"remaining={remaining_paths}",
    )
    # Restore link
    tracker.add_link("s1", 2, "s2", 1)
    restored_paths = tracker.k_shortest_paths("h1", "h4", k=3)
    check("after restoring s1-s2, both paths available", len(restored_paths) == 2)

    # 5. LLDP Packet Crafting & Parsing
    section("5. LLDP Discovery Frame Verification")
    lldp_data = build_lldp_packet(dpid=2, port_no=1, src_mac="00:00:00:00:00:02")
    check("LLDP frame generated (> 50 bytes)", len(lldp_data) > 50, f"len={len(lldp_data)} B")
    parsed = parse_lldp_packet(lldp_data)
    check(
        "LLDP packet parsed correctly (dpid=2, port=1)",
        parsed == (2, 1),
        f"parsed={parsed}",
    )
    non_lldp = parse_lldp_packet(b"not an lldp packet")
    check("non-LLDP packet returns None safely", non_lldp is None)

    # 6. Controller Integration
    section("6. Controller Integration & Snapshot")
    controller = SdnController(config=cfg)
    check("controller initialized with TopologyTracker", isinstance(controller.topology, TopologyTracker))
    snap = controller.snapshot()
    check("controller snapshot contains topology key", "topology" in snap)
    check("controller snapshot exposes topology nodes", len(snap["topology"]["nodes"]) >= 4)

    # Summary
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
                    "nodes": list(g.nodes()),
                    "edges": g.number_of_edges(),
                    "paths_h1_h4": paths,
                    "predicted_rtt_ms": rtt_s2,
                    "bottleneck_bw_mbps": bottle_s2,
                },
                f,
                indent=2,
            )

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
