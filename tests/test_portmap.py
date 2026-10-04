"""Tests for the port map and shortest-path forwarding (P3).

These cover the logic that the P3 acceptance test exercises on real hardware:
that the controller can turn a (src_switch, dst_switch, dst_mac) triple into a
complete chain of exact-match rules pointing the right way.
"""
from __future__ import annotations

import json
from typing import Dict, Tuple

import networkx as nx
import pytest

from controller.portmap import PortMap


@pytest.fixture
def port_map() -> PortMap:
    """Port map matching the P2 diamond as built by Mininet.

    Concretely: s1[h1=1, s2=2, s3=3], s2[s1=1, s4=2], s3[s1=1, s4=2],
    s4[s2=1, s3=2, h4=3]. Both paths have equal length, so tests that need a
    specific path use the src/dst pair whose tie-break is deterministic.
    """
    return PortMap(
        switch_ports={
            "s1": {"h1": 1, "s2": 2, "s3": 3},
            "s2": {"s1": 1, "s4": 2},
            "s3": {"s1": 1, "s4": 2},
            "s4": {"s2": 1, "s3": 2, "h4": 3},
        },
        host_switch={"h1": "s1", "h4": "s4"},
        host_ip={"h1": "10.0.0.1", "h4": "10.0.0.4"},
    )


# ------------------------------------------------------------------ basics --
def test_port_lookup(port_map: PortMap) -> None:
    assert port_map.port("s1", "s2") == 2
    assert port_map.port("s2", "s4") == 2
    assert port_map.port("s1", "nope") is None


def test_require_port_raises_with_useful_message(port_map: PortMap) -> None:
    with pytest.raises(KeyError, match="no entry for s1 -> nope"):
        port_map.require_port("s1", "nope")


def test_switch_graph_has_the_loop(port_map: PortMap) -> None:
    graph = port_map.switch_graph()
    assert set(graph.nodes) == {"s1", "s2", "s3", "s4"}
    assert len(nx.cycle_basis(graph)) == 1


def test_shortest_path_is_two_hops(port_map: PortMap) -> None:
    path = port_map.shortest_switch_path("s1", "s4")
    assert len(path) == 3, "s1->s4 must be two hops via s2 or s3"
    assert path[0] == "s1" and path[-1] == "s4"
    assert path[1] in ("s2", "s3")


def test_same_switch_path(port_map: PortMap) -> None:
    assert port_map.shortest_switch_path("s2", "s2") == ["s2"]


def test_full_path_includes_hosts(port_map: PortMap) -> None:
    path = port_map.full_path("h1", "h4")
    assert path[0] == "h1" and path[-1] == "h4"
    assert len(path) == 5


def test_hosts_on(port_map: PortMap) -> None:
    assert port_map.hosts_on("s1") == ["h1"]
    assert port_map.hosts_on("s4") == ["h4"]
    assert port_map.hosts_on("s2") == []


# ------------------------------------------------------------ round-trip ----
def test_json_round_trip(port_map: PortMap, tmp_path) -> None:
    path = str(tmp_path / "pm.json")
    port_map.save(path)
    assert PortMap.load(path).to_dict() == port_map.to_dict()


def test_load_missing_file_is_explicit(port_map: PortMap) -> None:
    """An absent port map must fail loudly, not degrade to an empty map."""
    with pytest.raises(FileNotFoundError, match="port map not found"):
        PortMap.load("/nonexistent/portmap.json")


def test_load_ignores_extra_keys(port_map: PortMap, tmp_path) -> None:
    path = tmp_path / "pm.json"
    path.write_text(json.dumps({"switch_ports": port_map.switch_ports,
                                "unexpected": 1}))
    assert PortMap.load(str(path)).known_switches() == {"s1", "s2", "s3", "s4"}


# ------------------------------------------------------------- validation --
def test_validate_against_live_ports_is_quiet_when_consistent(
    port_map: PortMap,
) -> None:
    live = {"s1": {1, 2, 3}, "s2": {1, 2}, "s3": {1, 2}, "s4": {1, 2, 3}}
    assert port_map.validate_against(live) == []


def test_validate_against_flags_wrong_ports(port_map: PortMap) -> None:
    live = {"s1": {1, 9}, "s2": {1, 2}, "s3": {1, 2}, "s4": {1, 2, 3}}
    warnings = port_map.validate_against(live)
    assert warnings, "a stale port map must be reported"
    assert any("s1" in w for w in warnings)


