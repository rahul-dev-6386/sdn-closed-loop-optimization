"""Pure-logic tests for the P2 multipath topology.

Nothing here needs root or a live network: the topology specification, path
enumeration, RTT prediction and static-flow-plan generation are all pure
functions of ``config.yaml``.
"""
from __future__ import annotations

from typing import Dict, List

import networkx as nx
import pytest

from common.config import ConfigError, load_config
from topology.multipath_topology import (
    FlowRule,
    Link,
    TopologySpec,
    build_flow_plan,
    build_spec,
    topology_paths,
)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def spec(cfg) -> TopologySpec:
    return build_spec(cfg)


# ------------------------------------------------------------------- spec ----
def test_spec_matches_the_described_shape(spec: TopologySpec) -> None:
    """S1-S2-S4 and S1-S3-S4, with H1 on S1 and H4 on S4."""
    assert set(spec.switches) == {"s1", "s2", "s3", "s4"}
    assert set(spec.hosts) == {"h1", "h4"}
    assert spec.hosts["h1"].switch == "s1"
    assert spec.hosts["h4"].switch == "s4"
    expected_links = {"s1--s2", "s1--s3", "s2--s4", "s3--s4"}
    assert set(spec.links) == expected_links


def test_every_link_is_shaped_from_config(spec: TopologySpec, cfg) -> None:
    for link in spec.links.values():
        assert link.bw_mbps == cfg.get("topology.link_bw_mbps")
        assert link.delay_ms == cfg.get("topology.link_delay_ms")
        assert link.max_queue_size == cfg.get("topology.link_max_queue_size")
        assert link.loss_pct == 0.0


def test_bw_delay_and_queue_are_configurable(cfg) -> None:
    """The acceptance test's shape must be driven by config, not hardcoded."""
    raw = dict(cfg)
    topo = dict(raw["topology"])
    topo.update(link_bw_mbps=20.0, link_delay_ms=2.5, link_max_queue_size=42)
    custom = ConfigLike(cfg, topo)

    spec = build_spec(custom)
    link = spec.link_between("s1", "s2")
    assert link.bw_mbps == 20.0
    assert link.delay_ms == 2.5
    assert link.max_queue_size == 42


class ConfigLike(dict):
    """Minimal stand-in exposing ``section()`` like a real :class:`Config`."""

    def __init__(self, base, topo_overrides):
        merged = dict(base)
        merged["topology"] = topo_overrides
        super().__init__(merged)

    def section(self, name):
        from common.config import Config

        return Config(self[name], f"<test>:{name}")

    def get(self, key, default=None):
        node = self
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node


def test_per_link_overrides_are_honoured(cfg) -> None:
    topo = dict(cfg["topology"])
    topo["links"] = [
        ["s1", "s2"],
        ["s1", "s3"],
        ["s2", "s4"],
        {"a": "s3", "b": "s4", "bw_mbps": 4.0, "delay_ms": 20.0, "max_queue_size": 7},
    ]
    spec = build_spec(ConfigLike(cfg, topo))
    assert spec.link_between("s3", "s4").bw_mbps == 4.0
    assert spec.link_between("s3", "s4").delay_ms == 20.0
    assert spec.link_between("s3", "s4").max_queue_size == 7
    # un-overridden links keep the defaults
    assert spec.link_between("s1", "s2").bw_mbps == cfg.get("topology.link_bw_mbps")


def test_duplicate_links_are_rejected(cfg) -> None:
    topo = dict(cfg["topology"])
    topo["links"] = [["s1", "s2"], ["s2", "s1"], ["s1", "s3"], ["s2", "s4"], ["s3", "s4"]]
    with pytest.raises(ValueError, match="duplicate link"):
        build_spec(ConfigLike(cfg, topo))


def test_unknown_switch_is_rejected(cfg) -> None:
    topo = dict(cfg["topology"])
    topo["links"] = [["s1", "s2"], ["s1", "s9"], ["s2", "s4"], ["s3", "s4"]]
    with pytest.raises(ValueError, match="unknown switch"):
        build_spec(ConfigLike(cfg, topo))


def test_insufficient_switch_ports_is_rejected(cfg) -> None:
    topo = dict(cfg["topology"])
    topo["switches"] = [
        {"name": "s1", "ports": 4},
        {"name": "s2", "ports": 1},  # needs 2 (one link + one link)
        {"name": "s3", "ports": 2},
        {"name": "s4", "ports": 4},
    ]
    with pytest.raises(ValueError, match="needs"):
        build_spec(ConfigLike(cfg, topo))


# ------------------------------------------------------------------ graph ----
def test_switch_graph_has_exactly_one_cycle(spec: TopologySpec) -> None:
    graph = spec.switch_graph()
    assert graph.number_of_nodes() == 4
    assert graph.number_of_edges() == 4
    cycles = nx.cycle_basis(graph)
    assert len(cycles) == 1, "the diamond must contain exactly one loop"


def test_graph_carries_link_capacity(spec: TopologySpec) -> None:
    graph = spec.switch_graph()
    data = graph.get_edge_data("s1", "s2")
    assert data["bw_mbps"] == 10.0
    assert data["delay_ms"] == 5.0
    assert data["max_queue_size"] == 100
    assert data["link"] == "s1--s2"


# ------------------------------------------------------------------ paths ----
def test_exactly_two_h1_to_h4_paths(spec: TopologySpec) -> None:
    paths = topology_paths(spec, "h1", "h4")
    assert len(paths) == 2
    assert ["h1", "s1", "s2", "s4", "h4"] in paths
    assert ["h1", "s1", "s3", "s4", "h4"] in paths


