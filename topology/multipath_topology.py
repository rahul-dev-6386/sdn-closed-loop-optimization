"""Multipath (diamond) SDN topology for H1 -> H4 traffic.

Shape (``topology.name: diamond``)::

              s2
             /  \\
      h1 -- s1    s4 -- h4
             \\  /
              s3

Two loop-free paths exist between the switches:

* **Path A** ``s1 -> s2 -> s4``
* **Path B** ``s1 -> s3 -> s4``

P2 only *builds* the topology and characterises each path in isolation. There is
no controller here (that is P3), so the module also installs **static OpenFlow
rules** to pin all traffic to one path at a time.

Isolation methods
-----------------
``static_flows`` (default)
    Every switch's flow table is cleared -- critically removing OVS's implicit
    ``priority=0 actions=NORMAL`` rule, which would otherwise flood unknown
    unicast/broadcast and, with the loop still up, cause a broadcast storm
    (design rule 1). Then one exact-match rule per hop is installed.
``link_down``
    Same rules as ``static_flows``, **plus** the unused path's inter-switch links
    are brought down with ``configLinkStatus`` so the graph becomes a tree.
    Downing links on their own cannot work: with no controller in P2 and no
    rules installed, nothing forwards at all. Used as a redundant-isolation
    cross-check.

Nothing in this module imports mininet at module scope, so the pure graph and
path logic can be unit-tested without root.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import networkx as nx

from common.config import Config, load_config
from common.logging_utils import setup_logging

LOGGER = logging.getLogger("topology.multipath")

Path = List[str]
"""A node sequence including both endpoints, e.g. ``['h1','s1','s2','s4','h4']``."""


# --------------------------------------------------------------------------- #
# Specification (pure data, no mininet)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Link:
    """A shaped inter-switch link."""

    a: str
    b: str
    bw_mbps: float
    delay_ms: float
    max_queue_size: int
    loss_pct: float = 0.0

    @property
    def name(self) -> str:
        """Canonical link identifier, e.g. ``s1--s2``.

        Endpoints are sorted so that a link declared as ``[s2, s1]`` produces the
        same name as ``[s1, s2]``. Without this, duplicate links written in
        opposite order would silently evade the duplicate check.
        """
        low, high = sorted((self.a, self.b))
        return f"{low}--{high}"

    def other(self, node: str) -> str:
        """Return the endpoint that is not ``node``."""
        if node == self.a:
            return self.b
        if node == self.b:
            return self.a
        raise KeyError(f"{node!r} is not an endpoint of {self.name}")

    def endpoints(self) -> Tuple[str, str]:
        """Return both endpoints as a tuple."""
        return self.a, self.b


@dataclass(frozen=True)
class Switch:
    """A switch with its expected port count."""

    name: str
    ports: int


@dataclass(frozen=True)
class Host:
    """A host attached to a switch."""

    name: str
    ip: str
    switch: str

    @property
    def address(self) -> str:
        """Host IP without the prefix length."""
        return str(ipaddress.ip_interface(self.ip).ip)


@dataclass(frozen=True)
class TopologySpec:
    """Immutable description of the network, buildable without root."""

    name: str
    switches: Dict[str, Switch]
    links: Dict[str, Link]
    hosts: Dict[str, Host]

    # ------------------------------------------------------------- lookups --
    def link_between(self, x: str, y: str) -> Optional[Link]:
        """Return the link joining ``x`` and ``y``, or None if they are not linked."""
        for link in self.links.values():
            if {link.a, link.b} == {x, y}:
                return link
        return None

    def host(self, name: str) -> Host:
        """Return the named host."""
        try:
            return self.hosts[name]
        except KeyError:
            raise KeyError(
                f"unknown host {name!r}; known hosts: {sorted(self.hosts)}"
            ) from None

    # -------------------------------------------------------------- graphs --
    def switch_graph(self) -> nx.Graph:
        """Undirected graph of switches only, with per-edge link metadata."""
        graph = nx.Graph(name=f"{self.name}-switches")
        for switch in self.switches.values():
            graph.add_node(switch.name, kind="switch")
        for link in self.links.values():
            graph.add_edge(
                link.a,
                link.b,
                link=link.name,
                bw_mbps=link.bw_mbps,
                delay_ms=link.delay_ms,
                max_queue_size=link.max_queue_size,
                loss_pct=link.loss_pct,
            )
        return graph

    def full_graph(self) -> nx.Graph:
        """Graph including hosts, so end-to-end paths can be enumerated."""
        graph = self.switch_graph()
        for host in self.hosts.values():
            graph.add_node(host.name, kind="host", ip=host.ip, switch=host.switch)
            graph.add_edge(
                host.name,
                host.switch,
                link=f"{host.name}--{host.switch}",
                bw_mbps=self.host_link_bw_mbps,
                delay_ms=self.host_link_delay_ms,
                kind="host",
            )
        return graph

    # host-link shaping is stored on the spec for path-cost maths
    host_link_bw_mbps: float = 100.0
    host_link_delay_ms: float = 1.0
    host_link_max_queue_size: int = 100

    # --------------------------------------------------------------- paths --
    def k_shortest_paths(
        self, source: str, dest: str, k: int = 3, weight: str = "hops"
    ) -> List[Path]:
        """Return up to ``k`` loop-free paths from ``source`` to ``dest``.

        Parameters
        ----------
        weight:
            ``hops`` minimises the number of links; ``delay_ms`` minimises
            accumulated one-way latency; ``bw_mbps`` maximises bottleneck
            bandwidth (implemented as a reciprocal weight).
        """
        graph = self.full_graph()
        for node, attrs in graph.nodes(data=True):
            attrs.setdefault("weight", 1.0)
        for u, v, attrs in graph.edges(data=True):
            if weight == "hops":
                attrs["_w"] = 1.0
            elif weight == "delay_ms":
                attrs["_w"] = float(attrs.get("delay_ms", 1.0))
            elif weight == "bw_mbps":
                attrs["_w"] = 1.0 / max(float(attrs.get("bw_mbps", 1.0)), 1e-9)
            else:
                raise ValueError(f"unknown path weight {weight!r}")
        for u, v, attrs in graph.edges(data=True):
            attrs["weight"] = attrs["_w"]
        return [list(p) for p in nx.shortest_simple_paths(graph, source, dest, weight="weight")][:k]

    def expected_rtt_ms(self, path: Sequence[str]) -> float:
        """Round-trip time predicted by the configured shaping.

        Each edge contributes its one-way delay in *both* directions, so the
        RTT is twice the sum of the delays along the path -- including the two
        host links.
        """
        one_way = self.path_one_way_delay_ms(path)
        if one_way <= 0.0:
            raise KeyError(f"path {list(path)} references an unknown hop")
        return 2.0 * one_way

    # ------------------------------------------------------ edge accessors --
    def edge_delay_ms(self, u: str, v: str) -> float:
        """One-way delay of the hop ``u``->``v``.

        Host links are not stored in :attr:`links` (they are derived from the
        single attached switch), so this falls back to the host-link shaping.
        """
        link = self.link_between(u, v)
        return link.delay_ms if link is not None else self.host_link_delay_ms

    def edge_bw_mbps(self, u: str, v: str) -> float:
        """Bandwidth of the hop ``u``->``v``, host links included."""
        link = self.link_between(u, v)
        return link.bw_mbps if link is not None else self.host_link_bw_mbps

    def path_bottleneck_bw(self, path: Sequence[str]) -> float:
        """Minimum link bandwidth along ``path`` (host links included)."""
        values = [self.edge_bw_mbps(u, v) for u, v in zip(path, path[1:])]
        return min(values) if values else 0.0

    def path_one_way_delay_ms(self, path: Sequence[str]) -> float:
        """Sum of one-way delays along ``path``, host links included."""
        return sum(self.edge_delay_ms(u, v) for u, v in zip(path, path[1:]))

    def path_inter_switch_links(self, path: Sequence[str]) -> List[Link]:
        """Return only the switch-to-switch links of ``path``."""
        out = []
        for u, v in zip(path, path[1:]):
            if u in self.switches and v in self.switches:
                out.append(self.link_between(u, v))  # type: ignore[arg-type]
        return out

    def describe(self) -> str:
        """Human-readable multi-line summary."""
        lines = [
            f"Topology '{self.name}'",
            f"  switches ({len(self.switches)}): "
            + ", ".join(f"{s.name}(ports={s.ports})" for s in self.switches.values()),
            f"  hosts ({len(self.hosts)}): "
            + ", ".join(f"{h.name}={h.ip}@{h.switch}" for h in self.hosts.values()),
            "  links:",
        ]
        for link in sorted(self.links.values(), key=lambda l: l.name):
            lines.append(
                f"    {link.name:<8} bw={link.bw_mbps:>6.1f} Mbps  "
                f"delay={link.delay_ms:>5.2f} ms  "
                f"queue={link.max_queue_size:>4} pkt"
                + (f"  loss={link.loss_pct}%" if link.loss_pct else "")
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Config -> spec
# --------------------------------------------------------------------------- #
def build_spec(config: Config) -> TopologySpec:
    """Build a :class:`TopologySpec` from ``config.yaml``.

    Per-link overrides are supported so asymmetric or deliberately degraded
    links can be modelled without editing the defaults::

        links:
          - [s1, s2]
          - {a: s1, b: s3, bw_mbps: 4.0, delay_ms: 20.0}
    """
    topo = config.section("topology")

    switches = {
        str(item["name"]): Switch(name=str(item["name"]), ports=int(item.get("ports", 4)))
        for item in topo.require("switches")
    }
    hosts = {
        str(item["name"]): Host(
            name=str(item["name"]),
            ip=str(item["ip"]),
            switch=str(item["switch"]),
        )
        for item in topo.require("hosts")
    }

    host_bw = float(topo.get("host_bw_mbps", 100.0))
    host_delay = float(topo.get("host_delay_ms", 1.0))
    host_queue = int(topo.get("host_max_queue_size", 100))
    link_bw = float(topo.get("link_bw_mbps", 10.0))
    link_delay = float(topo.get("link_delay_ms", 5.0))
    link_queue = int(topo.get("link_max_queue_size", 100))
    link_loss = float(topo.get("congestion_loss_pct", 0.0))

    links: Dict[str, Link] = {}
    for raw in topo.require("links"):
        if isinstance(raw, dict):
            a, b = str(raw["a"]), str(raw["b"])
            overrides = raw
        else:
            a, b = str(raw[0]), str(raw[1])
            overrides = {}
        for endpoint in (a, b):
            if endpoint not in switches:
                raise ValueError(
                    f"topology.links references unknown switch {endpoint!r}"
                )
        link = Link(
            a=a,
            b=b,
            bw_mbps=float(overrides.get("bw_mbps", link_bw)),
            delay_ms=float(overrides.get("delay_ms", link_delay)),
            max_queue_size=int(overrides.get("max_queue_size", link_queue)),
            loss_pct=float(overrides.get("loss_pct", link_loss)),
        )
        if link.name in links:
            raise ValueError(f"duplicate link {link.name} in topology.links")
        links[link.name] = link

    for host in hosts.values():
        if host.switch not in switches:
            raise ValueError(
                f"host {host.name!r} is attached to unknown switch {host.switch!r}"
            )

    # Every switch needs at least one free port per incident link plus hosts.
    for switch in switches.values():
        incident = sum(
            1 for link in links.values() if switch.name in link.endpoints()
        )
        attached = sum(1 for h in hosts.values() if h.switch == switch.name)
        needed = incident + attached
        if switch.ports < needed:
            raise ValueError(
                f"switch {switch.name} declares ports={switch.ports} but needs "
                f"{needed} ({incident} inter-switch + {attached} host)"
            )

    return TopologySpec(
        name=str(topo.get("name", "diamond")),
        switches=switches,
        links=links,
        hosts=hosts,
        host_link_bw_mbps=host_bw,
        host_link_delay_ms=host_delay,
        host_link_max_queue_size=host_queue,
    )


def topology_paths(spec: TopologySpec, source: str, dest: str) -> List[Path]:
    """Enumerate all simple h1->h4 paths, ordered deterministically.

    Ordering is by summed link delay then by hop count then lexicographically,
    so P2/P9 output is reproducible run to run.
    """
    graph = spec.full_graph()
    raw = [list(p) for p in nx.all_simple_paths(graph, source, dest)]
    return sorted(
        raw,
        key=lambda p: (spec.path_one_way_delay_ms(p), len(p), p),
    )


# --------------------------------------------------------------------------- #
# Static flow plan (pure -- the unprivileged part of path isolation)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FlowRule:
    """One static OpenFlow rule pinned to a switch."""

    switch: str
    in_port: int
    out_port: int
    description: str

    def ofctl_flow(self) -> str:
        """Render as an ``ovs-ofctl add-flow`` flow string."""
        return f"in_port={self.in_port},actions=output:{self.out_port}"


def build_flow_plan(
    spec: TopologySpec,
    path: Sequence[str],
    port_of: Any,
    bidirectional: bool = True,
) -> List[FlowRule]:
    """Generate the static rules that pin all traffic to ``path``.

    Parameters
    ----------
    spec:
        Topology description.
    path:
        Node sequence including hosts, e.g. ``['h1','s1','s2','s4','h4']``.
    port_of:
        Callable ``(switch_name, neighbour_name) -> port_number``. Injected so
        this function stays testable without a live network.
    bidirectional:
        Also emit the reverse-direction rules.

    Returns
    -------
    list[FlowRule]
        Rules for the forward direction, and, when ``bidirectional``, the
        reverse direction as well.

    Notes
    -----
    ARP needs no special handling: a request enters on the host's port and is
    forwarded along the same static chain, so it resolves without a controller.
    """
    if len(path) < 3:
        raise ValueError(f"path {list(path)} is too short to carry traffic")

    rules: List[FlowRule] = []

    # Forward: for each switch, map the incoming neighbour's port to the
    # outgoing neighbour's port.
    for index in range(1, len(path) - 1):
        node = path[index]
        if node not in spec.switches:
            continue
        upstream, downstream = path[index - 1], path[index + 1]
        in_port = port_of(node, upstream)
        out_port = port_of(node, downstream)
        rules.append(
            FlowRule(
                switch=node,
                in_port=in_port,
                out_port=out_port,
                description=f"{upstream}->{downstream} via {node}",
            )
        )

    if bidirectional:
        for index in range(len(path) - 2, 0, -1):
            node = path[index]
            if node not in spec.switches:
                continue
            upstream, downstream = path[index - 1], path[index + 1]
            in_port = port_of(node, downstream)
            out_port = port_of(node, upstream)
            rules.append(
                FlowRule(
                    switch=node,
                    in_port=in_port,
                    out_port=out_port,
                    description=f"{downstream}->{upstream} via {node} (reverse)",
                )
            )

    return rules


def tc_delay(delay_ms: float) -> str:
    """Format a millisecond delay for ``tc netem``.

    ``tc`` treats a unit-less number as **nanoseconds**, so passing ``5.0``
    produces ``delay 5ns`` instead of ``5ms`` -- a 1,000,000x error that
    silently flattens all latency measurements. Mininet's own docstring expects
    a string such as ``'1ms'``, so always pass the unit explicitly.

    >>> tc_delay(5.0)
    '5.0ms'
    >>> tc_delay(1)
    '1.0ms'
    """
    return f"{float(delay_ms):g}ms"


# --------------------------------------------------------------------------- #
# Live network (requires root)
# --------------------------------------------------------------------------- #
class MultipathTopology:
    """Build and tear down the Mininet network described by a spec.

    Requires root: Mininet creates network namespaces and OpenFlow datapaths.

    Parameters
    ----------
    spec:
        Topology to build.
    controller:
        ``None`` for a loop-safe-free static-flow test (default), or a
        ``(ip, port)`` tuple to attach a remote controller such as
        ``ryu-manager``.
    datapath_type:
        ``"kernel"`` for the in-kernel datapath, ``"netdev"`` for the
        userspace fallback.
    """

    def __init__(
        self,
        spec: TopologySpec,
        controller: Optional[Tuple[str, int]] = None,
        datapath_type: str = "kernel",
        protocols: str = "OpenFlow13",
    ) -> None:
        self.spec = spec
        self.controller = controller
        self.datapath_type = datapath_type
        self.protocols = protocols
        self.net: Any = None
        #: Mininet ``Link`` objects returned by ``addLink``. Mininet 2.3.x's Node
        #: exposes only ``intfs``/``ports`` (no ``connections`` map), so the
        #: authoritative port<->neighbour adjacency comes from the Link objects,
        #: which carry both endpoints and both interfaces. This avoids assuming
        #: Mininet's ``<switch>-eth<N>`` naming or port-allocation order.
        self.link_objects: List[Any] = []

    # ------------------------------------------------------------ lifecycle --
    def build(self) -> Any:
        """Create the Mininet network and start all nodes."""
        from functools import partial

        from mininet.net import Mininet
        from mininet.link import TCLink
        from mininet.node import Controller as MininetController
        from mininet.node import Host as MininetHost
        from mininet.node import OVSSwitch
        from mininet.node import RemoteController

        spec = self.spec
        if self.controller is None:
            LOGGER.info("building topology with NO controller (static flows only)")
        else:
            ip, port = self.controller
            LOGGER.info("building topology with remote controller %s:%s", ip, port)

        # NOTE: Mininet.__init__ does not forward **kwargs to the switch class,
        # so switch options (protocols, failMode, inBand, datapath_type) must be
        # baked into the class via functools.partial.
        switch_kwargs: Dict[str, Any] = {
            "protocols": self.protocols,
            "failMode": "secure",
            "inBand": False,
        }
        if self.datapath_type == "netdev":
            switch_kwargs["datapath_type"] = "netdev"
        SwitchClass = partial(OVSSwitch, **switch_kwargs)

        self.net = Mininet(
            switch=SwitchClass,
            host=MininetHost,
            # 'None' == no controller. Passing controller=None to Mininet() means
            # "no controller" too, but the parameter is a NAME string, so it can
            # never carry a configured instance -- a controller must be added
            # explicitly with addController() below.
            controller="None",
            build=False,
            autoSetMacs=True,
            autoStaticArp=False,
        )

        # Attach the remote controller when one was requested. Without this the
        # switches never talk to ryu-manager and every table-miss is silently
        # dropped, so the net looks "up" but carries nothing.
        if self.controller is not None:
            ip, port = self.controller
            self.net.addController(
                RemoteController(name="c0", ip=ip, port=port, inNamespace=False)
            )

        switches = [self.net.addSwitch(s.name) for s in spec.switches.values()]
        hosts = {
            h.name: self.net.addHost(h.name, ip=h.ip) for h in spec.hosts.values()
        }

        for link in spec.links.values():
            # NOTE: the Python API needs the link CLASS, not the CLI string
            # "tc" that `mn --link tc` uses.
            mn_link = self.net.addLink(
                link.a,
                link.b,
                cls=TCLink,
                bw=link.bw_mbps,
                delay=tc_delay(link.delay_ms),
                max_queue_size=link.max_queue_size,
                loss=link.loss_pct,
            )
            self.link_objects.append(mn_link)
        for host in spec.hosts.values():
            mn_link = self.net.addLink(
                host.switch,
                host.name,
                cls=TCLink,
                bw=spec.host_link_bw_mbps,
                delay=tc_delay(spec.host_link_delay_ms),
                max_queue_size=spec.host_link_max_queue_size,
            )
            self.link_objects.append(mn_link)

        self.net.build()
        self.net.start()
        if self.controller is not None:
            self._attach_controller_to_bridges(*self.controller)
        LOGGER.info("topology '%s' is up", spec.name)
        return self.net

    def _attach_controller_to_bridges(self, ip: str, port: int) -> None:
        """Attach the remote controller to every bridge, across OVS versions.

        Mininet 2.3.x's ``OVSSwitch.start()`` issues::

            ovs-vsctl ... set bridge s1 controllers=[<uuid>]

        That column is **plural**, and it was **removed in Open vSwitch 4.0**,
        where the Bridge table exposes a singular ``controller`` column
        instead. On OVS 4.0 Mininet's command fails, the bridge ends up with no
        controller at all, and the symptom is deeply misleading: the network
        looks healthy, ``is_connected`` rows exist, yet every table-miss is
        silently dropped because nothing is ever listening.

        This shim attaches the controller explicitly, preferring the OVS >= 4.0
        singular column and falling back to the legacy plural one.
        """
        import subprocess

        target = f"tcp:{ip}:{port}"

        def vsctl(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["ovs-vsctl", "--timeout=10", *args],
                capture_output=True, text=True, check=False,
            )

        uuids = [
            u for u, t in zip(
                vsctl("--bare", "--columns=_uuid,target", "list", "Controller")
                .stdout.split(),
                vsctl("--bare", "--columns=_uuid,target", "list", "Controller")
                .stdout.split()[1::2],
            )
            if t == target
        ]
        if not uuids:
            LOGGER.warning(
                "no OVS Controller row with target %s; cannot attach", target
            )
            return
        controller_uuid = uuids[0]
        bridges = vsctl("list-br").stdout.split()
        for bridge in bridges:
            # OVS >= 4.0: singular `controller`.
            result = vsctl("set", "bridge", bridge, f"controller={controller_uuid}")
            if result.returncode == 0:
                LOGGER.info("attached %s to bridge %s (OVS>=4.0 'controller')",
                            target, bridge)
                continue
            # Legacy OVS: plural `controllers` as a set.
            result = vsctl("set", "bridge", bridge,
                           f"controllers=[{controller_uuid}]")
            if result.returncode == 0:
                LOGGER.info("attached %s to bridge %s (legacy 'controllers')",
                            target, bridge)
            else:
                LOGGER.warning(
                    "could not attach controller to %s: %s",
                    bridge, result.stderr.strip(),
                )

    def stop(self) -> None:
        """Stop and delete the network, tolerating partial construction."""
        if self.net is not None:
            try:
                self.net.stop()
            finally:
                self.net = None

    # ----------------------------------------------------------- inspection --
    def port_of(self, switch_name: str, neighbour_name: str) -> int:
        """Return the port number on ``switch_name`` facing ``neighbour_name``.

        Resolved from the :attr:`link_objects` recorded at build time.
        """
        for link in self.link_objects:
            # Mininet 2.3.x Link exposes intf1/intf2 (each Intf knows its .node);
            # there is no node1/node2 attribute on Link itself.
            node1, intf1 = link.intf1.node, link.intf1
            node2, intf2 = link.intf2.node, link.intf2
            if node1.name == switch_name and node2.name == neighbour_name:
                return int(node1.ports[intf1])
            if node2.name == switch_name and node1.name == neighbour_name:
                return int(node2.ports[intf2])
        raise KeyError(
            f"no port on {switch_name} facing {neighbour_name}; "
            f"known links: "
            f"{[(l.intf1.node.name, l.intf2.node.name) for l in self.link_objects]}"
        )

    def export_portmap(self) -> Any:
        """Export a :class:`controller.portmap.PortMap` for the live network.

        The controller needs switch-to-switch port adjacency to install
        shortest-path forwarding (P3); the P3 controller loads it from here via
        ``controller.portmap.PortMap.load``. P4 replaces this side-channel with
        live LLDP/``OFPPortDesc`` discovery.
        """
        from controller.portmap import export_portmap as _export

        if self.net is None:
            raise RuntimeError("network not built; call build() first")
        return _export(self.link_objects, self.spec)

    def paths_with_ports(self, source: str, dest: str) -> List[Tuple[Path, List[FlowRule]]]:
        """Enumerate paths and resolve their static flow rules against live ports."""
        out = []
        for path in topology_paths(self.spec, source, dest):
            rules = build_flow_plan(
                self.spec,
                path,
                port_of=lambda sw, nb: self.port_of(sw, nb),
                bidirectional=True,
            )
            out.append((path, rules))
        return out


class StaticFlowInstaller:
    """Install and clear static OpenFlow rules on OVS switches.

    Uses ``ovs-ofctl`` directly, which requires root only for reading the OVS
    database, not for namespace access.

    Safety: :meth:`clear_switches` removes OVS's implicit ``priority=0
    actions=NORMAL`` rule. With that rule present, any packet not matched by our
    exact-match rules would be flooded out every port; because the diamond has a
    loop, that is a broadcast storm. Removing it first is what makes static-flow
    isolation safe here (design rule 1).
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._ofctl = "ovs-ofctl"

    def _run(self, args: Sequence[str]) -> str:
        """Run ``ovs-ofctl`` with the given arguments and return stdout."""
        cmd = [self._ofctl, "-OOpenFlow13", *args]
        LOGGER.debug("running %s", " ".join(cmd))
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self.timeout, check=False
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"{' '.join(cmd)} failed (rc={proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout

    def dump(self, switch: str) -> str:
        """Return the raw flow table of ``switch``."""
        return self._run(["dump-flows", switch])

    def clear_switches(self, switches: Iterable[str]) -> None:
        """Delete every flow on each switch, including the default NORMAL rule."""
        for switch in switches:
            self._run(["del-flows", switch])
            LOGGER.info("cleared flow table on %s (incl. any NORMAL flood rule)", switch)

    def install(self, rules: Sequence[FlowRule]) -> List[str]:
        """Install ``rules`` and return the ovs-ofctl commands actually run."""
        issued = []
        for rule in rules:
            args = ["add-flow", rule.switch, rule.ofctl_flow()]
            self._run(args)
            issued.append(" ".join([self._ofctl, "-OOpenFlow13", *args]))
            LOGGER.info(
                "installed %s: %s in_port=%d -> output:%d",
                rule.switch,
                rule.description,
                rule.in_port,
                rule.out_port,
            )
        return issued

    def flow_count(self, switch: str) -> int:
        """Number of flow entries currently on ``switch``."""
        return len([l for l in self.dump(switch).splitlines() if "cookie" in l])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the multipath diamond topology.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument(
        "--topo-only",
        action="store_true",
        help="attach the remote controller and block until interrupted (for P3)",
    )
    parser.add_argument(
        "--controller-ip", default="127.0.0.1", help="remote controller address"
    )
    parser.add_argument(
        "--controller-port", type=int, default=6653, help="remote controller port"
    )
    parser.add_argument(
        "--no-controller",
        action="store_true",
        help="build with no controller (static flows only)",
    )
    parser.add_argument(
        "--datapath",
        default=None,
        choices=("kernel", "netdev"),
        help="OVS datapath type (default: topology.datapath_type)",
    )
    parser.add_argument(
        "--dump-graph", action="store_true", help="print the topology graph and exit"
    )
    parser.add_argument(
        "--paths",
        nargs=2,
        metavar=("SRC", "DST"),
        help="print all simple paths between two nodes and exit",
    )
    parser.add_argument(
        "--dump-portmap",
        metavar="PATH",
        help="build the network, write the port map to PATH, then exit",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``python -m topology.multipath_topology``."""
    args = _parse_args(argv)
    config = load_config(args.config)
    setup_logging(config)

    spec = build_spec(config)

    if args.dump_graph:
        print(spec.describe())
        graph = spec.switch_graph()
        print(f"\nswitch graph: {graph.number_of_nodes()} nodes, "
              f"{graph.number_of_edges()} edges, cycles={len(nx.cycle_basis(graph))}")
        for cycle in nx.cycle_basis(graph):
            print(f"  cycle: {' -> '.join(cycle)} -> {cycle[0]}")
        return 0

    if args.paths:
        src, dst = args.paths
        print(spec.describe())
        for index, path in enumerate(topology_paths(spec, src, dst)):
            print(
                f"  path {index}: {' -> '.join(path)}  "
                f"predicted_rtt={spec.expected_rtt_ms(path):.2f} ms  "
                f"bottleneck={spec.path_bottleneck_bw(path):.1f} Mbps"
            )
        return 0

    if args.dump_portmap:
        topo = MultipathTopology(spec, controller=None, datapath_type="kernel")
        topo.build()
        try:
            port_map = topo.export_portmap()
            port_map.save(args.dump_portmap)
            print(port_map.describe())
        finally:
            topo.stop()
        return 0

    datapath = args.datapath or str(config.get("topology.datapath_type", "kernel"))
    controller: Optional[Tuple[str, int]] = None
    if not args.no_controller:
        controller = (args.controller_ip, args.controller_port)

    topo = MultipathTopology(
        spec, controller=controller, datapath_type=datapath
    )
    topo.build()
    LOGGER.info("switches: %s", ", ".join(sorted(spec.switches)))
    LOGGER.info("hosts: %s", ", ".join(sorted(spec.hosts)))
    for index, path in enumerate(topology_paths(spec, "h1", "h4")):
        LOGGER.info(
            "path %d: %s (predicted rtt %.2f ms)",
            index,
            " -> ".join(path),
            spec.expected_rtt_ms(path),
        )

    if args.topo_only or controller is not None:
        LOGGER.info(
            "--topo-only: topology is up, waiting for the remote controller "
            "at %s:%s (Ctrl-C to stop)",
            args.controller_ip,
            args.controller_port,
        )
        stop = {"now": False}

        def _handler(signum: int, _frame: Any) -> None:
            LOGGER.info("received signal %s, shutting down", signum)
            stop["now"] = True

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
        while not stop["now"]:
            signal.pause()
        topo.stop()
        LOGGER.info("topology stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())