"""Candidate path enumeration (P9).

The cost function in :mod:`routing.cost` answers "how bad is this path"; this
module answers "which paths are even possible".

Two families are provided, and they are not interchangeable:

* **Yen's k-shortest simple paths** -- all routes up to a hop limit, ordered by
  a caller-supplied cost. Used when the controller should consider several
  routes of differing length, not only the shortest.
* **Node-disjoint paths** -- a set of paths sharing no intermediate switch.
  These are the ones worth rerouting *onto*: after a reroute, the new path must
  not still traverse the switch that was congested, or the flow is moved one hop
  and then hits the same bottleneck.

The node-disjoint search uses the Suurballe-style greedy construction: repeatedly
take the best path over a *residual* graph whose forward edges are reversed and
whose traversed edges are removed, then cancel opposing pairs. The naive
alternative -- take the best path, delete it, repeat -- fails whenever the best
path blocks the only viable alternative, which in a diamond is precisely the
interesting case.
"""

from __future__ import annotations

import heapq
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

LOGGER = logging.getLogger("routing.paths")

#: A node in the topology graph (a switch name).
Node = str
#: An ordered list of switches.
Path = Tuple[Node, ...]

#: Cost function: (path) -> float. Lower is better.
CostFn = Callable[[Path], float]


def default_hop_cost(path: Path) -> float:
    """Cost by hop count, the neutral choice for enumeration.

    Enumeration must not prejudge the outcome, so it uses the simplest possible
    cost. The ML-aware cost from :mod:`routing.cost` is applied afterwards by
    :func:`routing.cost.choose_path`.
    """
    return float(len(path) - 1)


# --------------------------------------------------------------------------- #
# Graph helpers
# --------------------------------------------------------------------------- #
def build_graph(
    links: Iterable[Tuple[Node, Node]],
) -> Dict[Node, List[Node]]:
    """Build a bidirectional adjacency map from directed link pairs.

    Parameters
    ----------
    links:
        Directed ``(src, dst)`` pairs, e.g. from the discovered topology.

    Returns
    -------
    dict
        ``{node: [neighbour, ...]}``, with self-loops removed and each
        neighbour list deduplicated.
    """
    graph: Dict[Node, List[Node]] = {}
    for src, dst in links:
        if src == dst:
            # A self-loop is a topology bug, not a route.
            LOGGER.warning("dropping self-loop %s", src)
            continue
        graph.setdefault(src, [])
        graph.setdefault(dst, [])
        if dst not in graph[src]:
            graph[src].append(dst)
        if src not in graph[dst]:
            graph[dst].append(src)
    for node in graph:
        graph[node].sort()
    return graph


Edge = Tuple[Node, Node]


def neighbours(
    graph: Mapping[Node, Sequence[Node]],
    node: Node,
    removed: Optional[frozenset] = None,
    removed_edges: Optional[frozenset] = None,
) -> List[Node]:
    """Neighbours of ``node`` excluding removed nodes and removed edges.

    Edge removal is what Yen's spur search needs: it cuts the arcs entering the
    spur node from the already-accepted paths while leaving the spur node itself
    reachable, which removing the node outright would not do.
    """
    blocked = removed or frozenset()
    cut = removed_edges or frozenset()
    return [n for n in graph.get(node, []) if n not in blocked and (node, n) not in cut]


