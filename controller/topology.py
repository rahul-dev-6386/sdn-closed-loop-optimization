"""Dynamic topology discovery and NetworkX graph management for OpenFlow 1.3.

Provides :class:`TopologyTracker` which:
1. Tracks switches, ports, host locations, and inter-switch links in memory.
2. Maintains a :class:`networkx.Graph` with link capacities (bandwidth, delay,
   queue size, loss) mapped from ``config.yaml``.
3. Computes k-shortest loop-free paths between any two nodes using Yen's algorithm
   (``nx.shortest_simple_paths``) over hop count, latency, or reciprocal bandwidth.
4. Generates and parses standard LLDP discovery frames to dynamically learn
   switch-to-switch links without broadcast storms.
5. Exposes the exact query interface required by :class:`controller.portmap.PortMap`
   so the controller can operate either purely in-memory or save to disk for
   tooling/API backward-compatibility.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import networkx as nx
from ryu.lib.packet import ether_types, ethernet, lldp, packet
from ryu.ofproto import ofproto_v1_3

from common.config import Config, load_config
from common.logging_utils import setup_logging

LOGGER = logging.getLogger("controller.topology")


# --------------------------------------------------------------------------- #
# LLDP constants and helper functions
# --------------------------------------------------------------------------- #
# Standard LLDP multicast MAC (IEEE 802.1AB nearest bridge)
LLDP_DST_MAC = "01:80:c2:00:00:0e"
ETH_TYPE_LLDP = ether_types.ETH_TYPE_LLDP


def dpid_to_str(dpid: int) -> str:
    """Format numeric DPID into 16-hex-digit string."""
    return f"{dpid:016x}"


def str_to_dpid(dpid_str: str) -> int:
    """Parse 16-hex-digit string into numeric DPID."""
    return int(dpid_str, 16)


def build_lldp_packet(
    dpid: int,
    port_no: int,
    src_mac: str = "0e:00:00:00:00:01",
    ttl: int = 120,
) -> bytes:
    """Build a standard IEEE 802.1AB LLDP packet for switch/port identification.

    Parameters
    ----------
    dpid:
        Numeric datapath ID.
    port_no:
        Port number emitting this LLDP frame.
    src_mac:
        Source Ethernet MAC.
    ttl:
        Time-to-live in seconds.
    """
    pkt = packet.Packet()
    eth = ethernet.ethernet(
        dst=LLDP_DST_MAC,
        src=src_mac,
        ethertype=ETH_TYPE_LLDP,
    )
    pkt.add_protocol(eth)

    # Chassis ID: locally assigned string containing dpid
    chassis_id_str = f"dpid:{dpid_to_str(dpid)}"
    tlv_chassis = lldp.ChassisID(
        subtype=lldp.ChassisID.SUB_LOCALLY_ASSIGNED,
        chassis_id=chassis_id_str.encode("ascii"),
    )

    # Port ID: port component (4-byte binary integer for OF1.3 ports)
    tlv_port = lldp.PortID(
        subtype=lldp.PortID.SUB_PORT_COMPONENT,
        port_id=struct.pack("!I", port_no),
    )

    tlv_ttl = lldp.TTL(ttl=ttl)
    tlv_end = lldp.End()

    lldp_payload = lldp.lldp(tlvs=(tlv_chassis, tlv_port, tlv_ttl, tlv_end))
    pkt.add_protocol(lldp_payload)
    pkt.serialize()
    return bytes(pkt.data)


def parse_lldp_packet(data: bytes) -> Optional[Tuple[int, int]]:
    """Parse an LLDP packet and return (dpid, port_no), or None if not an LLDP frame.

    Raises no exceptions on malformed frames; returns None with a debug log.
    """
    try:
        pkt = packet.Packet(data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype != ETH_TYPE_LLDP:
            return None

        lldp_proto = pkt.get_protocol(lldp.lldp)
        if lldp_proto is None or len(lldp_proto.tlvs) < 3:
            return None

        tlv_chassis = lldp_proto.tlvs[0]
        chassis_bytes = getattr(tlv_chassis, "chassis_id", b"")
        if isinstance(chassis_bytes, bytes):
            chassis_str = chassis_bytes.decode("ascii", errors="ignore")
        else:
            chassis_str = str(chassis_bytes)

        if not chassis_str.startswith("dpid:"):
            return None
        src_dpid = str_to_dpid(chassis_str.split("dpid:")[1])

        tlv_port = lldp_proto.tlvs[1]
        port_bytes = getattr(tlv_port, "port_id", b"")
        if len(port_bytes) == 4:
            (src_port,) = struct.unpack("!I", port_bytes)
        elif len(port_bytes) == 2:
            (src_port,) = struct.unpack("!H", port_bytes)
        else:
            # Fallback to string representation if encoded as string
            src_port = int(port_bytes.decode("ascii", errors="ignore"))

        return src_dpid, src_port
    except Exception as exc:
        LOGGER.debug("failed to parse LLDP frame: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Discovered Link Dataclass
# --------------------------------------------------------------------------- #
@dataclass
class DiscoveredLink:
    """Represents a discovered unidirectional or bidirectional switch link."""

    src_switch: str
    src_port: int
    dst_switch: str
    dst_port: int
    bw_mbps: float = 10.0
    delay_ms: float = 5.0
    max_queue_size: int = 100
    loss_pct: float = 0.0
    last_seen: float = 0.0

    @property
    def canonical_name(self) -> str:
        """Normalized sorted endpoint identifier (e.g. ``s1--s2``)."""
        low, high = sorted((self.src_switch, self.dst_switch))
        return f"{low}--{high}"


# --------------------------------------------------------------------------- #
# Topology Tracker (NetworkX + PortMap Compatibility)
# --------------------------------------------------------------------------- #
class TopologyTracker:
    """Dynamic network topology model backed by NetworkX.

    Maintains the discovered network graph and link attributes loaded from
    ``config.yaml``. Compatible with the queries of :class:`controller.portmap.PortMap`.
    """

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config if config is not None else load_config()
        self.graph = nx.Graph(name="sdn-topology")
        # switch_name -> {neighbour_name: port_number}
        self.switch_ports: Dict[str, Dict[str, int]] = {}
        # host_name -> switch_name
        self.host_switch: Dict[str, str] = {}
        # host_name -> ip_address
        self.host_ip: Dict[str, str] = {}
        # host_name -> mac_address
        self.host_mac: Dict[str, str] = {}
        # host_name -> switch_port
        self.host_port: Dict[str, int] = {}
        # set of (switch_name, port_number) identifying host attachments
        self.host_ports: Set[Tuple[str, int]] = set()
        # switch_name -> datapath_id
        self.switch_dpid: Dict[str, int] = {}
        # dpid -> switch_name
        self.dpid_to_name_map: Dict[int, str] = {}
        # canonical_name -> DiscoveredLink
        self.links: Dict[str, DiscoveredLink] = {}

        # Default parameters from config
        topo_cfg = self.config.get("topology", {})
        self.default_link_bw = float(topo_cfg.get("link_bw_mbps", 10.0))
        self.default_link_delay = float(topo_cfg.get("link_delay_ms", 5.0))
        self.default_link_queue = int(topo_cfg.get("link_max_queue_size", 100))
        self.default_host_bw = float(topo_cfg.get("host_bw_mbps", 100.0))
        self.default_host_delay = float(topo_cfg.get("host_delay_ms", 1.0))
        self.default_host_queue = int(topo_cfg.get("host_max_queue_size", 100))

        # Preload known static topology spec if present in config
        self._init_from_config()

    def _init_from_config(self) -> None:
        """Pre-populate static configuration from config.yaml if available."""
        topo_cfg = self.config.get("topology", {})
        switches = topo_cfg.get("switches", [])
        for sw in switches:
            name = sw.get("name")
            if name:
                # Default DPID if naming is s<num> (e.g. s1 -> 1)
                dpid = int(name[1:]) if name.startswith("s") and name[1:].isdigit() else None
                self.add_switch(name, dpid=dpid)

        hosts = topo_cfg.get("hosts", [])
        for h in hosts:
            name = h.get("name")
            ip = h.get("ip", "").split("/")[0]
            sw = h.get("switch")
            if name and sw:
                self.add_host(name, ip=ip, switch=sw, port=None)

    def _get_configured_link_attrs(self, u: str, v: str) -> Dict[str, Any]:
        """Look up configured bandwidth, delay, and queue size for link (u, v)."""
        topo_cfg = self.config.get("topology", {})
        declared_links = topo_cfg.get("links", [])
        u_s, v_s = sorted((u, v))

        for entry in declared_links:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                if sorted((entry[0], entry[1])) == [u_s, v_s]:
                    return {
                        "bw_mbps": self.default_link_bw,
                        "delay_ms": self.default_link_delay,
                        "max_queue_size": self.default_link_queue,
                        "loss_pct": 0.0,
                    }
            elif isinstance(entry, dict):
                a = entry.get("a")
                b = entry.get("b")
                if sorted((a, b)) == [u_s, v_s]:
                    return {
                        "bw_mbps": float(entry.get("bw_mbps", self.default_link_bw)),
                        "delay_ms": float(entry.get("delay_ms", self.default_link_delay)),
                        "max_queue_size": int(entry.get("max_queue_size", self.default_link_queue)),
                        "loss_pct": float(entry.get("loss_pct", 0.0)),
                    }

        return {
            "bw_mbps": self.default_link_bw,
            "delay_ms": self.default_link_delay,
            "max_queue_size": self.default_link_queue,
            "loss_pct": 0.0,
        }

    # --------------------------------------------------------- Registration --
    def add_switch(self, switch: str, dpid: Optional[int] = None) -> None:
        """Register a switch in the graph."""
        self.switch_ports.setdefault(switch, {})
        if dpid is not None:
            self.switch_dpid[switch] = int(dpid)
            self.dpid_to_name_map[int(dpid)] = switch
        if not self.graph.has_node(switch):
            self.graph.add_node(switch, kind="switch", dpid=dpid)

    def remove_switch(self, switch: str) -> None:
        """Remove a disconnected switch and all its incident links."""
        self.switch_ports.pop(switch, None)
        dpid = self.switch_dpid.pop(switch, None)
        if dpid is not None:
            self.dpid_to_name_map.pop(dpid, None)
        for other, nbrs in list(self.switch_ports.items()):
            nbrs.pop(switch, None)
        # Remove incident links from links dictionary
        for k in list(self.links.keys()):
            if switch in k.split("--"):
                self.links.pop(k, None)
        if self.graph.has_node(switch):
            self.graph.remove_node(switch)

    def switch_name_of(self, dpid_or_name: Any) -> str:
        """Resolve DPID or name into a canonical switch name."""
        if isinstance(dpid_or_name, int) or (isinstance(dpid_or_name, str) and dpid_or_name.isdigit()):
            dpid = int(dpid_or_name)
            return self.dpid_to_name_map.get(dpid, f"s{dpid}")
        return str(dpid_or_name)

    def add_link(
        self,
        src: Any,
        src_port: int,
        dst: Any,
        dst_port: int,
        bw_mbps: Optional[float] = None,
        delay_ms: Optional[float] = None,
    ) -> DiscoveredLink:
        """Record a discovered link between two switches."""
        src_name = self.switch_name_of(src)
        dst_name = self.switch_name_of(dst)

        self.add_switch(src_name)
        self.add_switch(dst_name)

        self.switch_ports[src_name][dst_name] = int(src_port)
        self.switch_ports[dst_name][src_name] = int(dst_port)

        attrs = self._get_configured_link_attrs(src_name, dst_name)
        if bw_mbps is not None:
            attrs["bw_mbps"] = float(bw_mbps)
        if delay_ms is not None:
            attrs["delay_ms"] = float(delay_ms)

        link_obj = DiscoveredLink(
            src_switch=src_name,
            src_port=int(src_port),
            dst_switch=dst_name,
            dst_port=int(dst_port),
            bw_mbps=attrs["bw_mbps"],
            delay_ms=attrs["delay_ms"],
            max_queue_size=attrs["max_queue_size"],
            loss_pct=attrs["loss_pct"],
            last_seen=time.monotonic(),
        )
        self.links[link_obj.canonical_name] = link_obj

        # Add or update edge in NetworkX graph
        self.graph.add_edge(
            src_name,
            dst_name,
            src_port=src_port,
            dst_port=dst_port,
            bw_mbps=attrs["bw_mbps"],
            delay_ms=attrs["delay_ms"],
            max_queue_size=attrs["max_queue_size"],
            loss_pct=attrs["loss_pct"],
            link=link_obj.canonical_name,
            kind="switch",
        )
        LOGGER.info(
            "discovered link %s:%d <-> %s:%d (bw=%.1f Mbps, delay=%.1f ms)",
            src_name, src_port, dst_name, dst_port, attrs["bw_mbps"], attrs["delay_ms"],
        )
        return link_obj

    def remove_link(self, src: Any, dst: Any) -> bool:
        """Remove a link between two switches (e.g. on port down)."""
        src_name = self.switch_name_of(src)
        dst_name = self.switch_name_of(dst)
        removed = False

        if src_name in self.switch_ports and dst_name in self.switch_ports[src_name]:
            del self.switch_ports[src_name][dst_name]
            removed = True
        if dst_name in self.switch_ports and src_name in self.switch_ports[dst_name]:
            del self.switch_ports[dst_name][src_name]
            removed = True

        low, high = sorted((src_name, dst_name))
        canon = f"{low}--{high}"
        self.links.pop(canon, None)

        if self.graph.has_edge(src_name, dst_name):
            self.graph.remove_edge(src_name, dst_name)
            removed = True

        if removed:
            LOGGER.info("removed link between %s and %s", src_name, dst_name)
        return removed

    def remove_link_on_port(self, switch: str, port: int) -> List[str]:
        """Remove any switch link attached to (switch, port)."""
        removed = []
        for nbr, p in list(self.switch_ports.get(switch, {}).items()):
            if p == port and nbr in self.switch_ports:
                if self.remove_link(switch, nbr):
                    removed.append(nbr)
        return removed

    def check_link_timeouts(self, timeout_s: float, now: Optional[float] = None) -> List[str]:
        """Remove links not refreshed by LLDP within timeout_s."""
        if now is None:
            now = time.monotonic()
        timed_out = []
        for canon, link in list(self.links.items()):
            if link.last_seen > 0 and (now - link.last_seen) > timeout_s:
                self.remove_link(link.src_switch, link.dst_switch)
                timed_out.append(canon)
        return timed_out

    def is_host_port(self, switch: str, port: int) -> bool:
        """Return True if (switch, port) is known to face a host."""
        return (switch, int(port)) in self.host_ports

    def add_host(
        self,
        host: str,
        ip: str,
        switch: str,
        port: Optional[int] = None,
        mac: Optional[str] = None,
    ) -> None:
        """Associate a host with its ingress switch and port."""
        self.host_switch[host] = switch
        self.host_ip[host] = ip
        if mac is not None:
            self.host_mac[host] = mac
        if port is not None:
            self.host_port[host] = int(port)
            self.switch_ports.setdefault(switch, {})[host] = int(port)
            self.host_ports.add((switch, int(port)))

        # Graph node for host
        if not self.graph.has_node(host):
            self.graph.add_node(host, kind="host", ip=ip, switch=switch, mac=mac)
        if not self.graph.has_edge(host, switch):
            self.graph.add_edge(
                host,
                switch,
                bw_mbps=self.default_host_bw,
                delay_ms=self.default_host_delay,
                max_queue_size=self.default_host_queue,
                kind="host",
                link=f"{host}--{switch}",
            )

    # ------------------------------------------------------------- Queries --
    def dpid_of(self, switch: str) -> Optional[int]:
        """Return the datapath id of ``switch``, or None if unknown."""
        return self.switch_dpid.get(switch)

    def switch_of_dpid(self, dpid: Any) -> Optional[str]:
        """Reverse lookup: datapath id -> switch name."""
        try:
            wanted = int(dpid)
        except (TypeError, ValueError):
            return None
        return self.dpid_to_name_map.get(wanted)

    def port(self, switch: str, neighbour: str) -> Optional[int]:
        """Return the port on ``switch`` facing ``neighbour``, or None."""
        return self.switch_ports.get(switch, {}).get(neighbour)

    def require_port(self, switch: str, neighbour: str) -> int:
        """Return port on ``switch`` facing ``neighbour`` or raise KeyError."""
        value = self.port(switch, neighbour)
        if value is None:
            raise KeyError(
                f"topology tracker has no entry for {switch} -> {neighbour}; "
                f"known neighbours of {switch}: "
                f"{sorted(self.switch_ports.get(switch, {}))}"
            )
        return value

    def known_switches(self) -> Set[str]:
        """Set of known switch names."""
        return set(self.switch_ports)

    def hosts_on(self, switch: str) -> List[str]:
        """List of hosts attached to ``switch``."""
        return sorted(h for h, s in self.host_switch.items() if s == switch)

    # ------------------------------------------------------- Graph & Paths --
    def switch_graph(self) -> nx.Graph:
        """Return sub-graph containing only switches."""
        switches = [n for n, d in self.graph.nodes(data=True) if d.get("kind") != "host"]
        return self.graph.subgraph(switches).copy()

    def full_graph(self) -> nx.Graph:
        """Return the complete graph (switches and hosts)."""
        return self.graph.copy()

    def shortest_switch_path(self, src: str, dst: str) -> List[str]:
        """Shortest switch sequence from ``src`` to ``dst`` (inclusive)."""
        sw_graph = self.switch_graph()
        return list(nx.shortest_path(sw_graph, src, dst))

    def full_path(self, src_host: str, dst_host: str) -> List[str]:
        """Full node sequence from src_host -> switches -> dst_host."""
        src_sw = self.host_switch[src_host]
        dst_sw = self.host_switch[dst_host]
        if src_sw == dst_sw:
            return [src_host, src_sw, dst_host]
        middle = self.shortest_switch_path(src_sw, dst_sw)
        return [src_host, *middle, dst_host]

    def k_shortest_paths(
        self, source: str, dest: str, k: int = 3, weight: str = "hops"
    ) -> List[List[str]]:
        """Return up to ``k`` loop-free simple paths between ``source`` and ``dest``.

        Parameters
        ----------
        source, dest:
            Node names (hosts or switches).
        k:
            Maximum number of paths to return.
        weight:
            - ``hops``: Minimizes the number of links.
            - ``delay_ms``: Minimizes total one-way delay.
            - ``bw_mbps``: Maximizes bottleneck bandwidth (reciprocal cost).
        """
        g = self.full_graph()
        for u, v, data in g.edges(data=True):
            if weight == "hops":
                data["_w"] = 1.0
            elif weight == "delay_ms":
                data["_w"] = float(data.get("delay_ms", 1.0))
            elif weight == "bw_mbps":
                bw = float(data.get("bw_mbps", 1.0))
                data["_w"] = 1.0 / max(bw, 1e-9)
            else:
                raise ValueError(f"unknown path weight {weight!r}")

        paths_gen = nx.shortest_simple_paths(g, source, dest, weight="_w")
        paths: List[List[str]] = []
        try:
            for p in paths_gen:
                paths.append(list(p))
                if len(paths) >= k:
                    break
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []
        return paths

    def path_delay_ms(self, path: Sequence[str]) -> float:
        """Sum of one-way delays along ``path``."""
        total = 0.0
        for u, v in zip(path, path[1:]):
            edge_data = self.graph.get_edge_data(u, v)
            if edge_data:
                total += float(edge_data.get("delay_ms", 0.0))
            else:
                total += self.default_link_delay
        return total

    def expected_rtt_ms(self, path: Sequence[str]) -> float:
        """Round-trip time predicted by summing one-way delays in both directions."""
        return 2.0 * self.path_delay_ms(path)

    def path_bottleneck_bw(self, path: Sequence[str]) -> float:
        """Minimum link bandwidth along ``path``."""
        bws = []
        for u, v in zip(path, path[1:]):
            edge_data = self.graph.get_edge_data(u, v)
            if edge_data:
                bws.append(float(edge_data.get("bw_mbps", self.default_link_bw)))
            else:
                bws.append(self.default_link_bw)
        return min(bws) if bws else 0.0

    # ---------------------------------------------------- PortMap Export --
    def to_portmap(self) -> Any:
        """Export state into a :class:`controller.portmap.PortMap` object."""
        from controller.portmap import PortMap

        return PortMap(
            switch_ports={k: dict(v) for k, v in self.switch_ports.items()},
            host_switch=dict(self.host_switch),
            host_ip=dict(self.host_ip),
            switch_dpid=dict(self.switch_dpid),
        )

    def save(self, path: str) -> str:
        """Write the current topology map as JSON for backward compatibility."""
        portmap = self.to_portmap()
        return portmap.save(path)

    def export_topology_dict(self) -> Dict[str, Any]:
        """Return full JSON-serializable dictionary of live topology state."""
        return {
            "switches": sorted(self.known_switches()),
            "switch_dpid": {k: int(v) for k, v in sorted(self.switch_dpid.items())},
            "hosts": {
                h: {
                    "switch": s,
                    "port": self.host_port.get(h),
                    "ip": self.host_ip.get(h),
                    "mac": self.host_mac.get(h),
                }
                for h, s in sorted(self.host_switch.items())
            },
            "links": [
                {
                    "src": l.src_switch,
                    "src_port": l.src_port,
                    "dst": l.dst_switch,
                    "dst_port": l.dst_port,
                    "bw_mbps": l.bw_mbps,
                    "delay_ms": l.delay_ms,
                    "last_seen": l.last_seen,
                }
                for l in sorted(self.links.values(), key=lambda x: x.canonical_name)
            ],
            "paths_h1_h4": self.k_shortest_paths("h1", "h4", k=3),
            "timestamp": time.time(),
        }

    def validate_against(self, datapath_ports: Dict[str, Set[int]]) -> List[str]:
        """Check the topology map against live switch port numbers."""
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

    def load_from_portmap_file(self, path: str) -> bool:
        """Load external portmap JSON and merge into tracker."""
        if not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            for sw, neighbours in data.get("switch_ports", {}).items():
                for nbr, port in neighbours.items():
                    if nbr in data.get("switch_ports", {}):
                        dst_port = data["switch_ports"][nbr].get(sw, 1)
                        self.add_link(sw, port, nbr, dst_port)
                    elif nbr in data.get("host_switch", {}):
                        ip = data.get("host_ip", {}).get(nbr, "")
                        self.add_host(nbr, ip=ip, switch=sw, port=port)
            for sw, dpid in data.get("switch_dpid", {}).items():
                self.switch_dpid[sw] = int(dpid)
                self.dpid_to_name_map[int(dpid)] = sw
            return True
        except Exception as exc:
            LOGGER.warning("could not load portmap %s: %s", path, exc)
            return False

    def describe(self) -> str:
        """Return a formatted human-readable summary of the topology."""
        switches = sorted(self.known_switches())
        hosts = sorted(self.host_switch.items())
        lines = [
            f"Topology Graph (nodes={self.graph.number_of_nodes()}, edges={self.graph.number_of_edges()})",
            f"  Switches ({len(switches)}): "
            + ", ".join(f"{s}(dpid={self.switch_dpid.get(s, '?')})" for s in switches),
            f"  Hosts ({len(hosts)}): "
            + ", ".join(f"{h}={self.host_ip.get(h, '')}@{s}" for h, s in hosts),
            "  Links:",
        ]
        for u, v, data in sorted(
            self.graph.edges(data=True), key=lambda e: sorted((e[0], e[1]))
        ):
            kind = data.get("kind", "switch")
            bw = data.get("bw_mbps", 0.0)
            delay = data.get("delay_ms", 0.0)
            q = data.get("max_queue_size", 0)
            lines.append(
                f"    {u:<4} <-> {v:<4}  bw={bw:>6.1f} Mbps  delay={delay:>5.2f} ms  "
                f"queue={q:>4} pkt  ({kind})"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI entrypoint
# --------------------------------------------------------------------------- #
def main() -> None:
    """CLI to inspect topology graph and k-shortest paths."""
    parser = argparse.ArgumentParser(description="P4: SDN Topology Discovery & Path Inspection")
    parser.add_argument("--paths", nargs=2, metavar=("SRC", "DST"), help="Compute k-shortest paths")
    parser.add_argument("-k", type=int, default=3, help="Number of paths to find (default: 3)")
    parser.add_argument(
        "--weight",
        choices=["hops", "delay_ms", "bw_mbps"],
        default="hops",
        help="Weight metric for path search",
    )
    parser.add_argument("--portmap", help="Optional portmap JSON file to load")
    parser.add_argument("--graph", action="store_true", help="Print topology graph summary")
    args = parser.parse_args()

    cfg = load_config()
    tracker = TopologyTracker(cfg)

    # If portmap exists or is specified, load its connections
    portmap_file = args.portmap or cfg.get("controller.port_map_path", "data/portmap.json")
    if os.path.isfile(portmap_file):
        try:
            with open(portmap_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Reconstruct tracker from portmap
            for sw, neighbors in data.get("switch_ports", {}).items():
                for nbr, port in neighbors.items():
                    if nbr in data.get("switch_ports", {}):
                        # Switch-to-switch link
                        dst_port = data["switch_ports"][nbr].get(sw, 1)
                        tracker.add_link(sw, port, nbr, dst_port)
                    elif nbr in data.get("host_switch", {}):
                        ip = data.get("host_ip", {}).get(nbr, "")
                        tracker.add_host(nbr, ip=ip, switch=sw, port=port)
            for sw, dpid in data.get("switch_dpid", {}).items():
                tracker.switch_dpid[sw] = int(dpid)
                tracker.dpid_to_name_map[int(dpid)] = sw
        except Exception as exc:
            print(f"Warning: could not load portmap {portmap_file}: {exc}")

    print(tracker.describe())

    if args.paths:
        src, dst = args.paths
        paths = tracker.k_shortest_paths(src, dst, k=args.k, weight=args.weight)
        print(f"\nk-shortest paths from {src} to {dst} (k={args.k}, weight={args.weight}):")
        if not paths:
            print(f"  No path found between {src} and {dst}")
        for i, p in enumerate(paths):
            rtt = tracker.expected_rtt_ms(p)
            bottle = tracker.path_bottleneck_bw(p)
            print(f"  path {i}: {' -> '.join(p)}  predicted_rtt={rtt:.2f} ms  bottleneck={bottle:.1f} Mbps")


if __name__ == "__main__":
    main()
