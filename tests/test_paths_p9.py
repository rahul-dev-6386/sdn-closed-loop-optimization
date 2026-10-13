"""Tests for P9 candidate path enumeration.

The graph used throughout is the project's own diamond, plus small variants that
isolate one behaviour each:

.. code-block:: text

    s1 --- s2 --- s4        path A: s1 -> s2 -> s4
      \\                 /
       \\--- s3 -------/        path B: s1 -> s3 -> s4
"""
from __future__ import annotations

from typing import Dict, List

import pytest

from routing.paths import (
    DisjointPathResult,
    build_graph,
    default_hop_cost,
    k_disjoint_candidates,
    neighbours,
    node_disjoint_paths,
    shortest_path,
    k_shortest_paths,
)

#: The diamond: s1 - s2 - s4 and s1 - s3 - s4.
DIAMOND_LINKS = [("s1", "s2"), ("s2", "s4"), ("s1", "s3"), ("s3", "s4")]


@pytest.fixture
def diamond() -> Dict[str, List[str]]:
    """Adjacency for the diamond topology."""
    return build_graph(DIAMOND_LINKS)


# ------------------------------------------------------------- graph --
def test_build_graph_is_bidirectional() -> None:
    g = build_graph([("s1", "s2")])
    assert g["s1"] == ["s2"]
    assert g["s2"] == ["s1"], "a one-way link still permits a return route"


def test_build_graph_deduplicates() -> None:
    g = build_graph([("s1", "s2"), ("s2", "s1")])
    assert g["s1"] == ["s2"]
    assert g["s2"] == ["s1"]


def test_build_graph_drops_self_loops() -> None:
    """A self-loop is a topology bug, and would make a path non-simple."""
    g = build_graph([("s1", "s1"), ("s1", "s2")])
    assert "s1" not in g.get("s1", [])
    assert g["s1"] == ["s2"]


def test_build_graph_registers_isolated_endpoints() -> None:
    g = build_graph([("s1", "s2")])
    assert set(g) == {"s1", "s2"}


def test_neighbours_excludes_removed_and_blocked() -> None:
    g = build_graph([("s1", "s2"), ("s1", "s3")])
    assert neighbours(g, "s1") == ["s2", "s3"]
    assert neighbours(g, "s1", frozenset({"s2"})) == ["s3"]


def test_default_hop_cost_counts_edges_not_nodes() -> None:
    assert default_hop_cost(("s1",)) == 0.0
    assert default_hop_cost(("s1", "s2", "s4")) == 2.0


# --------------------------------------------------------- shortest path --
def test_shortest_path_in_a_diamond(diamond) -> None:
    assert shortest_path(diamond, "s1", "s4") == ("s1", "s2", "s4")


def test_shortest_path_to_self_is_trivial(diamond) -> None:
    assert shortest_path(diamond, "s1", "s1") == ("s1",)


def test_unreachable_destination_returns_none(diamond) -> None:
    assert shortest_path(diamond, "s1", "s9") is None


def test_unknown_source_returns_none(diamond) -> None:
    assert shortest_path(diamond, "s9", "s4") is None


def test_shortest_path_avoids_a_blocked_node(diamond) -> None:
    assert shortest_path(diamond, "s1", "s4", blocked="s2") == ("s1", "s3", "s4")


def test_shortest_path_avoids_removed_nodes(diamond) -> None:
    assert shortest_path(diamond, "s1", "s4", removed=frozenset({"s2"})) == \
        ("s1", "s3", "s4")


def test_shortest_path_never_repeats_a_node() -> None:
    """A loop would make the path cost meaningless."""
    g = build_graph([("a", "b"), ("b", "c"), ("c", "a")])
    path = shortest_path(g, "a", "c")
    assert path is not None
    assert len(set(path)) == len(path)