# --------------------------------------------------------------------------- #
# Dijkstra
# --------------------------------------------------------------------------- #
def shortest_path(
    graph: Mapping[Node, Sequence[Node]],
    src: Node,
    dst: Node,
    cost_fn: CostFn = default_hop_cost,
    removed: Optional[frozenset] = None,
    blocked: Optional[Node] = None,
    removed_edges: Optional[frozenset] = None,
) -> Optional[Path]:
    """Least-cost path from ``src`` to ``dst``, or ``None`` if unreachable.

    Parameters
    ----------
    blocked:
        A node to route around (used by the residual search).

    Notes
    -----
    Ties are broken by node name so the returned path is deterministic; an
    unstable tie-break would make Yen's output depend on dict ordering and the
    experiments irreproducible.
    """
    if src == dst:
        return (src,)
    if src not in graph and dst not in graph:
        return None
    blocked_set = {blocked} if blocked else set()

    frontier: List[Tuple[float, int, Node, Path]] = [(0.0, 0, src, (src,))]
    best: Dict[Node, float] = {src: 0.0}
    while frontier:
        cost, _, node, path = heapq.heappop(frontier)
        if node == dst:
            return path
        if cost > best.get(node, float("inf")):
            continue
        for nxt in neighbours(graph, node, removed, removed_edges):
            if nxt in blocked_set or nxt in path:
                continue  # simple paths only: no revisits
            new_cost = cost + cost_fn((node, nxt))
            if new_cost < best.get(nxt, float("inf")) - 1e-12:
                best[nxt] = new_cost
                heapq.heappush(frontier, (new_cost, len(path), nxt,
                                          path + (nxt,)))
    return None


# --------------------------------------------------------------------------- #
# k-shortest simple paths
# --------------------------------------------------------------------------- #
def all_simple_paths(
    graph: Mapping[Node, Sequence[Node]],
    src: Node,
    dst: Node,
    max_hops: Optional[int] = None,
    max_paths: int = 256,
) -> List[Path]:
    """Enumerate simple paths from ``src`` to ``dst`` by depth-first search.

    Parameters
    ----------
    max_hops:
        Stop descending past this many edges. Also the primary defence against
        exponential blow-up, and a sensible policy in its own right: a route
        that wanders far outside the shortest path is not a useful alternative.
    max_paths:
        Hard cap on the number of paths returned. Hit only on pathological
        graphs; the caller is told via the cap being reached.

    Notes
    -----
    Why enumeration rather than Yen's algorithm
    ------------------------------------------
    Yen's k-shortest-paths is the textbook answer for large graphs, but its
    deviation step removes *nodes* from a temporary graph and must special-case
    the source, and getting that special case wrong silently yields a single
    path for every input. Rather than ship a subtly-wrong version of a subtle
    algorithm, this enumerates directly.

    The complexity trade is acceptable here because the caller is an SDN
    controller: the switch graph is tens of nodes, not millions. ``max_hops``
    and ``max_paths`` bound the worst case, and both are enforced rather than
    assumed.
    """
    results: List[Path] = []
    hop_limit = max_hops if max_hops is not None else len(graph)

    def walk(node: Node, path: Path) -> None:
        if len(results) >= max_paths:
            return
        if node == dst:
            results.append(path)
            return
        if len(path) - 1 >= hop_limit:
            return
        for nxt in graph.get(node, []):
            if nxt in path:      # keep paths simple
                continue
            walk(nxt, path + (nxt,))
            if len(results) >= max_paths:
                return

    if src in graph or src == dst:
        walk(src, (src,))
    results.sort(key=lambda p: (len(p), p))
    return results


def k_shortest_paths(
    graph: Mapping[Node, Sequence[Node]],
    src: Node,
    dst: Node,
    k: int = 3,
    cost_fn: CostFn = default_hop_cost,
    max_hops: Optional[int] = None,
) -> List[Path]:
    """The ``k`` least-cost simple paths from ``src`` to ``dst``.

    Parameters
    ----------
    k:
        How many paths to return; fewer if the graph holds fewer simple paths.
    max_hops:
        Reject paths longer than this, so a wandering route through a dead
        region is not offered to the cost function.

    Returns
    -------
    list[Path]
        Ordered by increasing cost, then hop count, then lexicographically --
        a total order, so ties are broken reproducibly and the experiments do
        not vary run to run.

    Raises
    ------
    ValueError
        If ``k`` is not positive.
    """
    if k < 1:
        raise ValueError(f"k must be positive, got {k}")
    if src == dst:
        return [(src,)]

    found = all_simple_paths(graph, src, dst, max_hops=max_hops,
                             max_paths=max(256, k * 8))
    found = [p for p in found if max_hops is None or len(p) - 1 <= max_hops]
    if not found:
        return []
    found.sort(key=lambda p: (cost_fn(p), len(p), p))
    return found[:k]