def test_paths_are_identical_in_delay_so_ordering_is_deterministic(
    spec: TopologySpec,
) -> None:
    paths = topology_paths(spec, "h1", "h4")
    rtts = [spec.expected_rtt_ms(p) for p in paths]
    assert rtts[0] == rtts[1]
    # deterministic tie-break -> lexicographic order of the equal-length paths
    assert paths == sorted(paths)


def test_k_shortest_respects_k(spec: TopologySpec) -> None:
    assert len(spec.k_shortest_paths("h1", "h4", k=1)) == 1
    assert len(spec.k_shortest_paths("h1", "h4", k=2)) == 2
    assert len(spec.k_shortest_paths("h1", "h4", k=99)) == 2


def test_expected_rtt_is_double_the_one_way_sum(spec: TopologySpec, cfg) -> None:
    """2*(host 1ms + link 5ms + link 5ms + host 1ms) = 24 ms."""
    link_delay = cfg.get("topology.link_delay_ms")
    host_delay = cfg.get("topology.host_delay_ms")
    path = ["h1", "s1", "s2", "s4", "h4"]
    expected = 2 * (host_delay + 2 * link_delay + host_delay)
    assert spec.expected_rtt_ms(path) == pytest.approx(expected)
    assert spec.expected_rtt_ms(path) == pytest.approx(24.0)


def test_bottleneck_bandwidth_is_the_minimum_hop(spec: TopologySpec) -> None:
    """Host links are 100 Mbps, inter-switch links 10 Mbps -> bottleneck 10."""
    assert spec.path_bottleneck_bw(["h1", "s1", "s2", "s4", "h4"]) == 10.0


def test_inter_switch_link_extraction(spec: TopologySpec) -> None:
    path = ["h1", "s1", "s2", "s4", "h4"]
    names = [l.name for l in spec.path_inter_switch_links(path)]
    assert names == ["s1--s2", "s2--s4"]


def test_unknown_weight_is_rejected(spec: TopologySpec) -> None:
    with pytest.raises(ValueError, match="unknown path weight"):
        spec.k_shortest_paths("h1", "h4", k=2, weight="bogus")


# ------------------------------------------------------------- flow plan ----
#: Synthetic port map: (switch, neighbour) -> port number.
PORTS: Dict[str, Dict[str, int]] = {
    "s1": {"h1": 1, "s2": 2, "s3": 3},
    "s2": {"s1": 1, "s4": 2},
    "s3": {"s1": 1, "s4": 2},
    "s4": {"s2": 1, "s3": 2, "h4": 3},
}


def port_of(switch: str, neighbour: str) -> int:
    return PORTS[switch][neighbour]


def test_flow_plan_for_path_a(spec: TopologySpec) -> None:
    """Path A h1->s1->s2->s4->h4 installs forward and reverse rules."""
    rules = build_flow_plan(spec, ["h1", "s1", "s2", "s4", "h4"], port_of)
    forward = [r for r in rules if "reverse" not in r.description]
    reverse = [r for r in rules if "reverse" in r.description]

    assert len(forward) == 3 and len(reverse) == 3
    assert (forward[0].switch, forward[0].in_port, forward[0].out_port) == ("s1", 1, 2)
    assert (forward[1].switch, forward[1].in_port, forward[1].out_port) == ("s2", 1, 2)
    assert (forward[2].switch, forward[2].in_port, forward[2].out_port) == ("s4", 1, 3)
    # reverse mirrors each hop
    assert (reverse[0].switch, reverse[0].in_port, reverse[0].out_port) == ("s4", 3, 1)
    assert (reverse[2].switch, reverse[2].in_port, reverse[2].out_port) == ("s1", 2, 1)


def test_flow_plan_for_path_b(spec: TopologySpec) -> None:
    rules = build_flow_plan(spec, ["h1", "s1", "s3", "s4", "h4"], port_of)
    forward = [r for r in rules if "reverse" not in r.description]
    assert (forward[1].switch, forward[1].in_port, forward[1].out_port) == ("s3", 1, 2)
    # never references s2 -> path B is genuinely disjoint from path A
    assert all(r.switch != "s2" for r in rules)


def test_flow_plan_unidirectional_halves_the_rules(spec: TopologySpec) -> None:
    both = build_flow_plan(spec, ["h1", "s1", "s2", "s4", "h4"], port_of, True)
    fwd = build_flow_plan(spec, ["h1", "s1", "s2", "s4", "h4"], port_of, False)
    assert len(fwd) == 3
    assert len(both) == 2 * len(fwd)


def test_flow_renders_as_ovs_ofctl_string() -> None:
    rule = FlowRule(switch="s2", in_port=1, out_port=2, description="x")
    assert rule.ofctl_flow() == "in_port=1,actions=output:2"


def test_degenerate_path_is_rejected(spec: TopologySpec) -> None:
    with pytest.raises(ValueError, match="too short"):
        build_flow_plan(spec, ["h1", "s1"], port_of)


def test_each_path_produces_distinct_rule_sets(spec: TopologySpec) -> None:
    """The two paths must not share a rule, otherwise isolation is not real."""
    a = {(r.switch, r.in_port, r.out_port) for r in
         build_flow_plan(spec, ["h1", "s1", "s2", "s4", "h4"], port_of)}
    b = {(r.switch, r.in_port, r.out_port) for r in
         build_flow_plan(spec, ["h1", "s1", "s3", "s4", "h4"], port_of)}
    assert a.isdisjoint(b)


def test_unknown_neighbour_port_is_a_clear_error(spec: TopologySpec) -> None:
    def bad_port_of(switch: str, neighbour: str) -> int:
        raise KeyError(f"no port on {switch} facing {neighbour}")

    with pytest.raises(KeyError, match="facing"):
        build_flow_plan(spec, ["h1", "s1", "s2", "s4", "h4"], bad_port_of)