def test_shortest_path_is_deterministic_under_ties() -> None:
    """Ties break by node name, so experiments are reproducible."""
    g = build_graph([("a", "z"), ("a", "m"), ("z", "d"), ("m", "d")])
    runs = {shortest_path(g, "a", "d") for _ in range(20)}
    assert len(runs) == 1


def test_shortest_path_respects_a_custom_cost() -> None:
    """A costly-but-short route loses to a cheap-but-long one."""
    g = build_graph([("a", "b"), ("b", "c"), ("a", "x"), ("x", "y"), ("y", "c")])
    edge = {"ab": 1.0, "bc": 1.0, "ax": 1.0, "xy": 1.0, "yc": 1.0}

    def cost(p):
        return sum(edge.get(f"{p[i]}{p[i+1]}", 1.0) for i in range(len(p) - 1))

    # Make the direct two-hop route expensive.
    edge["ab"] = edge["bc"] = 50.0
    assert shortest_path(g, "a", "c", cost) == ("a", "x", "y", "c")


# ------------------------------------------------- k-shortest paths --
def test_k_shortest_returns_the_shortest_first(diamond) -> None:
    paths = k_shortest_paths(diamond, "s1", "s4", k=2)
    assert paths[0] == ("s1", "s2", "s4")


def test_k_shortest_finds_the_second_route_in_a_diamond(diamond) -> None:
    paths = k_shortest_paths(diamond, "s1", "s4", k=2)
    assert len(paths) == 2
    assert ("s1", "s3", "s4") in paths


def test_k_shortest_respects_k(diamond) -> None:
    assert len(k_shortest_paths(diamond, "s1", "s4", k=1)) == 1


def test_k_shortest_returns_fewer_when_exhausted() -> None:
    g = build_graph([("a", "b"), ("b", "c"), ("a", "c")])
    assert len(k_shortest_paths(g, "a", "c", k=5)) == 2


def test_k_shortest_rejects_a_bad_k(diamond) -> None:
    for bad in (0, -1):
        with pytest.raises(ValueError, match="k must be positive"):
            k_shortest_paths(diamond, "s1", "s4", k=bad)


def test_k_shortest_returns_nothing_when_unreachable(diamond) -> None:
    assert k_shortest_paths(diamond, "s1", "s9", k=3) == []


def test_k_shortest_honours_max_hops(diamond) -> None:
    """A wandering route must not be offered as a candidate."""
    assert k_shortest_paths(diamond, "s1", "s4", k=3, max_hops=1) == []


def test_k_shortest_paths_are_simple_and_unique() -> None:
    g = build_graph([("a", "b"), ("b", "c"), ("a", "d"), ("d", "c"),
                     ("c", "e"), ("a", "e")])
    paths = k_shortest_paths(g, "a", "e", k=6)
    assert len(set(paths)) == len(paths)
    for path in paths:
        assert len(set(path)) == len(path), f"{path} revisits a node"


def test_k_shortest_finds_three_routes() -> None:
    g = build_graph([("a", "b"), ("b", "z"), ("a", "c"), ("c", "z"),
                     ("a", "d"), ("d", "z")])
    paths = k_shortest_paths(g, "a", "z", k=3)
    assert len(paths) == 3
    assert paths[0] == ("a", "b", "z"), "ordered by increasing cost"


# -------------------------------------------------- node-disjoint paths --
def test_diamond_yields_two_disjoint_paths(diamond) -> None:
    """The whole point: after a reroute, the new path avoids the old bottleneck."""
    result = node_disjoint_paths(diamond, "s1", "s4", k=2)
    assert set(result.paths) == {("s1", "s2", "s4"), ("s1", "s3", "s4")}
    assert result.fully_disjoint is True


def test_disjoint_paths_share_no_intermediate_node(diamond) -> None:
    result = node_disjoint_paths(diamond, "s1", "s4", k=2)
    interiors = [set(p[1:-1]) for p in result.paths]
    for i, a in enumerate(interiors):
        for b in interiors[i + 1:]:
            assert not (a & b), f"paths {result.paths} share {a & b}"


