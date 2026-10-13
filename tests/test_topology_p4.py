"""Unit tests for Phase 4: Topology discovery and NetworkX graph management.

Covers:
1. Dynamic NetworkX graph construction and link capacity assignment from config.
2. k-shortest simple path enumeration across the multipath diamond.
3. Metric weighting (hop count, delay, reciprocal bandwidth).
4. Dynamic link/switch addition and removal.
5. PortMap backward-compatibility interface.
6. LLDP frame generation and parsing.
"""

from __future__ import annotations

import pytest
import networkx as nx

from common.config import load_config
from controller.topology import (
    TopologyTracker,
    DiscoveredLink,
    build_lldp_packet,
    parse_lldp_packet,
)


@pytest.fixture
def tracker() -> TopologyTracker:
    """Fixture providing a TopologyTracker populated from config.yaml."""
    cfg = load_config()
    t = TopologyTracker(cfg)
    # Populate the diamond links (matching P2 topology)
    t.add_link("s1", 2, "s2", 1)
    t.add_link("s1", 3, "s3", 1)
    t.add_link("s2", 2, "s4", 1)
    t.add_link("s3", 2, "s4", 2)
    # Populate host attachments
    t.add_host("h1", ip="10.0.0.1", switch="s1", port=1)
    t.add_host("h4", ip="10.0.0.4", switch="s4", port=3)
    return t


# ---------------------------------------------------------------- Graph Structure --
def test_topology_graph_nodes_and_edges(tracker: TopologyTracker) -> None:
    """Validate that the tracker's graph contains all 4 switches and 2 hosts."""
    g = tracker.full_graph()
    assert set(g.nodes()) == {"s1", "s2", "s3", "s4", "h1", "h4"}
    assert g.number_of_edges() == 6  # 4 inter-switch links + 2 host links


def test_topology_link_capacities(tracker: TopologyTracker) -> None:
    """Validate that link capacities match config.yaml (10 Mbps inter-switch, 100 Mbps host)."""
    g = tracker.full_graph()
    # Inter-switch link
    s1_s2 = g.get_edge_data("s1", "s2")
    assert s1_s2 is not None
    assert s1_s2["bw_mbps"] == 10.0
    assert s1_s2["delay_ms"] == 5.0
    assert s1_s2["max_queue_size"] == 100

    # Host link
    h1_s1 = g.get_edge_data("h1", "s1")
    assert h1_s1 is not None
    assert h1_s1["bw_mbps"] == 100.0
    assert h1_s1["delay_ms"] == 1.0


# ----------------------------------------------------------- k-Shortest Paths --
def test_k_shortest_paths_finds_both_diamond_paths(tracker: TopologyTracker) -> None:
    """Verify that k-shortest paths returns both loop-free paths for h1 -> h4."""
    paths = tracker.k_shortest_paths("h1", "h4", k=3)
    assert len(paths) == 2, "Multipath diamond must yield exactly 2 simple paths between h1 and h4"

    p0, p1 = paths[0], paths[1]
    assert p0[0] == "h1" and p0[-1] == "h4"
    assert p1[0] == "h1" and p1[-1] == "h4"

    path_via_s2 = ["h1", "s1", "s2", "s4", "h4"]
    path_via_s3 = ["h1", "s1", "s3", "s4", "h4"]

    assert path_via_s2 in paths
    assert path_via_s3 in paths


def test_k_shortest_respects_limit(tracker: TopologyTracker) -> None:
    """Verify that specifying k=1 returns only the single shortest path."""
    paths = tracker.k_shortest_paths("h1", "h4", k=1)
    assert len(paths) == 1


def test_expected_rtt_and_bottleneck(tracker: TopologyTracker) -> None:
    """Check that expected RTT is 24 ms and bottleneck bandwidth is 10 Mbps."""
    path = ["h1", "s1", "s2", "s4", "h4"]
    # 1.0 (h1-s1) + 5.0 (s1-s2) + 5.0 (s2-s4) + 1.0 (s4-h4) = 12.0 ms one-way
    assert tracker.path_delay_ms(path) == 12.0
    assert tracker.expected_rtt_ms(path) == 24.0
    assert tracker.path_bottleneck_bw(path) == 10.0


def test_weight_metrics(tracker: TopologyTracker) -> None:
    """Check path finding across hops, delay_ms, and bw_mbps weights."""
    paths_hops = tracker.k_shortest_paths("h1", "h4", k=2, weight="hops")
    paths_delay = tracker.k_shortest_paths("h1", "h4", k=2, weight="delay_ms")
    paths_bw = tracker.k_shortest_paths("h1", "h4", k=2, weight="bw_mbps")

    assert len(paths_hops) == 2
    assert len(paths_delay) == 2
    assert len(paths_bw) == 2

    with pytest.raises(ValueError, match="unknown path weight"):
        tracker.k_shortest_paths("h1", "h4", weight="invalid")