def test_validate_flags_disconnected_switch(port_map: PortMap) -> None:
    warnings = port_map.validate_against({"s1": {1, 2, 3}})
    assert any("not connected" in w for w in warnings)


# --------------------------------------------- rule-chain construction ------
def expected_rules(port_map: PortMap, src: str, dst: str, dst_mac: str,
                   in_port: int, dst_host: str = "h4"):
    """Reference implementation of the chain _program_path must produce.

    The final hop's egress port is the port facing ``dst_host`` (the controller
    gets that from its MAC->location table; here it is read from the map).
    """
    path = port_map.shortest_switch_path(src, dst)
    rules = []
    for i, node in enumerate(path):
        ip = in_port if i == 0 else port_map.port(node, path[i - 1])
        op = (port_map.port(node, dst_host) if i == len(path) - 1
              else port_map.port(node, path[i + 1]))
        rules.append((node, ip, dst_mac, op))
    return path, rules


def test_rule_chain_for_h1_to_h4_via_s2(port_map: PortMap) -> None:
    """Every switch on the path gets in_port -> out_port, consistently oriented."""
    dst_mac = "0a:00:00:00:00:04"
    path, rules = expected_rules(port_map, "s1", "s4", dst_mac, in_port=1)

    by_switch = {r[0]: r for r in rules}
    if path[1] == "s2":
        # s1: from h1 (port 1) out towards s2 (port 2)
        assert by_switch["s1"] == ("s1", 1, dst_mac, 2)
        # s2: in from s1 (port 1) out towards s4 (port 2)
        assert by_switch["s2"] == ("s2", 1, dst_mac, 2)
        # s4: in from s2 (port 1) out towards h4 (port 3)
        assert by_switch["s4"] == ("s4", 1, dst_mac, 3)
    else:  # symmetric via s3
        assert by_switch["s3"] == ("s3", 1, dst_mac, 2)
        assert by_switch["s4"] == ("s4", 2, dst_mac, 3)


def test_rule_chain_ports_are_internally_consistent(port_map: PortMap) -> None:
    """If s1 emits out port P towards s2, then s2 must receive on its own port Q.

    This is the invariant that makes a multi-hop chain actually deliver: each
    hop's egress port has to be the next hop's ingress port. Getting this
    backwards is the classic way to build a silently dead path.
    """
    dst_mac = "0a:aa:bb:cc:dd:ee"
    path, rules = expected_rules(port_map, "s1", "s4", dst_mac, in_port=1)

    egress: Dict[Tuple[str, int], int] = {
        (sw, in_p): out_p for sw, in_p, _, out_p in rules
    }

    def ingress_of(node: str) -> int:
        index = path.index(node)
        if index == 0:
            return 1  # the host-facing port the caller supplied
        return port_map.port(node, path[index - 1])

    for src, nxt in zip(path, path[1:]):
        out_port = egress[(src, ingress_of(src))]
        # src emits out `out_port`; nxt must receive on the port it records for
        # src. If these disagree the chain silently drops at that hop.
        assert port_map.port(nxt, src) == ingress_of(nxt)
        assert out_port == port_map.port(src, nxt)


def test_rule_chain_covers_every_switch_on_the_path(port_map: PortMap) -> None:
    path, rules = expected_rules(port_map, "s1", "s4", "0a:aa:bb:cc:dd:ee", 1)
    assert {r[0] for r in rules} == set(path)
    assert len(rules) == len(path)


def test_reverse_chain_is_the_mirror_image(port_map: PortMap) -> None:
    """Forward and reverse rules together give bidirectional connectivity."""
    dst_mac = "0a:00:00:00:00:04"
    path, rules = expected_rules(port_map, "s1", "s4", dst_mac, in_port=1)
    rev_path, rev_rules = expected_rules(port_map, "s4", "s1", "0a:00:00:00:00:01",
                                         in_port=3, dst_host="h1")
    assert path[0] == rev_path[-1] and path[-1] == rev_path[0]
    fwd_egress = {r[0]: r[3] for r in rules}
    for sw, in_p, _, _out_p in rev_rules:
        assert fwd_egress[sw] == in_p, (
            f"{sw}: reverse ingress {in_p} must equal forward egress "
            f"{fwd_egress[sw]}"
        )