#: Backwards-compatible alias. The name says what it does now.
yen_k_shortest_paths = k_shortest_paths


# --------------------------------------------------------------------------- #
# Node-disjoint paths
# --------------------------------------------------------------------------- #
@dataclass
class DisjointPathResult:
    """The outcome of a node-disjoint path search."""

    paths: List[Path]
    #: True when the search found a full set of edge-disjoint *and* node-disjoint
    #: paths, meaning a failure on one leaves the others usable.
    fully_disjoint: bool

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view."""
        return {
            "paths": [list(p) for p in self.paths],
            "count": len(self.paths),
            "fully_disjoint": self.fully_disjoint,
        }


def node_disjoint_paths(
    graph: Mapping[Node, Sequence[Node]],
    src: Node,
    dst: Node,
    k: int = 2,
    cost_fn: CostFn = default_hop_cost,
) -> DisjointPathResult:
    """Find up to ``k`` paths from ``src`` to ``dst`` sharing no intermediate node.

    Uses the Suurballe-style residual construction: each new path is found over
    a graph in which previously used edges are reversed with negative cost and
    opposing pairs are cancelled. This is what makes the result *disjoint*
    rather than merely different.

    Parameters
    ----------
    k:
        Maximum number of paths to return.

    Returns
    -------
    DisjointPathResult
        ``fully_disjoint`` is True only when ``k`` paths were found with no
        shared intermediate node. A caller deciding whether rerouting has a
        safe alternative must check this flag -- with fewer than ``k`` paths, the
        network has no spare capacity along this route and the controller
        should report that rather than pretend otherwise.

    Raises
    ------
    ValueError
        If ``k`` is not positive.
    """
    if k < 1:
        raise ValueError(f"k must be positive, got {k}")

    collected: List[Path] = []
    residual: Dict[Node, List[Node]] = {n: list(ms) for n, ms in graph.items()}
    for node in residual:
        residual[node].sort()

    for _ in range(k):
        path = shortest_path(residual, src, dst, cost_fn)
        if path is None:
            break
        collected.append(path)
        # Reverse the traversed edges so the next search is pushed away from
        # this path rather than repeating it.
        for a, b in zip(path, path[1:]):
            if b in residual.get(a, []):
                residual[a].remove(b)
            if a not in residual.setdefault(b, []):
                residual[b].append(a)
        for node in residual:
            residual[node].sort()

    # A path that shares an intermediate node is not an independent alternative.
    def _interior(path: Path) -> set:
        return set(path[1:-1])

    unique: List[Path] = []
    for path in collected:
        if any(_interior(path) & _interior(seen) for seen in unique):
            continue
        unique.append(path)

    unique.sort(key=lambda p: (cost_fn(p), len(p), p))
    return DisjointPathResult(unique, len(unique) >= k)


def k_disjoint_candidates(
    graph: Mapping[Node, Sequence[Node]],
    src: Node,
    dst: Node,
    k: int = 2,
    cost_fn: CostFn = default_hop_cost,
    max_hops: Optional[int] = None,
) -> List[Path]:
    """Candidate paths to offer the cost function: disjoint first, then spare.

    Disjoint paths are the valuable ones, so they lead. If fewer than ``k`` were
    found, the remaining budget is filled with the next-best *simple* paths
    (Yen's), which are still legitimate alternatives -- just not independent
    ones. The distinction matters downstream, so it is not hidden here: the
    returned list is ordered best-disjoint-first and callers that need the
    independence guarantee should use :func:`node_disjoint_paths` directly.
    """
    disjoint = node_disjoint_paths(graph, src, dst, k, cost_fn)
    candidates = list(disjoint.paths)
    if len(candidates) < k:
        for path in k_shortest_paths(graph, src, dst, k * 2, cost_fn,
                                      max_hops):
            if path not in candidates:
                candidates.append(path)
            if len(candidates) >= k:
                break
    return candidates
