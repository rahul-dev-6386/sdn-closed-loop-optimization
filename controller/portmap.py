"""Port adjacency map: which port of which switch faces which neighbour.

Why this exists
---------------
Shortest-path forwarding (design rule 3 / P3) needs two facts the controller
cannot learn by watching packet-ins, because the loop forbids flooding:

* which **host** port sits behind which MAC, and
* which **switch-to-switch** port faces which neighbour.

Host ports are learned from ARP requests. Switch-to-switch ports cannot be, since
no frame ever traverses them in a way the controller observes. In P3 this map is
captured once by the topology builder (:func:`export_portmap`) and loaded from
JSON.

**P4 replaces this with live discovery** (LLDP / ``OFPPortDesc``) so the
controller no longer depends on a side-channel file. Until then the map is
validated against the live datapaths at load time: if a port in the map is not
present on the connected switch, a warning is logged rather than silently
producing dead flows.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import networkx as nx

LOGGER = logging.getLogger("controller.portmap")


@dataclass
class PortMap:
    """Bidirectional neighbour lookup for switches and hosts.

    Attributes
    ----------
    switch_ports:
        ``{switch_name: {neighbour_name: port_number}}`` covering both
        switch-to-switch and switch-to-host neighbours.
    host_switch:
        ``{host_name: switch_name}``.
    host_ip:
        ``{host_name: ip_address}``.
    switch_dpid:
        ``{switch_name: datapath_id}``.

        Required because OpenFlow identifies a datapath by its numeric dpid, not
        by Mininet's name: a switch named ``s1`` arrives at the controller with
        ``datapath.id == 1``. Without this mapping the controller cannot relate
        its connected datapaths to the port map's switch names.
    """

    switch_ports: Dict[str, Dict[str, int]] = field(default_factory=dict)
    host_switch: Dict[str, str] = field(default_factory=dict)
    host_ip: Dict[str, str] = field(default_factory=dict)
    switch_dpid: Dict[str, int] = field(default_factory=dict)

    def dpid_of(self, switch: str) -> Optional[int]:
        """Return the datapath id of ``switch``, or None if unknown."""
        return self.switch_dpid.get(switch)

    def switch_of_dpid(self, dpid: Any) -> Optional[str]:
        """Reverse lookup: datapath id -> switch name."""
        try:
            wanted = int(dpid)
        except (TypeError, ValueError):
            return None
        for name, value in self.switch_dpid.items():
            if value == wanted:
                return name
        return None

    # ------------------------------------------------------------- queries --
    def port(self, switch: str, neighbour: str) -> Optional[int]:
        """Return the port on ``switch`` facing ``neighbour``, or None."""
        return self.switch_ports.get(switch, {}).get(neighbour)

    def require_port(self, switch: str, neighbour: str) -> int:
        """Like :meth:`port` but raises when the mapping is absent."""
        value = self.port(switch, neighbour)
        if value is None:
            raise KeyError(
                f"port map has no entry for {switch} -> {neighbour}; "
                f"known neighbours of {switch}: "
                f"{sorted(self.switch_ports.get(switch, {}))}"
            )
        return value

    def known_switches(self) -> Set[str]:
        """Switches present in the map."""
        return set(self.switch_ports)

    def hosts_on(self, switch: str) -> List[str]:
        """Hosts attached to ``switch``."""
        return sorted(h for h, s in self.host_switch.items() if s == switch)

    # -------------------------------------------------------------- graphs --
    def switch_graph(self) -> nx.Graph:
        """Graph of switches only, with one edge per inter-switch link."""
        graph = nx.Graph()
        for switch, neighbours in self.switch_ports.items():
            graph.add_node(switch)
            for neighbour, port in neighbours.items():
                if neighbour in self.switch_ports:
                    graph.add_edge(switch, neighbour, port=port)
        return graph

    def shortest_switch_path(self, src: str, dst: str) -> List[str]:
        """Shortest switch sequence from ``src`` to ``dst`` (inclusive).

        Raises
        ------
        nx.NetworkXNoPath
            If the switches are not connected.
        """
        graph = self.switch_graph()
        return list(nx.shortest_path(graph, src, dst))

    def full_path(self, src_host: str, dst_host: str) -> List[str]:
        """Node sequence host -> ... -> host for the shortest path.

        Example: ``['h1', 's1', 's2', 's4', 'h4']``.
        """
        src_switch = self.host_switch[src_host]
        dst_switch = self.host_switch[dst_host]
        if src_switch == dst_switch:
            return [src_host, src_switch, dst_host]
        middle = self.shortest_switch_path(src_switch, dst_switch)
        return [src_host, *middle, dst_host]

    # -------------------------------------------------------- serialisation --
    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "switch_ports": {k: dict(sorted(v.items()))
                             for k, v in sorted(self.switch_ports.items())},
            "host_switch": dict(sorted(self.host_switch.items())),
            "host_ip": dict(sorted(self.host_ip.items())),
            "switch_dpid": {k: int(v) for k, v in sorted(self.switch_dpid.items())},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PortMap":
        """Rebuild a map from :meth:`to_dict` output."""
        return cls(
            switch_ports={
                str(k): {str(n): int(p) for n, p in v.items()}
                for k, v in (data.get("switch_ports") or {}).items()
            },
            host_switch={str(k): str(v)
                         for k, v in (data.get("host_switch") or {}).items()},
            host_ip={str(k): str(v)
                     for k, v in (data.get("host_ip") or {}).items()},
            switch_dpid={str(k): int(v)
                         for k, v in (data.get("switch_dpid") or {}).items()},
        )

    def save(self, path: str) -> str:
        """Write the map to ``path`` as JSON, returning the path."""
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
        LOGGER.info("port map written to %s", path)
        return path

    @classmethod
    def load(cls, path: str) -> "PortMap":
        """Read a map from ``path``.

        Raises
        ------
        FileNotFoundError
            If the file does not exist -- an explicit failure, because
            silently falling back to an empty map would drop every packet.
        """
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"port map not found: {path}\n"
                f"Generate it with topology/multipath_topology.py --dump-portmap "
                f"(P4 will replace this with live discovery)."
            )
        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    # ------------------------------------------------------------- checking --
    def validate_against(self, datapath_ports: Dict[str, Set[int]]) -> List[str]:
        """Check the map against live switch port numbers.

        Parameters
        ----------
        datapath_ports:
            ``{switch_name: {port numbers reported by the switch}}``.

        Returns
        -------
        list[str]
            Human-readable warnings for every entry that does not match the
            live datapath. An empty list means the map is consistent.
        """
        warnings: List[str] = []
        for switch, neighbours in self.switch_ports.items():
            live = datapath_ports.get(switch)
            if live is None:
                warnings.append(f"{switch}: not connected; map entries unverified")
                continue
            for neighbour, port in sorted(neighbours.items()):
                if port not in live:
                    warnings.append(
                        f"{switch}: map says port {port} faces {neighbour}, "
                        f"but live ports are {sorted(live)}"
                    )
        return warnings

    def describe(self) -> str:
        """Multi-line human-readable dump."""
        lines = ["Port map:"]
        for switch in sorted(self.switch_ports):
            neighbours = ", ".join(
                f"{n}={p}" for n, p in sorted(self.switch_ports[switch].items())
            )
            lines.append(f"  {switch}: {neighbours}")
        return "\n".join(lines)


def export_portmap(link_objects: Iterable[Any], spec: Any) -> PortMap:
    """Build a :class:`PortMap` from Mininet ``Link`` objects.

    Parameters
    ----------
    link_objects:
        The objects returned by ``Mininet.addLink``. Each exposes ``intf1`` and
        ``intf2``, and every ``Intf`` knows its ``.node``, so the port number on
        either side is ``intf.node.ports[intf]``. This is authoritative and
        assumes nothing about Mininet's interface naming or port-allocation
        order.
    spec:
        The :class:`~topology.multipath_topology.TopologySpec` used to build it.
    """
    port_map = PortMap()
    for switch_name in spec.switches:
        port_map.switch_ports.setdefault(switch_name, {})

    for link in link_objects:
        node1, intf1 = link.intf1.node, link.intf1
        node2, intf2 = link.intf2.node, link.intf2
        # Record each switch's OpenFlow datapath id while we have the node.
        for node in (node1, node2):
            dpid = getattr(node, "dpid", None)
            if node.name in port_map.switch_ports and dpid is not None:
                try:
                    port_map.switch_dpid[node.name] = int(dpid, 16) \
                        if isinstance(dpid, str) else int(dpid)
                except (TypeError, ValueError):
                    pass
        if node1.name in port_map.switch_ports:
            port_map.switch_ports[node1.name][node2.name] = int(node1.ports[intf1])
        if node2.name in port_map.switch_ports:
            port_map.switch_ports[node2.name][node1.name] = int(node2.ports[intf2])

    for host_name, host in spec.hosts.items():
        port_map.host_switch[host_name] = host.switch
        port_map.host_ip[host_name] = host.address
    LOGGER.info("exported port map for %d switches", len(port_map.switch_ports))
    return port_map


def datapath_port_sets(datapaths: Dict[str, Any]) -> Dict[str, Set[int]]:
    """Extract live port numbers from connected Ryu datapaths.

    Parameters
    ----------
    datapaths:
        ``{switch_name: Datapath}`` as held by the controller (name-keyed).
    """
    out: Dict[str, Set[int]] = {}
    for name, datapath in datapaths.items():
        ports = set()
        try:
            for port_no in datapath.ports:
                ports.add(int(port_no))
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.debug("could not read ports for %s: %s", name, exc)
        out[name] = ports
    return out