def test_endpoints_may_of_course_be_shared(diamond) -> None:
    result = node_disjoint_paths(diamond, "s1", "s4", k=2)
    for path in result.paths:
        assert path[0] == "s1" and path[-1] == "s4"


def test_incomplete_disjoint_set_is_flagged() -> None:
    """With one route only, the network has no spare capacity on this pair.

    The flag must say so: a controller that believes it has a safe alternative
    will move a flow onto a path that fails identically.
    """
    g = build_graph([("a", "b"), ("b", "c")])
    result = node_disjoint_paths(g, "a", "c", k=2)
    assert len(result.paths) == 1
    assert result.fully_disjoint is False


def test_disjoint_search_rejects_a_bad_k(diamond) -> None:
    with pytest.raises(ValueError, match="k must be positive"):
        node_disjoint_paths(diamond, "s1", "s4", k=0)


def test_disjoint_returns_nothing_when_unreachable(diamond) -> None:
    result = node_disjoint_paths(diamond, "s1", "s9", k=2)
    assert result.paths == []
    assert result.fully_disjoint is False


def test_shared_bottleneck_is_reported_honestly() -> None:
    """Both routes traverse x, so there is no independent alternative.

    This is the case where a search that maximises path *count* rather than
    independence would hand the controller two paths that fail identically. The
    flag must say so, and no bogus second path may be invented.
    """
    g = build_graph([("a", "x"), ("x", "c"), ("a", "y"), ("y", "x"),
                     ("x", "z"), ("z", "c")])
    result = node_disjoint_paths(g, "a", "c", k=2)
    assert result.fully_disjoint is False
    # Both enumerated routes pass through x; only one is reported as independent.
    for path in result.paths:
        assert path[0] == "a" and path[-1] == "c"
    interiors = [set(p[1:-1]) for p in result.paths]
    for i, a in enumerate(interiors):
        for b in interiors[i + 1:]:
            assert not (a & b), "reported paths must never share an interior node"


def test_disjoint_prefers_a_route_that_avoids_the_first_paths_interior() -> None:
    """Where independence *is* achievable, it must be found.

    s1-s2-s4 is shortest, but s1-s3-s2-s4 shares s2 with it. The result must offer
    the genuinely independent s1-s3-s4.
    """
    g = build_graph([("s1", "s2"), ("s2", "s4"), ("s1", "s3"), ("s3", "s4"),
                     ("s3", "s2")])
    result = node_disjoint_paths(g, "s1", "s4", k=2)
    assert result.fully_disjoint is True
    assert ("s1", "s3", "s4") in result.paths


def test_disjoint_result_serialises() -> None:
    result = DisjointPathResult([("a", "b", "c")], True)
    out = result.as_dict()
    assert out["count"] == 1
    assert out["fully_disjoint"] is True
    assert out["paths"] == [["a", "b", "c"]]


# --------------------------------------------------- candidate mix --
def test_candidates_put_disjoint_paths_first(diamond) -> None:
    paths = k_disjoint_candidates(diamond, "s1", "s4", k=2)
    assert len(paths) == 2
    assert set(paths) == {("s1", "s2", "s4"), ("s1", "s3", "s4")}


def test_candidates_top_up_when_fewer_disjoint_exist() -> None:
    """A single-route pair still gets alternatives, just not independent ones."""
    g = build_graph([("a", "b"), ("b", "c"), ("a", "c")])
    paths = k_disjoint_candidates(g, "a", "c", k=2)
    assert len(paths) == 2
    assert ("a", "b", "c") in paths and ("a", "c") in paths


def test_candidates_respect_k(diamond) -> None:
    assert len(k_disjoint_candidates(diamond, "s1", "s4", k=1)) == 1


def test_candidates_are_empty_when_unreachable(diamond) -> None:
    assert k_disjoint_candidates(diamond, "s1", "s9", k=2) == []
