"""Loop-safe OpenFlow 1.3 SDN controller for the multipath diamond.

Loop safety
-----------
The P2 topology contains a cycle (``s1 -> s2 -> s4 -> s3 -> s1``), so this
controller must never flood. Two mechanisms guarantee that:

1. **ARP proxy** (:mod:`controller.arp_proxy`) answers every ARP request from a
   learned ``IP -> MAC`` table, so no ARP request is ever forwarded.
2. **Unicast forwarding only.** Packet-in handling resolves a destination MAC to
   a port and installs an **exact-match** flow. If the destination is unknown,
   the packet is **dropped with a log line**, never flooded.

This is the deliberate choice among the two options: drop-with-log rather than
flood-along-a-spanning-tree. Rationale: with only two hosts and two disjoint
paths, a spanning tree would work, but it would silently pin all traffic to one
path and make P9's multipath rerouting impossible to build on top of it.
Dropping unknown unicast is also what a production SDN controller does, since it
cannot safely guess a destination without flooding. P9 will add controlled
multipath forwarding on top of this exact-match core.

Table-miss on every switch points at the controller, so new destinations are
always discovered by packet-in rather than assumed.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    DEAD_DISPATCHER,
    HANDSHAKE_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from ryu.lib import hub
from ryu.lib.packet import arp, ether_types, ethernet, packet
from ryu.ofproto import ofproto_v1_3

from common.config import Config, load_config
from common.logging_utils import get_event_log, setup_logging
from controller.arp_proxy import ArpProxy, ArpTable, is_valid_mac
from controller.flow_manager import FlowProgrammer, FlowSpec, table_miss_spec
from controller.telemetry import (
    PROBE_ETHERTYPE,
    TelemetryCollector,
    build_probe_packet,
    parse_probe_packet,
)
from controller.topology import TopologyTracker, build_lldp_packet, parse_lldp_packet

LOGGER = logging.getLogger("controller.sdn")

#: Path selection for P3: shortest path by hop count.
#: P9 replaces this with the ML-aware cost function; P4 replaces the static
#: graph with live discovery. Isolated here so both swaps are one-line changes.
PATH_SELECTION = "shortest_hops"


# --------------------------------------------------------------------------- #
# ARP field compatibility
# --------------------------------------------------------------------------- #
# ryu 4.34 (os-ken 2.x) renamed the scapy-style ARP attributes:
#   op -> opcode, sha -> src_mac, spa -> src_ip, tha -> dst_mac, tpa -> dst_ip
# These helpers read whichever spelling is present so the controller works
# against both the old and new ryu packet libraries.
def arp_opcode(arp_pkt: Any) -> Optional[int]:
    """Return the ARP opcode (1 = request, 2 = reply)."""
    value = getattr(arp_pkt, "opcode", None)
    return value if value is not None else getattr(arp_pkt, "op", None)


def arp_src_mac(arp_pkt: Any) -> Optional[str]:
    """Return the sender hardware address."""
    value = getattr(arp_pkt, "src_mac", None)
    return value if value is not None else getattr(arp_pkt, "sha", None)


def arp_src_ip(arp_pkt: Any) -> Optional[str]:
    """Return the sender protocol (IP) address."""
    value = getattr(arp_pkt, "src_ip", None)
    return value if value is not None else getattr(arp_pkt, "spa", None)


def arp_dst_mac(arp_pkt: Any) -> Optional[str]:
    """Return the target hardware address."""
    value = getattr(arp_pkt, "dst_mac", None)
    return value if value is not None else getattr(arp_pkt, "tha", None)


def arp_dst_ip(arp_pkt: Any) -> Optional[str]:
    """Return the target protocol (IP) address."""
    value = getattr(arp_pkt, "dst_ip", None)
    return value if value is not None else getattr(arp_pkt, "tpa", None)


def _mac_to_bytes(mac: str) -> bytes:
    """Convert ``aa:bb:cc:dd:ee:ff`` to six bytes."""
    return bytes(int(part, 16) for part in mac.split(":"))


def _ip_to_bytes(address: str) -> bytes:
    """Convert a dotted-quad IPv4 address to four bytes."""
    return bytes(int(part) for part in address.split("."))


class SdnController(app_manager.RyuApp):
    """Learning-switch + ARP-proxy controller, loop-safe by construction.

    Parameters
    ----------
    config:
        Parsed ``config.yaml``. Defaults to the discovered file so the app can
        be launched by a bare ``ryu-manager`` invocation.
    """

    #: Advertise OF1.3 only, matching the switches built in P2.
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, config: Optional[Config] = None, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.config = config if config is not None else load_config()
        # Keyed by switch NAME. OpenFlow identifies datapaths by numeric dpid, so
        # `dpid_to_name` translates; keeping the primary map name-keyed means the
        # port map, flow specs and logs all agree on one identifier.
        self.datapaths: Dict[str, Any] = {}
        self.dpid_to_name: Dict[Any, str] = {}
        # Global MAC -> (switch, port). NOT per-switch: h1 is attached to s1 and
        # h4 to s4, so a per-switch table would never let s4 forward towards
        # h1's MAC and every cross-switch packet would be dropped as unknown
        # unicast. Shortest-path forwarding needs a network-wide view.
        self.mac_to_location: Dict[str, Tuple[str, int]] = {}
        self.topology = TopologyTracker(self.config)
        self.port_map = self.topology
        self.arp = ArpProxy(
            ArpTable(), own_ip=self.config.get("controller.own_ip", "0.0.0.0")
        )
        # Identity used when the controller itself originates ARP traffic.
        self.proxy_mac = str(self.config.get("controller.proxy_mac",
                                             "0e:00:00:00:00:01"))
        self.proxy_ip = str(self.config.get("controller.proxy_ip", "10.0.0.254"))
        self._hosts_resolved = False
        self._load_port_map()
        self.flows: Optional[FlowProgrammer] = None
        # NOTE: name must not collide with Ryu's own hub machinery -- AppBase
        # exposes `self.events` as the queue the event loop consumes, so
        # shadowing it with an EventLog breaks hub._launch with
        # "AttributeError: 'EventLog' object has no attribute 'get'".
        self.event_log = get_event_log(self.config)
        self.idle_timeout = int(self.config.get("controller.flow_idle_timeout", 30))
        self.hard_timeout = int(self.config.get("controller.flow_hard_timeout", 0))
        self.priority_default = int(self.config.get("controller.priority_default", 100))
        self.priority_selected = int(self.config.get("controller.priority_selected", 1000))
        self.packet_in_protocols = set(
            self.config.get("controller.packet_in_protocols", ["arp", "ipv4"])
        )
        # Counters for the acceptance test / API.
        self.counters: Dict[str, int] = {
            "packet_in": 0,
            "arp_requests": 0,
            "arp_replies_sent": 0,
            "unknown_unicast_dropped": 0,
            "flows_installed": 0,
            "links_discovered": 0,
        }
        # Per-switch counters for LLDP tracking
        self.switch_lldp_sent: Dict[str, int] = defaultdict(int)
        self.switch_lldp_recv: Dict[str, int] = defaultdict(int)
        self.switch_links_added: Dict[str, int] = defaultdict(int)

        self.discovery_enabled = bool(self.config.get("discovery.enabled", True))
        self.discovery_thread = hub.spawn(self._discovery_loop) if self.discovery_enabled else None

        self.telemetry = TelemetryCollector(self.config)
        for link in self.topology.links.values():
            self.telemetry.register_link(
                link.src_switch, link.src_port, link.dst_switch, link.dst_port, link.bw_mbps, link.delay_ms
            )
            self.telemetry.register_link(
                link.dst_switch, link.dst_port, link.src_switch, link.src_port, link.bw_mbps, link.delay_ms
            )
        # P9 decision loop. The object is created here so the API and the
        # acceptance tests can report on it, but it is *started* later, once
        # switches have connected and the flow programmer exists -- starting it
        # now would find no datapaths and no port map.
        self.reroute: Optional["RerouteIntegration"] = None
        # (flow, switch path) installed before the reroute loop starts, replayed
        # into it once it does -- otherwise rules installed during fabric bring-up
        # are invisible to the decision loop forever.
        self._pending_flows: List[Tuple[str, Tuple[str, ...]]] = []

        self.telemetry_enabled = bool(self.config.get("telemetry.enabled", True))
        self.telemetry_thread = hub.spawn(self._telemetry_loop) if self.telemetry_enabled else None
        self.probe_thread = (
            hub.spawn(self._probe_loop)
            if self.telemetry_enabled and bool(self.config.get("telemetry.probe.enabled", True))
            else None
        )

    def _maybe_start_reroute_loop(self) -> None:
        """Start the P9 decision loop once, when the fabric is ready.

        Requires a flow programmer (so switches have connected), a populated
        topology (so ports can be resolved) and at least one registered host
        (so a path's end rules can be built). Each of those arrives
        asynchronously, hence the repeated call -- it is a no-op until all are
        satisfied, and it starts exactly once.
        """
        if self.reroute is not None:
            return
        if self.flows is None or not self.topology.links:
            return
        if not self.topology.host_ports:
            return

        from controller.reroute_integration import RerouteIntegration

        self.reroute = RerouteIntegration(self)
        self.reroute.start()
        # Replay anything the fabric installed before the loop existed.
        for flow, path in getattr(self, "_pending_flows", []):
            self.reroute.register(flow, path)
        self._pending_flows = []
        LOGGER.info("reroute loop state: %s", self.reroute.report())

    def port_of(self, src: str, dst: str) -> Optional[int]:
        """The port on ``src`` facing ``dst``, or ``None`` if not adjacent.

        Used by the P9 reroute loop to turn an abstract "install_ingress on s2"
        operation into a concrete output port. Returning ``None`` for a
        non-adjacent pair is deliberate: the caller refuses the operation and
        rolls the plan back, which is better than installing a rule that sends
        traffic nowhere.
        """
        for link in self.topology.links.values():
            if link.src_switch == src and link.dst_switch == dst:
                return link.src_port
            if link.src_switch == dst and link.dst_switch == src:
                return link.dst_port
        return None

    def host_port_of(self, switch: str) -> Optional[int]:
        """The port on ``switch`` facing its attached host, or ``None``.

        A path's first switch receives the packet from a host and its last
        switch forwards one, so the P9 applier needs this to build the end rules.
        When a switch has several hosts attached the lowest port is returned; the
        diamond gives each switch at most one.
        """
        ports = [port for (sw, port) in self.topology.host_ports if sw == switch]
        return min(ports) if ports else None

    def _switch_name(self, datapath: Any) -> str:
        """Return the Mininet switch name for a datapath.

        Falls back to the raw dpid or s<dpid> when unmapped.
        """
        resolved = None
        if hasattr(self, "topology") and self.topology is not None:
            resolved = self.topology.switch_of_dpid(datapath.id)
        if resolved is None and hasattr(self, "port_map") and self.port_map is not None:
            resolved = self.port_map.switch_of_dpid(datapath.id)
        if resolved:
            return resolved
        if hasattr(self, "dpid_to_name") and datapath.id in self.dpid_to_name:
            return self.dpid_to_name[datapath.id]
        if isinstance(datapath.id, int):
            return f"s{datapath.id}"
        return str(datapath.id)

    # ------------------------------------------------------ ARP resolution --
    def _all_switches_ready(self) -> bool:
        """True once every switch in the port map has connected.

        Switches arrive keyed by numeric datapath id, so names are mapped through
        the port map rather than compared directly.
        """
        if self.port_map is None:
            return bool(self.datapaths)
        wanted = self.port_map.known_switches()
        return bool(wanted) and wanted <= set(self.datapaths)

    def resolve_all_hosts(self) -> int:
        """Proactively resolve every known host's MAC by ARP solicitation.

        Why this is necessary
        ---------------------
        ``h1`` asking for ``h4`` cannot be answered until ``h4``'s MAC is known,
        but ``h4`` only reveals its MAC by speaking. With flooding forbidden the
        request cannot be forwarded to discover it, so waiting passively
        deadlocks: ``h1`` retries forever and ``h4`` sits idle.

        Rather than relying on that race (and on the host's ARP retry timer, which
        is ~1 s and would lose the first ping), the controller actively asks
        each known host for its MAC once, as soon as all switches are up, using a
        **unicast packet-out** on that host's own port. No frame is flooded, so
        this stays loop-safe.
        """
        if self.port_map is None:
            LOGGER.warning("resolve_all_hosts: no port map")
            return 0
        sent = 0
        for host_name in sorted(self.port_map.host_switch):
            switch = self.port_map.host_switch[host_name]
            ip = self.port_map.host_ip.get(host_name)
            if switch not in self.datapaths or ip is None:
                continue
            port = self.port_map.port(switch, host_name)
            if port is None:
                LOGGER.warning("no port for %s on %s", host_name, switch)
                continue
            self._send_arp_request(self.datapaths[switch], ip, port)
            sent += 1
        if sent:
            LOGGER.info("proactively solicited %d host MAC(s) via unicast packet-out",
                        sent)
        return sent

    def _send_arp_request(
        self, datapath: Any, target_ip: str, out_port: int
    ) -> None:
        """Emit an ARP request for ``target_ip`` out of ``out_port``.

        Unicast at the OVS level but with a broadcast Ethernet/ARP destination,
        which is what makes an ARP request: it reaches exactly one host, which
        replies unicast to our MAC.
        """
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        proxy_mac = self.proxy_mac
        proxy_ip = self.proxy_ip

        eth_dst = bytes.fromhex("ffffffffffff")
        eth_src = _mac_to_bytes(proxy_mac)
        ethertype = ether_types.ETH_TYPE_ARP.to_bytes(2, "big")

        arp_payload = (
            (1).to_bytes(2, "big")                       # htype = Ethernet
            + ether_types.ETH_TYPE_IP.to_bytes(2, "big")  # ptype = IPv4
            + bytes([6, 4])                              # hlen, plen
            + arp.ARP_REQUEST.to_bytes(2, "big")
            + eth_src
            + _ip_to_bytes(proxy_ip)
            + bytes.fromhex("000000000000")              # target hw addr
            + _ip_to_bytes(target_ip)
        )
        frame = eth_dst + eth_src + ethertype + arp_payload

        # NOTE: ryu 4.34's OFPPacketOut has no `out_port` parameter -- the
        # output port is carried by the OFPActionOutput in `actions`.
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=ofp.OFPP_CONTROLLER,
            actions=[parser.OFPActionOutput(out_port)],
            data=frame,
        )
        try:
            datapath.send_msg(out)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("could not send ARP request for %s: %s", target_ip, exc)

    # ----------------------------------------------------------- port map --
    def _load_port_map(self) -> None:
        """Load switch/port adjacency, or initialize dynamic topology tracker.

        In P4, TopologyTracker maintains live topology dynamically, merging with
        portmap.json if present on disk.
        """
        path = self.config.get("controller.port_map_path", "data/portmap.json")
        loaded = self.topology.load_from_portmap_file(str(path))
        if loaded:
            LOGGER.info(
                "loaded initial port map from %s (%d switches)",
                path, len(self.topology.known_switches()),
            )
        else:
            LOGGER.info(
                "port map %s absent; dynamic LLDP topology discovery active.",
                path,
            )

    # ---------------------------------------------------------------- logs --
    def _setup_logging(self) -> None:
        """Install console/file logging once the app is instantiated."""
        try:
            setup_logging(self.config, name="controller.sdn", force=True)
        except Exception as exc:  # pragma: no cover - logging must never be fatal
            print(f"WARNING: could not configure logging: {exc}", file=sys.stderr)

    # --------------------------------------------------------- connections --
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev: Any) -> None:
        """Handle switch features reply and log datapath info."""
        datapath = ev.msg.datapath
        dpid = ev.msg.datapath_id
        switch = self._switch_name(datapath)
        LOGGER.info(
            "[SWITCH_FEATURES] datapath 0x%x (switch %s) connected; capabilities=0x%x",
            dpid, switch, ev.msg.capabilities,
        )

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def port_desc_stats_reply_handler(self, ev: Any) -> None:
        """Handle port description stats reply to discover and log physical switch ports."""
        datapath = ev.msg.datapath
        switch = self._switch_name(datapath)
        port_list = []
        for port in ev.msg.body:
            port_no = port.port_no
            if port_no <= datapath.ofproto.OFPP_MAX:
                name_str = (
                    port.name.decode("utf-8", "ignore")
                    if isinstance(port.name, bytes)
                    else str(port.name)
                )
                port_list.append((port_no, name_str))
        ports_str = ", ".join(f"{p}:{n}" for p, n in sorted(port_list))
        LOGGER.info(
            "[PORT_DESC] switch %s (dpid=%s) ports: [%s]",
            switch, datapath.id, ports_str,
        )

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, CONFIG_DISPATCHER])
    def state_change_handler(self, ev: Any) -> None:
        """Track datapath connections and reconnects.

        On a *new* connection the switch is programmed with the table-miss rule;
        on a *disconnect* the cached bindings for that switch are dropped. Both
        make the controller restart-safe (a P3 acceptance criterion).

        NOTE: ``ev.state`` is one of Ryu's dispatcher constants, whose values are
        the lowercase strings ``'main'`` / ``'config'`` / ``'dead'``. Comparing
        against the *names* (``"MAIN_DISPATCHER"``) never matches, which makes
        the handler silently do nothing -- every switch then has no table-miss
        rule and no traffic is ever forwarded.
        """
        self._setup_logging()
        datapath = ev.datapath
        switch = self._switch_name(datapath)

        if ev.state == MAIN_DISPATCHER:
            if switch not in self.datapaths:
                LOGGER.info("switch %s connected (%s)", switch, datapath.ofproto)
                self.event_log.emit("switch_connected", switch=switch)
            self.datapaths[switch] = datapath
            self.dpid_to_name[datapath.id] = switch
            self.topology.add_switch(switch, dpid=datapath.id)
            self.flows = FlowProgrammer(
                self.datapaths,
                idle_timeout=self.idle_timeout,
                hard_timeout=self.hard_timeout,
            )
            # Only the table-miss rule is installed up front; forwarding is
            # learned from packet-ins.
            self.flows.install(table_miss_spec(switch))
            # Probe packets (ethertype 0x88b5) must always go straight to the controller
            # with zero buffering, bypassing normal learning/forwarding flows.
            self.flows.install(
                FlowSpec(
                    switch=switch,
                    priority=65000,
                    match={"eth_type": PROBE_ETHERTYPE},
                    out_port=None,
                    idle_timeout=0,
                    hard_timeout=0,
                )
            )
            warnings = []
            if self.port_map is not None:
                from controller.portmap import datapath_port_sets

                warnings = self.port_map.validate_against(
                    datapath_port_sets(self.datapaths)
                )
                for warning in warnings:
                    LOGGER.warning("port map: %s", warning)
            LOGGER.info("switch %s ready (table-miss -> controller)", switch)

            # Proactively emit LLDP probe for this switch
            self._send_lldp_probes_for_switch(datapath)

            # Once every switch is up, proactively learn every host's MAC so the
            # very first ARP request can be answered immediately (see
            # resolve_all_hosts for why passive learning deadlocks here).
            self._maybe_start_reroute_loop()
            if not self._hosts_resolved and self._all_switches_ready():
                if self.resolve_all_hosts():
                    self._hosts_resolved = True

        elif ev.state in (DEAD_DISPATCHER, HANDSHAKE_DISPATCHER):
            # datapath.id is None during handshake, so only purge when known.
            if switch is None:
                return
            LOGGER.info("switch %s disconnected (%s)", switch, ev.state)
            self.event_log.emit("switch_disconnected", switch=switch, state=ev.state)
            self.datapaths.pop(switch, None)
            self.topology.remove_switch(switch)
            # Drop bindings this switch was the only source for; they are
            # re-learned from the next packet-in.
            for mac in [m for m, (sw, _) in self.mac_to_location.items()
                        if sw == switch]:
                self.mac_to_location.pop(mac, None)

    # ------------------------------------------------------------- packets --
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev: Any) -> None:
        """Handle one packet-in.

        Dispatch is by ethertype. ARP is answered by the proxy; IPv4 triggers
        learning and exact-match forwarding; LLDP triggers topology discovery;
        anything else is dropped.
        """
        msg = ev.msg
        datapath = msg.datapath
        ofp = datapath.ofproto
        switch = self._switch_name(datapath)
        self.counters["packet_in"] += 1

        try:
            pkt = packet.Packet(msg.data)
        except Exception as exc:
            LOGGER.warning("%s: unparseable packet-in: %s", switch, exc)
            self._drop_packet(msg)
            return

        # NOTE: ryu.lib.packet.packet.Packet in 4.34 exposes
        # get_protocol(), not the Scapy-style get().
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            LOGGER.debug("%s: no ethernet header, dropping", switch)
            self._drop_packet(msg)
            return

        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            self._handle_arp(datapath, msg, pkt, eth)
        elif eth.ethertype == ether_types.ETH_TYPE_IP:
            self._handle_ipv4(datapath, msg, pkt, eth)
        elif eth.ethertype == ether_types.ETH_TYPE_LLDP:
            self._handle_lldp(datapath, msg, pkt, eth)
        elif eth.ethertype == PROBE_ETHERTYPE:
            self._handle_probe(datapath, msg, pkt, eth)
        else:
            LOGGER.info(
                "%s: ethertype 0x%04x not in %s -> dropped",
                switch, eth.ethertype, sorted(self.packet_in_protocols),
            )
            self._drop_packet(msg)

    # ---------------------------------------------------------------- LLDP --
    def _handle_lldp(
        self, datapath: Any, msg: Any, pkt: Any, eth: Any
    ) -> None:
        """Handle incoming LLDP discovery frame to learn switch links."""
        res = parse_lldp_packet(msg.data)
        if res is None:
            self._drop_packet(msg)
            return

        src_dpid, src_port = res
        dst_dpid = datapath.id
        dst_port = msg.match["in_port"]

        src_switch = self.topology.switch_name_of(src_dpid)
        dst_switch = self.topology.switch_name_of(dst_dpid)

        # Do not loopback
        if src_switch == dst_switch:
            self._drop_packet(msg)
            return

        self.switch_lldp_recv[dst_switch] += 1
        LOGGER.info(
            "[LLDP_RECV] %s: port %d <- from remote switch %s port %d (switch_total_recv=%d)",
            dst_switch, dst_port, src_switch, src_port, self.switch_lldp_recv[dst_switch],
        )

        link = self.topology.add_link(src_switch, src_port, dst_switch, dst_port)
        link.last_seen = time.monotonic()
        # Discovery may complete the last precondition for the P9 loop
        self._maybe_start_reroute_loop()
        self.switch_links_added[dst_switch] += 1
        LOGGER.info(
            "[LINK_ADDED] %s:%d <-> %s:%d (switch_total=%d, total_links=%d)",
            dst_switch, dst_port, src_switch, src_port, self.switch_links_added[dst_switch], len(self.topology.links),
        )
        self.telemetry.register_link(
            src_switch, src_port, dst_switch, dst_port, link.bw_mbps, link.delay_ms
        )
        self.telemetry.register_link(
            dst_switch, dst_port, src_switch, src_port, link.bw_mbps, link.delay_ms
        )
        self.counters["links_discovered"] = len(self.topology.links)
        self.event_log.emit(
            "link_discovered",
            src=src_switch,
            src_port=src_port,
            dst=dst_switch,
            dst_port=dst_port,
            bw_mbps=link.bw_mbps,
            delay_ms=link.delay_ms,
        )
        self._sync_topology_file()
        self._drop_packet(msg)

    def _handle_probe(
        self, datapath: Any, msg: Any, pkt: Any, eth: Any
    ) -> None:
        """Handle incoming active latency probe frame."""
        res = parse_probe_packet(msg.data)
        if res is None:
            self._drop_packet(msg)
            return

        src_dpid, src_port, dst_dpid, dst_port, seq, send_time_ns = res
        src_switch = self.topology.switch_name_of(src_dpid)
        dst_switch = self.topology.switch_name_of(dst_dpid)

        if src_switch and dst_switch:
            recv_time_ns = time.monotonic_ns()
            latency_ms = self.telemetry.record_probe_received(
                src_switch=src_switch,
                dst_switch=dst_switch,
                seq=seq,
                recv_time_ns=recv_time_ns,
            )
            if latency_ms is not None:
                LOGGER.debug("probe %s -> %s latency: %.2f ms", src_switch, dst_switch, latency_ms)

        self._drop_packet(msg)

    @set_ev_cls(ofp_event.EventOFPEchoReply, [MAIN_DISPATCHER, CONFIG_DISPATCHER])
    def echo_reply_handler(self, ev: Any) -> None:
        """Handle periodic OpenFlow EchoReply to measure control-channel latency per switch."""
        msg = ev.msg
        dp = msg.datapath
        now_ns = time.monotonic_ns()
        rtt_ms = self.telemetry.record_echo_reply(dp.id, msg.xid, recv_time_ns=now_ns)
        if rtt_ms is not None:
            LOGGER.debug("Switch %s (dpid=%d) Echo RTT: %.2f ms", self._switch_name(dp), dp.id, rtt_ms)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev: Any) -> None:
        """Handle periodic port statistics reply from OpenFlow switch."""
        msg = ev.msg
        datapath = msg.datapath
        switch = self._switch_name(datapath)
        now_mono = time.monotonic()
        now_wall = time.time()

        for stat in msg.body:
            port_no = stat.port_no
            if port_no > ofproto_v1_3.OFPP_MAX or port_no == ofproto_v1_3.OFPP_LOCAL:
                continue
            self.telemetry.record_port_stats_reply(
                switch=switch,
                port_no=port_no,
                tx_bytes=stat.tx_bytes,
                rx_bytes=stat.rx_bytes,
                tx_packets=stat.tx_packets,
                rx_packets=stat.rx_packets,
                tx_dropped=stat.tx_dropped,
                rx_dropped=stat.rx_dropped,
                tx_errors=stat.tx_errors,
                rx_errors=stat.rx_errors,
                timestamp_monotonic=now_mono,
                timestamp_wall=now_wall,
            )

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev: Any) -> None:
        """Handle dynamic port status changes (link down/up) from switches."""
        msg = ev.msg
        reason = msg.reason
        desc = msg.desc
        port_no = desc.port_no
        datapath = msg.datapath
        ofp = datapath.ofproto
        switch = self._switch_name(datapath)

        is_down = False
        if reason == ofp.OFPPR_DELETE:
            is_down = True
        elif reason == ofp.OFPPR_MODIFY:
            if (desc.state & ofp.OFPPS_LINK_DOWN) or (desc.config & ofp.OFPPC_PORT_DOWN):
                is_down = True

        if is_down:
            removed = self.topology.remove_link_on_port(switch, port_no)
            if removed:
                LOGGER.info(
                    "%s: port %d DOWN -> removed link(s) to %s",
                    switch, port_no, removed,
                )
                self.event_log.emit(
                    "link_down",
                    switch=switch,
                    port=port_no,
                    neighbors=removed,
                )
                self._sync_topology_file()
        else:
            if port_no <= ofproto_v1_3.OFPP_MAX and port_no != ofproto_v1_3.OFPP_LOCAL:
                LOGGER.info("%s: port %d UP -> probing", switch, port_no)
                self._send_lldp_probe_on_port(datapath, port_no)

    def _send_lldp_probe_on_port(self, datapath: Any, port_no: int) -> None:
        """Send an LLDP probe on port_no unless it is a host-facing or internal port."""
        ofp = datapath.ofproto
        if port_no > ofproto_v1_3.OFPP_MAX or port_no == ofproto_v1_3.OFPP_LOCAL:
            return
        switch = self._switch_name(datapath)

        # Rule 2: Never emit LLDP out host-facing ports
        if self.topology.is_host_port(switch, port_no):
            return
        if any(sw == switch and p == port_no for (sw, p) in self.mac_to_location.values()):
            return

        port = getattr(datapath, "ports", {}).get(port_no)
        src_mac = str(port.hw_addr) if port and hasattr(port, "hw_addr") else self.proxy_mac
        try:
            lldp_data = build_lldp_packet(
                dpid=datapath.id,
                port_no=port_no,
                src_mac=src_mac,
            )
            parser = datapath.ofproto_parser
            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=ofp.OFP_NO_BUFFER,
                in_port=ofp.OFPP_CONTROLLER,
                actions=[parser.OFPActionOutput(port_no)],
                data=lldp_data,
            )
            datapath.send_msg(out)
            self.switch_lldp_sent[switch] += 1
            LOGGER.info(
                "[LLDP_SENT] %s: port %d -> dpid=%s (switch_total_sent=%d)",
                switch, port_no, datapath.id, self.switch_lldp_sent[switch],
            )
        except Exception as exc:
            LOGGER.debug("error sending LLDP probe on %s:%d: %s", switch, port_no, exc)

    def _send_lldp_probes_for_switch(self, datapath: Any) -> None:
        """Send LLDP packets out of all active physical ports on a switch."""
        for port_no in list(getattr(datapath, "ports", {}).keys()):
            self._send_lldp_probe_on_port(datapath, port_no)

    def _send_lldp_probes(self) -> None:
        """Send LLDP probes across all connected switches."""
        for datapath in list(self.datapaths.values()):
            self._send_lldp_probes_for_switch(datapath)

    def _discovery_loop(self) -> None:
        """Periodic background thread emitting LLDP discovery packets and checking timeouts."""
        interval = float(
            self.config.get(
                "discovery.lldp_interval_s",
                self.config.get("discovery.poll_interval_s", 2.0),
            )
        )
        link_timeout = float(self.config.get("discovery.link_timeout_s", 6.0))
        while True:
            try:
                self._send_lldp_probes()
                timed_out = self.topology.check_link_timeouts(link_timeout)
                if timed_out:
                    LOGGER.info("LLDP timeout -> removed link(s): %s", timed_out)
                    for canon in timed_out:
                        self.event_log.emit("link_timeout", link=canon)
                    self._sync_topology_file()
            except Exception as exc:
                LOGGER.debug("discovery loop error: %s", exc)
            hub.sleep(interval)

    def _sync_topology_file(self) -> None:
        """Sync in-memory topology state to data/topology.json and data/portmap.json."""
        try:
            # Both output paths come from config. Previously only the port map
            # was configurable while the topology file was hardcoded to
            # data/topology.json, which made this method untestable without
            # writing into the repository and broke the no-magic-paths rule.
            top_path = str(
                self.config.get("controller.topology_file_path", "data/topology.json")
            )
            os.makedirs(os.path.dirname(os.path.abspath(top_path)), exist_ok=True)
            tmp_path = f"{top_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.topology.export_topology_dict(), f, indent=2)
            os.replace(tmp_path, top_path)

            portmap_path = str(self.config.get("controller.port_map_path", "data/portmap.json"))
            self.topology.save(portmap_path)
            for link in self.topology.links.values():
                self.telemetry.register_link(
                    link.src_switch, link.src_port, link.dst_switch, link.dst_port, link.bw_mbps, link.delay_ms
                )
                self.telemetry.register_link(
                    link.dst_switch, link.dst_port, link.src_switch, link.src_port, link.bw_mbps, link.delay_ms
                )
        except Exception as exc:
            LOGGER.error("could not sync topology file: %s", exc, exc_info=True)

    def _request_all_port_stats(self) -> None:
        """Send OFPPortStatsRequest to all currently connected switches."""
        for switch, datapath in list(self.datapaths.items()):
            try:
                ofp = datapath.ofproto
                parser = datapath.ofproto_parser
                req = parser.OFPPortStatsRequest(datapath, 0, ofp.OFPP_ANY)
                datapath.send_msg(req)
            except Exception as exc:
                LOGGER.debug("failed to request port stats from %s: %s", switch, exc)

    def _telemetry_loop(self) -> None:
        """Periodic background thread: poll port stats and write CSV snapshots."""
        interval = float(self.config.get("telemetry.sample_interval_s", 2.0))
        hub.sleep(1.0)
        while True:
            try:
                self._request_all_port_stats()
                hub.sleep(0.15)
                self.telemetry.write_csv_snapshot()
                if self.telemetry.is_telemetry_stalled():
                    elapsed = round(time.monotonic() - self.telemetry.last_sample_monotonic, 1)
                    LOGGER.warning("Telemetry STALLED: no updates within stall threshold (elapsed=%.1fs)", elapsed)
                    self.event_log.emit("telemetry_stall", elapsed_s=elapsed)
            except Exception as exc:
                LOGGER.debug("telemetry loop error: %s", exc)
            hub.sleep(max(0.1, interval - 0.15))

    def _send_telemetry_probes(self) -> None:
        """Emit active latency probes across all registered inter-switch links."""
        for (src_sw, src_p), (dst_sw, dst_p, _, _) in list(self.telemetry.link_map.items()):
            dp = self.datapaths.get(src_sw)
            dst_dp = self.datapaths.get(dst_sw)
            if dp is None or dst_dp is None:
                continue
            try:
                self.telemetry.probe_seq += 1
                seq = self.telemetry.probe_seq
                send_time_ns = time.monotonic_ns()
                self.telemetry.record_probe_sent(src_sw, dst_sw, seq, send_time_ns)

                probe_data = build_probe_packet(
                    src_dpid=dp.id,
                    src_port=src_p,
                    dst_dpid=dst_dp.id,
                    dst_port=dst_p,
                    seq=seq,
                    src_mac=self.proxy_mac,
                    send_time_ns=send_time_ns,
                )
                ofp = dp.ofproto
                parser = dp.ofproto_parser
                out = parser.OFPPacketOut(
                    datapath=dp,
                    buffer_id=ofp.OFP_NO_BUFFER,
                    in_port=ofp.OFPP_CONTROLLER,
                    actions=[parser.OFPActionOutput(src_p)],
                    data=probe_data,
                )
                dp.send_msg(out)
            except Exception as exc:
                LOGGER.debug("error sending probe %s:%d -> %s:%d: %s", src_sw, src_p, dst_sw, dst_p, exc)

    def _send_echo_requests(self) -> None:
        """Send OFPEchoRequest to each connected switch to measure control-channel RTT."""
        now_ns = time.monotonic_ns()
        for dp in list(self.datapaths.values()):
            try:
                parser = dp.ofproto_parser
                req = parser.OFPEchoRequest(dp, data=b"telemetry_echo")
                self.telemetry.record_echo_request(dp.id, req.xid, send_time_ns=now_ns)
                dp.send_msg(req)
            except Exception as exc:
                LOGGER.debug("error sending echo request to %s: %s", self._switch_name(dp), exc)

    def _probe_loop(self) -> None:
        """Periodic background thread emitting active latency probes and echo requests."""
        interval = float(self.config.get("telemetry.probe.probe_interval_s", 1.0))
        hub.sleep(1.5)
        while True:
            try:
                self._send_echo_requests()
                self._send_telemetry_probes()
            except Exception as exc:
                LOGGER.debug("probe loop error: %s", exc)
            hub.sleep(interval)

    # ----------------------------------------------------------------- ARP --
    def _learn_host(
        self,
        ip: str,
        mac: str,
        switch: str,
        port: int,
        source_type: str = "packet",
    ) -> None:
        """Dynamically learn host attachment (ip, mac, switch, port) and update graph."""
        if not ip or not mac or not switch or port is None:
            return
        known = self.arp.table.lookup_ip(ip)
        self.mac_to_location[mac] = (switch, port)
        self.arp.table.learn(ip, mac, switch, port)
        h_name = self.topology.record_host(None, ip=ip, switch=switch, port=port, mac=mac)
        self._sync_topology_file()
        # Host learning may complete the preconditions for the P9 loop
        # (flow programmer + non-empty host_ports + discovered links); retry it.
        self._maybe_start_reroute_loop()
        if known is None or known.mac != mac:
            LOGGER.info(
                "%s: learned host %s (%s -> %s) on port %d (from %s)",
                switch, h_name, ip, mac, port, source_type,
            )
            self.event_log.emit(
                "host_resolved", ip=ip, mac=mac,
                switch=switch, port=port, host=h_name,
            )

    def _handle_arp(
        self, datapath: Any, msg: Any, pkt: Any, eth: Any
    ) -> None:
        """Answer an ARP request from the learned table; never forward one."""
        ofp = datapath.ofproto
        switch = self._switch_name(datapath)
        arp_pkt = pkt.get_protocol(arp.arp)

        if arp_pkt is None:
            self._drop_packet(msg)
            return

        # NOTE: ryu.lib.packet.arp names these ARP_REQUEST / ARP_REPLY (not the
        # ARPOP_* spelling used by Scapy).
        if arp_opcode(arp_pkt) == arp.ARP_REPLY:
            # A host answering our proactive solicitation (or another host's
            # request). This is how a host's MAC becomes known at all, so it is
            # learned here rather than discarded.
            src_ip, src_mac = arp_src_ip(arp_pkt), arp_src_mac(arp_pkt)
            if src_ip and src_mac:
                in_port = msg.match["in_port"]
                self._learn_host(src_ip, src_mac, switch, in_port, source_type="ARP reply")
            self._drop_packet(msg)
            return

        if arp_opcode(arp_pkt) != arp.ARP_REQUEST:
            LOGGER.debug("%s: ARP opcode %s not a request; ignoring",
                         switch, arp_opcode(arp_pkt))
            self._drop_packet(msg)
            return

        sender_ip = arp_src_ip(arp_pkt)
        sender_mac = eth.src
        in_port = msg.match["in_port"]
        if sender_ip and sender_mac:
            self._learn_host(sender_ip, sender_mac, switch, in_port, source_type="ARP request")

        self.counters["arp_requests"] += 1
        decision = self.arp.handle_request(
            sender_ip=sender_ip,
            sender_mac=sender_mac,
            target_ip=arp_dst_ip(arp_pkt),
            switch=switch,
            in_port=in_port,
        )

        if decision.action != "reply" or decision.reply_target is None:
            # 'learn' (target unknown) or 'ignore': drop, do not flood.
            LOGGER.info(
                "%s: ARP %s (target %s) -> %s",
                switch, eth.src, arp_dst_ip(arp_pkt), decision.reason,
            )
            self._drop_packet(msg)
            return

        target_ip, target_mac = decision.reply_target
        LOGGER.info(
            "%s: ARP proxy answering %s: who-has %s? tell %s",
            switch, eth.src, target_ip, target_mac,
        )
        reply = self._build_arp_reply(
            msg.data, arp_pkt, target_ip, target_mac
        )
        self._send_packet(datapath, msg, reply, msg.match["in_port"])
        self.counters["arp_replies_sent"] += 1
        self.event_log.emit(
            "arp_reply",
            switch=switch,
            requester=eth.src,
            requester_ip=arp_src_ip(arp_pkt),
            target_ip=target_ip,
            target_mac=target_mac,
            in_port=msg.match["in_port"],
        )

    @staticmethod
    def _build_arp_reply(
        request_bytes: bytes, arp_pkt: Any, target_ip: str, target_mac: str
    ) -> bytes:
        """Build an ARP reply for ``request_bytes`` claiming ``target_mac``.

        Pure byte manipulation of the original request: the switch the packet
        arrived on learns ``target_mac``, which is what makes subsequent
        unicast forwarding to that host work without any flooding.
        """
        reply = bytearray(request_bytes)
        # Ethernet header of a reply: dst = the requester, src = the claimed
        # owner of target_ip. The switch learns target_mac from this frame,
        # which is what makes later unicast forwarding to that host possible.
        requester_mac = arp_src_mac(arp_pkt)
        reply[0:6] = bytes(int(x, 16) for x in requester_mac.split(":"))
        reply[6:12] = bytes(int(x, 16) for x in target_mac.split(":"))
        # ARP payload starts after ethertype.
        offset = 14
        # opcode -> REPLY
        reply[offset + 6:offset + 8] = int(arp.ARP_REPLY).to_bytes(2, "big")
        # sender hardware/protocol address = the claimed owner
        reply[offset + 8:offset + 14] = bytes(
            int(x, 16) for x in target_mac.split(":")
        )
        reply[offset + 14:offset + 18] = bytes(
            int(o) for o in target_ip.split(".")
        )
        # target hardware/protocol address = the original requester
        reply[offset + 18:offset + 24] = bytes(
            int(x, 16) for x in requester_mac.split(":")
        )
        reply[offset + 24:offset + 28] = bytes(
            int(o) for o in arp_src_ip(arp_pkt).split(".")
        )
        return bytes(reply)

    # ---------------------------------------------------------------- IPv4 --
    def _handle_ipv4(
        self, datapath: Any, msg: Any, pkt: Any, eth: Any
    ) -> None:
        """Learn the source, then forward along the shortest path.

        Unlike a learning switch, this resolves the destination **network-wide**
        and programs *every* switch on the path, because the destination host
        sits on a different switch and no frame traversing the intermediate
        links is ever seen by the controller (the loop forbids flooding).
        """
        switch = self._switch_name(datapath)
        in_port = msg.match["in_port"]
        from ryu.lib.packet import ipv4
        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
        if ipv4_pkt is not None:
            self._learn_host(ipv4_pkt.src, eth.src, switch, in_port, source_type="IPv4 packet")
        else:
            self.mac_to_location[eth.src] = (switch, in_port)

        dst = self.mac_to_location.get(eth.dst)
        if dst is None:
            # Unknown unicast. Flooding would loop, so drop and log (rule 1).
            self.counters["unknown_unicast_dropped"] += 1
            LOGGER.info(
                "%s: unknown unicast dst %s (in_port %d) -> DROPPED, not flooded "
                "(loop safety); known destinations: %s",
                switch, eth.dst, in_port, sorted(self.mac_to_location),
            )
            self.event_log.emit(
                "unknown_unicast_dropped",
                switch=switch, eth_dst=eth.dst, in_port=in_port,
            )
            self._drop_packet(msg)
            return

        dst_switch = dst[0]
        if dst_switch == switch:
            # Same switch: a single exact-match rule is enough.
            spec = FlowSpec(
                switch=switch,
                match={"in_port": in_port, "eth_dst": eth.dst},
                out_port=dst[1],
                priority=self.priority_default,
                idle_timeout=self.idle_timeout,
                hard_timeout=self.hard_timeout,
                reason=f"same-switch {eth.src}->{eth.dst}",
            )
            if self.flows is not None and self.flows.install(spec):
                self.counters["flows_installed"] += 1
            self._send_packet(datapath, msg, msg.data, dst[1])
            return

        hops = self._program_path(switch, dst_switch, eth.dst, first_hop_port=in_port)
        if hops is None:
            self._drop_packet(msg)
            return
        self._send_packet(datapath, msg, msg.data, hops[0])

    def _program_path(
        self,
        src_switch: str,
        dst_switch: str,
        dst_mac: str,
        first_hop_port: Optional[int] = None,
    ) -> Optional[List[int]]:
        """Install exact-match forwarding rules along the shortest path.

        Parameters
        ----------
        src_switch, dst_switch:
            Endpoints of the path.
        dst_mac:
            Destination MAC, matched on every rule so unrelated traffic is
            unaffected.
        first_hop_port:
            Ingress port on ``src_switch``. Learned from the packet-in on the
            first call; on later calls it is taken from the port map.

        Returns
        -------
        list[int] | None
            The output port on each switch along the path, or None when the
            path cannot be resolved (caller must drop the packet).
        """
        if self.flows is None:
            return None
        # Primary provider is live dynamic TopologyTracker; fallback to port_map if present
        provider = self.topology if (hasattr(self, "topology") and self.topology is not None and self.topology.links) else self.port_map
        if provider is None:
            LOGGER.warning(
                "no topology or port map available; cannot forward %s->%s across switches",
                src_switch, dst_switch,
            )
            return None
        try:
            path = provider.shortest_switch_path(src_switch, dst_switch)
        except Exception as exc:
            LOGGER.warning("no path %s -> %s: %s", src_switch, dst_switch, exc)
            return None
        import sys as _sys
        print(f"_program_path path={path}", file=_sys.stderr)

        out_ports: List[int] = []
        installed = 0
        for index, node in enumerate(path):
            # Where does the packet come from on this switch?
            if index == 0:
                in_port = first_hop_port if first_hop_port is not None else None
            else:
                in_port = provider.port(node, path[index - 1])
                if in_port is None and self.port_map is not None:
                    in_port = self.port_map.port(node, path[index - 1])
            if in_port is None:
                LOGGER.warning("path %s: no ingress port on %s", path, node)
                return None

            if index == len(path) - 1:
                out_port = self.mac_to_location.get(dst_mac, (None, None))[1]
                if out_port is None:
                    LOGGER.warning("path %s: no egress port for %s", path, dst_mac)
                    return None
            else:
                out_port = provider.port(node, path[index + 1])
                if out_port is None and self.port_map is not None:
                    out_port = self.port_map.port(node, path[index + 1])
                if out_port is None:
                    LOGGER.warning("path %s: no egress port on %s facing %s", path, node, path[index + 1])
                    return None

            spec = FlowSpec(
                switch=node,
                match={"in_port": in_port, "eth_dst": dst_mac},
                out_port=out_port,
                priority=self.priority_default,
                idle_timeout=self.idle_timeout,
                hard_timeout=self.hard_timeout,
                reason=f"shortest path {'->'.join(path)} to {dst_mac}",
            )
            if self.flows.install(spec):
                installed += 1
            out_ports.append(out_port)

        if installed:
            self.counters["flows_installed"] += installed
            LOGGER.info(
                "installed %d rule(s) for %s along shortest path %s",
                installed, dst_mac, " -> ".join(path),
            )
            flow_key = f"{src_switch}->{dst_switch}:{dst_mac}"
            flow_path = tuple(path)
            if self.reroute is not None:
                self.reroute.register(flow_key, flow_path)
            else:
                # Loop not up yet (fabric still coming up): queue it; the
                # replay in _maybe_start_reroute_loop registers it once the
                # loop runs.
                pending = getattr(self, "_pending_flows", None)
                if pending is None:
                    pending = []
                    self._pending_flows = pending
                pending.append((flow_key, flow_path))
            self.event_log.emit(
                "path_installed",
                src=src_switch, dst=dst_switch, eth_dst=dst_mac,
                path=path, rules=installed,
            )
        return out_ports

    # -------------------------------------------------------------- helpers --
    @staticmethod
    def _drop_packet(msg: Any, reason: str = "") -> None:
        """Drop a packet-in by sending no packet-out at all.

        OpenFlow 1.3 has no ``OFPP_NONE`` (that was an OF1.0/1.2 reserved
        port). In OF1.3 the way to discard a packet-in is simply to not answer:
        the switch then drops the buffered packet once it expires. Emitting an
        OFPPacketOut with an invented out_port would be wrong.
        """
        if reason:
            LOGGER.debug("dropping packet-in (%s)", reason)

    def _send_packet(
        self, datapath: Any, msg: Any, data: bytes, out_port: int
    ) -> None:
        """Emit a packet out of ``out_port`` (used for ARP replies and first packets)."""
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        action = parser.OFPActionOutput(out_port)
        # Two non-obvious requirements, both learned the hard way:
        #
        # 1. `data` is a REWRITTEN frame (ARP replies have MACs/IPs swapped), so
        #    it must be sent with buffer_id=OFP_NO_BUFFER. Passing the incoming
        #    buffer_id alongside data makes ryu assert / emit the original bytes.
        # 2. `in_port` must be OFPP_CONTROLLER, NOT the packet-in's ingress port.
        #    Setting in_port to the same port we output on makes OVS treat the
        #    packet-out as a hairpin and silently drop it, so the host never sees
        #    the reply (symptom: ARP stays INCOMPLETE, ping 100% loss).
        reply = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=ofp.OFPP_CONTROLLER,
            actions=[action],
            data=data,
        )
        try:
            datapath.send_msg(reply)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("could not send packet-out: %s", exc)

    # ------------------------------------------------------------ reporting --
    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serialisable view of controller state (P12 API)."""
        return {
            "switches": sorted(self.datapaths),
            "counters": dict(self.counters),
            "flows": self.flows.stats.as_dict() if self.flows else {},
            "arp_table": {
                ip: {"mac": e.mac, "switch": e.switch, "port": e.port}
                for ip, e in self.arp.table.entries.items()
            },
            "mac_to_location": {
                mac: {"switch": sw, "port": port}
                for mac, (sw, port) in sorted(self.mac_to_location.items())
            },
            "path_selection": PATH_SELECTION,
            "topology": {
                "nodes": list(self.topology.graph.nodes()),
                "links": [
                    {
                        "u": u,
                        "v": v,
                        "bw_mbps": d.get("bw_mbps"),
                        "delay_ms": d.get("delay_ms"),
                    }
                    for u, v, d in self.topology.graph.edges(data=True)
                ],
            },
            "telemetry": self.telemetry.export_snapshot(),
        }


#: ``ryu-manager`` discovers apps in this module; referenced explicitly so the
#: import is not seen as unused.
app = SdnController


def main() -> None:  # pragma: no cover - convenience entry point
    """Launch the controller with the project config preloaded."""
    setup_logging(load_config(), name="controller.sdn", force=True)
    from ryu.cmd.manager import main as ryu_main

    ryu_main()


if __name__ == "__main__":  # pragma: no cover
    main()