def test_k_shortest_fewer_paths_than_k_does_not_crash(tracker: TopologyTracker) -> None:
    """Verify that requesting k=10 when only 2 paths exist safely returns 2 paths without crashing."""
    paths = tracker.k_shortest_paths("h1", "h4", k=10)
    assert len(paths) == 2


@pytest.fixture
def asymmetric_tracker() -> TopologyTracker:
    """Fixture with asymmetric links where hops, delay, and bw produce DIFFERENT best paths.

    Paths between h1 and h4:
      - Path A (direct s1-s4): 3 hops (fewest hops), delay=35 ms, bw=5 Mbps
      - Path B (via s2): 4 hops, delay=5 ms per link (lowest delay), bw=10 Mbps
      - Path C (via s3): 4 hops, delay=20 ms per link, bw=100 Mbps (highest bandwidth)
    """
    cfg = load_config()
    t = TopologyTracker(cfg)
    t.add_switch("s1", 1)
    t.add_switch("s2", 2)
    t.add_switch("s3", 3)
    t.add_switch("s4", 4)
    t.add_host("h1", ip="10.0.0.1", switch="s1", port=1)
    t.add_host("h4", ip="10.0.0.4", switch="s4", port=3)

    # Path B: low latency 5 ms, 10 Mbps
    t.add_link("s1", 2, "s2", 1, bw_mbps=10.0, delay_ms=5.0)
    t.add_link("s2", 2, "s4", 1, bw_mbps=10.0, delay_ms=5.0)

    # Path C: high bw 100 Mbps, 20 ms delay
    t.add_link("s1", 3, "s3", 1, bw_mbps=100.0, delay_ms=20.0)
    t.add_link("s3", 2, "s4", 2, bw_mbps=100.0, delay_ms=20.0)

    # Path A: direct s1-s4 (3 hops total, 35 ms delay, 5 Mbps)
    t.add_link("s1", 4, "s4", 4, bw_mbps=5.0, delay_ms=35.0)

    return t


def test_asymmetric_metrics_produce_different_best_paths(asymmetric_tracker: TopologyTracker) -> None:
    """Verify that hops, delay_ms, and bw_mbps produce DIFFERENT optimal paths on asymmetric topology."""
    # 1. Hops: fewest hops is direct s1-s4 (3 hops: h1 -> s1 -> s4 -> h4)
    best_hops = asymmetric_tracker.k_shortest_paths("h1", "h4", k=1, weight="hops")[0]
    assert best_hops == ["h1", "s1", "s4", "h4"], f"Expected direct path for hops, got {best_hops}"

    # 2. Delay: lowest delay is via s2 (1 + 5 + 5 + 1 = 12 ms one-way)
    best_delay = asymmetric_tracker.k_shortest_paths("h1", "h4", k=1, weight="delay_ms")[0]
    assert best_delay == ["h1", "s1", "s2", "s4", "h4"], f"Expected s2 path for delay, got {best_delay}"

    # 3. Bandwidth (1/bw): highest bandwidth is via s3 (100 Mbps)
    best_bw = asymmetric_tracker.k_shortest_paths("h1", "h4", k=1, weight="bw_mbps")[0]
    assert best_bw == ["h1", "s1", "s3", "s4", "h4"], f"Expected s3 path for bw, got {best_bw}"

    # Separate bottleneck calculation
    assert asymmetric_tracker.path_bottleneck_bw(best_hops) == 5.0
    assert asymmetric_tracker.path_bottleneck_bw(best_delay) == 10.0
    assert asymmetric_tracker.path_bottleneck_bw(best_bw) == 100.0

    # Ensure requesting k=5 returns all 3 paths without crashing
    all_paths = asymmetric_tracker.k_shortest_paths("h1", "h4", k=5, weight="hops")
    assert len(all_paths) == 3


# --------------------------------------------------------- Dynamic Adaptations --
def test_dynamic_link_failure_rerouting(tracker: TopologyTracker) -> None:
    """When a link is removed, k_shortest_paths must immediately reflect the remaining topology."""
    # Remove s1-s2 link
    removed = tracker.remove_link("s1", "s2")
    assert removed is True

    paths = tracker.k_shortest_paths("h1", "h4", k=3)
    assert len(paths) == 1
    assert paths[0] == ["h1", "s1", "s3", "s4", "h4"]

    # Re-add s1-s2 link
    tracker.add_link("s1", 2, "s2", 1)
    paths_restored = tracker.k_shortest_paths("h1", "h4", k=3)
    assert len(paths_restored) == 2


def test_switch_removal(tracker: TopologyTracker) -> None:
    """Removing a switch cleans up its node and all incident links."""
    tracker.remove_switch("s2")
    assert not tracker.graph.has_node("s2")
    assert tracker.port("s1", "s2") is None
    paths = tracker.k_shortest_paths("h1", "h4", k=3)
    assert len(paths) == 1
    assert paths[0] == ["h1", "s1", "s3", "s4", "h4"]


