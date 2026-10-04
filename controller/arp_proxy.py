"""ARP proxy: the controller terminates ARP instead of the network flooding it.

Why this exists
---------------
Design rule 1 forbids broadcast storms, and the P2 diamond contains a loop
(``s1 -> s2 -> s4 -> s3 -> s1``). If ARP were answered by flooding, each request
would multiply around the loop. So the controller answers ARP itself from a
learned ``IP -> MAC`` table and never floods an ARP request.

The host-visible behaviour is identical to a switched network: a host sends an
ARP request, receives a reply, and caches the mapping. The difference is
invisible on the wire except that no ARP request is ever forwarded.

Pure logic, no mininet and no Ryu imports, so it is directly unit-testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

LOGGER = logging.getLogger("controller.arp_proxy")

#: Ethernet broadcast address.
BROADCAST_MAC = "ff:ff:ff:ff:ff:ff"


@dataclass(frozen=True)
class ArpEntry:
    """One learned IP -> MAC binding, tagged with where it was learned."""

    ip: str
    mac: str
    switch: str
    port: int

    def __post_init__(self) -> None:
        if not is_valid_mac(self.mac):
            raise ValueError(f"invalid MAC {self.mac!r} for IP {self.ip!r}")


def is_valid_mac(mac: str) -> bool:
    """Return True if ``mac`` looks like ``aa:bb:cc:dd:ee:ff``."""
    parts = str(mac).split(":")
    if len(parts) != 6:
        return False
    return all(len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p) for p in parts)


@dataclass
class ArpTable:
    """Learned ``IP -> MAC`` bindings, keyed by IP.

    Also tracks a reverse ``MAC -> IP`` view because forwarding decisions are
    naturally expressed per destination MAC.
    """

    entries: Dict[str, ArpEntry] = field(default_factory=dict)

    def learn(
        self, ip: str, mac: str, switch: str, port: int
    ) -> Optional[ArpEntry]:
        """Record ``ip -> (mac, switch, port)``, returning the stored entry.

        A MAC change for an existing IP is logged, because it indicates either
        a host with two NICs or a stale entry; it is still accepted so the
        network keeps working.
        """
        entry = ArpEntry(ip=ip, mac=mac, switch=switch, port=port)
        previous = self.entries.get(ip)
        if previous is not None and previous.mac != mac:
            LOGGER.info(
                "ARP: %s moved from %s to %s (on %s port %d)",
                ip, previous.mac, mac, switch, port,
            )
        self.entries[ip] = entry
        return entry

    def lookup_ip(self, ip: str) -> Optional[ArpEntry]:
        """Return the entry for ``ip``, or None if unknown."""
        return self.entries.get(ip)

    def lookup_mac(self, mac: str) -> Optional[ArpEntry]:
        """Return the entry whose MAC is ``mac``, or None if unknown."""
        for entry in self.entries.values():
            if entry.mac == mac:
                return entry
        return None

    def known_ips(self) -> Tuple[str, ...]:
        """All learned IPs, sorted, for logging."""
        return tuple(sorted(self.entries))

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class ArpDecision:
    """Outcome of handling one ARP request.

    Attributes
    ----------
    action:
        ``reply``    -- send an ARP reply built from the learned table.
        ``learn``    -- binding stored, but no reply possible yet (target unknown).
        ``ignore``   -- not an ARP request we answer (e.g. gratuitous, or a reply).
    reply_target:
        The ``(ip, mac)`` the reply should claim, present when ``action='reply'``.
    reason:
        Human-readable explanation, always populated, for the CLI log.
    """

    action: str
    reason: str
    reply_target: Optional[Tuple[str, str]] = None
    learned_ip: Optional[str] = None


class ArpProxy:
    """Answers ARP requests from a learned table; never floods.

    Parameters
    ----------
    table:
        The learned ``IP -> MAC`` table, shared with the L2 forwarding logic.
    own_ip:
        IP the proxy claims for itself when it must answer for itself. Not used
        in P3 (every host has a real answer) but kept so the proxy stays
        self-contained and P9 can reuse it.
    """

    def __init__(self, table: Optional[ArpTable] = None, own_ip: str = "0.0.0.0") -> None:
        self.table = table if table is not None else ArpTable()
        self.own_ip = own_ip

    def handle_request(
        self,
        sender_ip: str,
        sender_mac: str,
        target_ip: str,
        switch: str,
        in_port: int,
    ) -> ArpDecision:
        """Learn the sender and decide whether to reply.

        Parameters
        ----------
        sender_ip, sender_mac:
            Source of the ARP request (a real host, so always trusted).
        target_ip:
            The IP being asked about.
        switch, in_port:
            Where the request arrived, used to learn the port binding and to
            send the reply back out of the correct port.
        """
        if not is_valid_mac(sender_mac):
            return ArpDecision("ignore", f"malformed sender MAC {sender_mac!r}")

        self.table.learn(sender_ip, sender_mac, switch, in_port)

        if target_ip == sender_ip:
            # Gratuitous / ARP-announcement: nothing to answer.
            return ArpDecision(
                "ignore", "gratuitous ARP (target == sender)", learned_ip=sender_ip
            )

        if target_ip == self.own_ip:
            # Answering for the controller itself.
            return ArpDecision(
                "reply",
                f"{target_ip} is the proxy's own address",
                reply_target=(target_ip, sender_mac),
                learned_ip=sender_ip,
            )

        entry = self.table.lookup_ip(target_ip)
        if entry is None:
            # Deliberate no-flood behaviour: with a loop present, forwarding an
            # unknown ARP request would multiply it. Drop it and let the sender
            # retry once the binding exists.
            LOGGER.info(
                "ARP: no binding for %s (asked by %s on %s port %d) -> "
                "dropped, not flooded (loop safety)",
                target_ip, sender_ip, switch, in_port,
            )
            return ArpDecision(
                "learn",
                f"target {target_ip} unknown; learned sender only",
                learned_ip=sender_ip,
            )

        return ArpDecision(
            "reply",
            f"answered from learned table: {target_ip} -> {entry.mac}",
            reply_target=(target_ip, entry.mac),
            learned_ip=sender_ip,
        )