# ------------------------------------------------------ PortMap Compatibility --
def test_portmap_queries_and_export(tracker: TopologyTracker) -> None:
    """Ensure TopologyTracker provides complete PortMap drop-in compatibility."""
    assert tracker.port("s1", "s2") == 2
    assert tracker.require_port("s1", "s2") == 2
    with pytest.raises(KeyError, match="no entry for s1 -> unknown"):
        tracker.require_port("s1", "unknown")

    assert tracker.shortest_switch_path("s1", "s4") in (["s1", "s2", "s4"], ["s1", "s3", "s4"])
    assert tracker.full_path("h1", "h4") in (
        ["h1", "s1", "s2", "s4", "h4"],
        ["h1", "s1", "s3", "s4", "h4"],
    )

    portmap = tracker.to_portmap()
    assert portmap.port("s1", "s2") == 2
    assert portmap.shortest_switch_path("s1", "s4") in (["s1", "s2", "s4"], ["s1", "s3", "s4"])


# ------------------------------------------------------------- LLDP Encoding --
def test_lldp_packet_roundtrip() -> None:
    """Verify that build_lldp_packet and parse_lldp_packet correctly round-trip dpid and port."""
    data = build_lldp_packet(dpid=4, port_no=2, src_mac="00:00:00:00:00:04", ttl=60)
    assert isinstance(data, bytes)

    res = parse_lldp_packet(data)
    assert res is not None
    dpid, port = res
    assert dpid == 4
    assert port == 2


def test_lldp_parse_non_lldp_returns_none() -> None:
    """Verify that non-LLDP frames are safely ignored by parse_lldp_packet."""
    assert parse_lldp_packet(b"not a valid lldp packet") is None
    assert parse_lldp_packet(b"") is None


def test_dynamic_host_record_and_paths() -> None:
    """Verify dynamic host attachment via record_host without pre-existing port map."""
    cfg = load_config()
    t = TopologyTracker(cfg)
    t.add_link("s1", 1, "s2", 1)
    t.add_link("s1", 2, "s3", 1)
    t.add_link("s2", 2, "s4", 1)
    t.add_link("s3", 2, "s4", 2)

    # Before hosts are discovered, hosts dict is empty and paths_h1_h4 is safe empty list
    d = t.export_topology_dict()
    assert d["hosts"] == {}
    assert d["paths_h1_h4"] == []

    # Dynamic host learning via ARP
    h1 = t.record_host(None, ip="10.0.0.1", switch="s1", port=3, mac="00:00:00:00:00:01")
    assert h1 == "h1"
    h4 = t.record_host(None, ip="10.0.0.4", switch="s4", port=3, mac="00:00:00:00:00:02")
    assert h4 == "h4"

    d2 = t.export_topology_dict()
    assert "h1" in d2["hosts"]
    assert "h4" in d2["hosts"]
    assert d2["hosts"]["h1"]["switch"] == "s1"
    assert d2["hosts"]["h1"]["port"] == 3
    assert d2["hosts"]["h4"]["switch"] == "s4"
    assert d2["hosts"]["h4"]["port"] == 3

    assert len(d2["paths_h1_h4"]) == 2
    assert ["h1", "s1", "s2", "s4", "h4"] in d2["paths_h1_h4"]
    assert ["h1", "s1", "s3", "s4", "h4"] in d2["paths_h1_h4"]


def test_sdn_controller_sync_topology_file(tmp_path: Any) -> None:
    """Verify that SdnController._sync_topology_file writes valid data/topology.json."""
    import json
    import os
    from common.config import Config, load_config
    from controller.sdn_controller import SdnController

    raw = dict(load_config()._data)
    raw["discovery"] = dict(raw.get("discovery", {}))
    raw["discovery"]["enabled"] = False
    raw["telemetry"] = dict(raw.get("telemetry", {}))
    raw["telemetry"]["enabled"] = False
    # Point BOTH output paths at tmp_path. The test previously read and wrote
    # the repository's real data/topology.json, so it (a) failed whenever a live
    # run had left a wider topology there, and (b) needed write access to files
    # that root-owned live runs create.
    raw["controller"] = dict(raw.get("controller", {}))
    raw["controller"]["topology_file_path"] = str(tmp_path / "topology.json")
    raw["controller"]["port_map_path"] = str(tmp_path / "portmap.json")
    cfg = Config(raw)

    controller = SdnController(config=cfg)
    controller.topology.add_link("s1", 1, "s2", 1)
    controller.topology.record_host("h1", ip="10.0.0.1", switch="s1", port=3, mac="00:00:00:00:00:01")
    controller._sync_topology_file()

    topo_file = tmp_path / "topology.json"
    assert os.path.isfile(topo_file)
    with open(topo_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data["links"]) == 1
    assert "h1" in data["hosts"]
    # Nothing may leak into the repository's data/ directory.
    assert os.path.isfile(tmp_path / "portmap.json")

