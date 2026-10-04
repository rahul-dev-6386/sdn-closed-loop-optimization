"""Pure-logic tests for the P3 controller: ARP proxy and flow programming.

No network, no root, no live Ryu datapath. The ARP proxy is exercised end to end
at the decision level, and the byte layout of the ARP reply is checked against
a hand-built frame, since a wrong offset there would break connectivity in a way
no unit test of the surrounding logic would catch.
"""
from __future__ import annotations

import struct
from typing import Optional

import pytest

from controller.arp_proxy import (
    ArpProxy,
    ArpTable,
    is_valid_mac,
)
from controller.flow_manager import (
    TABLE_MISS_PRIORITY,
    FlowProgrammer,
    FlowSpec,
    table_miss_spec,
)

H1_MAC = "0a:00:00:00:00:01"
H4_MAC = "0a:00:00:00:00:04"
H1_IP = "10.0.0.1"
H4_IP = "10.0.0.4"


# ---------------------------------------------------------------- MAC check --
@pytest.mark.parametrize(
    "mac,valid",
    [
        ("0a:00:00:00:00:01", True),
        ("FF:FF:FF:FF:FF:FF", True),
        ("0a:00:00:00:01", False),      # too few octets
        ("0a:00:00:00:00:0g", False),   # non-hex
        ("0a-00-00-00-00-01", False),   # wrong separator
        ("", False),
    ],
)
def test_mac_validation(mac: str, valid: bool) -> None:
    assert is_valid_mac(mac) is valid


# ---------------------------------------------------------------- ArpTable --
def test_learn_and_lookup() -> None:
    table = ArpTable()
    table.learn(H1_IP, H1_MAC, "s1", 1)
    entry = table.lookup_ip(H1_IP)
    assert entry is not None and entry.mac == H1_MAC
    assert table.lookup_mac(H1_MAC) is not None
    assert table.lookup_ip("10.0.0.9") is None
    assert len(table) == 1


def test_invalid_mac_is_rejected_at_learn() -> None:
    table = ArpTable()
    with pytest.raises(ValueError, match="invalid MAC"):
        table.learn(H1_IP, "not-a-mac", "s1", 1)


# ----------------------------------------------------------------- ArpProxy --
def test_first_request_learns_sender_but_does_not_reply() -> None:
    """h1 asks for h4, who is still unknown: learn h1, drop, never flood."""
    proxy = ArpProxy()
    decision = proxy.handle_request(H1_IP, H1_MAC, H4_IP, "s1", 1)
    assert decision.action == "learn"
    assert decision.reply_target is None
    assert proxy.table.lookup_ip(H1_IP).mac == H1_MAC
    assert proxy.table.lookup_ip(H4_IP) is None


def test_second_request_is_answered_from_the_table() -> None:
    """Once h4 is known, h1's request gets a real reply with no flooding."""
    proxy = ArpProxy()
    proxy.table.learn(H4_IP, H4_MAC, "s4", 1)
    decision = proxy.handle_request(H1_IP, H1_MAC, H4_IP, "s1", 1)
    assert decision.action == "reply"
    assert decision.reply_target == (H4_IP, H4_MAC)


def test_gratuitous_arp_is_ignored() -> None:
    proxy = ArpProxy()
    decision = proxy.handle_request(H1_IP, H1_MAC, H1_IP, "s1", 1)
    assert decision.action == "ignore"
    assert "gratuitous" in decision.reason


def test_proxy_answers_for_its_own_ip() -> None:
    proxy = ArpProxy(own_ip="10.0.0.254")
    decision = proxy.handle_request(H1_IP, H1_MAC, "10.0.0.254", "s1", 1)
    assert decision.action == "reply"
    assert decision.reply_target[0] == "10.0.0.254"


def test_malformed_sender_mac_is_ignored() -> None:
    proxy = ArpProxy()
    decision = proxy.handle_request(H1_IP, "bogus", H4_IP, "s1", 1)
    assert decision.action == "ignore"
    assert "malformed" in decision.reason


def test_mac_change_is_logged_but_accepted() -> None:
    table = ArpTable()
    table.learn(H1_IP, H1_MAC, "s1", 1)
    table.learn(H1_IP, "0a:00:00:00:00:99", "s1", 1)
    assert table.lookup_ip(H1_IP).mac == "0a:00:00:00:00:99"


def test_no_decision_path_ever_says_flood() -> None:
    """Loop safety invariant: the proxy has no flood action at all."""
    proxy = ArpProxy()
    for target in (H4_IP, "10.0.0.77", "0.0.0.0"):
        decision = proxy.handle_request(H1_IP, H1_MAC, target, "s1", 1)
        assert decision.action in ("reply", "learn", "ignore")
        assert "flood" not in decision.action.lower()


# ------------------------------------------------------- ARP reply bytes ----
def _ethernet(dst: str, src: str, ethertype: int = 0x0806) -> bytes:
    def mac_bytes(m: str) -> bytes:
        return bytes(int(x, 16) for x in m.split(":"))

    return mac_bytes(dst) + mac_bytes(src) + struct.pack("!H", ethertype)


def _arp_request(sender_mac: str, sender_ip: str, target_ip: str) -> bytes:
    def mac_bytes(m: str) -> bytes:
        return bytes(int(x, 16) for x in m.split(":"))

    return (
        struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)  # htype, ptype, hlen, plen, op
        + mac_bytes(sender_mac)
        + bytes(int(o) for o in sender_ip.split("."))
        + b"\x00\x00\x00\x00\x00\x00"
        + bytes(int(o) for o in target_ip.split("."))
    )


class _FakeArp:
    """Minimal stand-in for ryu.lib.packet.arp.ARP (only the fields we read)."""

    def __init__(self, sha: str, spa: str, tpa: str) -> None:
        self.sha = sha
        self.spa = spa
        self.tpa = tpa


def test_arp_reply_bytes_are_correctly_rewritten() -> None:
    """The reply must claim h4's MAC/IP and target h1 -- verified by offsets."""
    from controller.sdn_controller import SdnController

    request = _ethernet("ff:ff:ff:ff:ff:ff", H1_MAC) + _arp_request(
        H1_MAC, H1_IP, H4_IP
    )
    fake_arp = _FakeArp(sha=H1_MAC, spa=H1_IP, tpa=H4_IP)
    reply = SdnController._build_arp_reply(request, fake_arp, H4_IP, H4_MAC)

    assert len(reply) == len(request)

    eth_dst = reply[0:6].hex(":")
    eth_src = reply[6:12].hex(":")
    assert eth_dst == H1_MAC, "reply ethernet dst must be the requester"
    assert eth_src == H4_MAC, "reply ethernet src must be the claimed owner"

    ethertype = struct.unpack("!H", reply[12:14])[0]
    assert ethertype == 0x0806

    # ARP payload fields, at their defined offsets.
    assert struct.unpack("!H", reply[20:22])[0] == 2, "opcode must be REPLY"
    assert reply[22:28].hex(":") == H4_MAC, "sender HW addr must be h4"
    assert reply[28:32] == bytes([10, 0, 0, 4]), "sender proto addr must be h4's IP"
    assert reply[32:38].hex(":") == H1_MAC, "target HW addr must be the requester"
    assert reply[38:42] == bytes([10, 0, 0, 1]), "target proto addr must be h1's IP"


def test_arp_reply_reply_target_is_the_claimed_owner() -> None:
    """A switch learns the MAC we put in the Ethernet source of the reply.

    This is the property that makes subsequent unicast forwarding work without
    any ARP request ever being forwarded.
    """
    from controller.sdn_controller import SdnController

    request = _ethernet("ff:ff:ff:ff:ff:ff", H1_MAC) + _arp_request(
        H1_MAC, H1_IP, H4_IP
    )
    reply = SdnController._build_arp_reply(
        request, _FakeArp(H1_MAC, H1_IP, H4_IP), H4_IP, H4_MAC
    )
    assert reply[6:12].hex(":") == H4_MAC


# ------------------------------------------------------------- FlowSpec -----
def test_flow_spec_describe_is_readable() -> None:
    spec = FlowSpec(
        switch="s1",
        match={"in_port": 1, "eth_dst": H4_MAC},
        out_port=2,
        reason="unicast",
    )
    text = spec.describe()
    assert "s1" in text and "in_port=1" in text and "output:2" in text


def test_matchless_flow_must_be_table_miss() -> None:
    with pytest.raises(ValueError, match="table-miss"):
        FlowSpec(switch="s1", match={}, out_port=None, priority=100)


def test_table_miss_spec_is_valid() -> None:
    spec = table_miss_spec("s1")
    assert spec.priority == TABLE_MISS_PRIORITY
    assert spec.out_port is None and spec.match == {}


def test_flow_spec_carries_idle_timeout() -> None:
    """Idle timeouts are mandatory (design rule 6: stale rules must expire)."""
    spec = FlowSpec(switch="s1", match={"in_port": 1}, out_port=2, idle_timeout=45)
    assert spec.idle_timeout == 45


# --------------------------------------------------------- FlowProgrammer ----
def test_build_match_rejects_unknown_fields() -> None:
    """A typo must fail loudly rather than silently widen a match."""
    with pytest.raises(ValueError, match="invalid OpenFlow match fields"):
        FlowProgrammer.build_match({"not_a_real_field": 1})


def test_build_match_accepts_real_fields() -> None:
    match = FlowProgrammer.build_match(
        {"in_port": 1, "eth_dst": H4_MAC, "eth_type": 0x0800}
    )
    assert match is not None


def test_install_on_unknown_switch_returns_false() -> None:
    programmer = FlowProgrammer({}, idle_timeout=30)
    spec = FlowSpec(switch="s9", match={"in_port": 1}, out_port=2)
    assert programmer.install(spec) is False


def test_stats_start_at_zero_and_track_installs() -> None:
    programmer = FlowProgrammer({}, idle_timeout=30)
    assert programmer.stats.as_dict() == {"installed": 0, "deleted": 0, "replies": 0}
    programmer.install(
        FlowSpec(switch="s9", match={"in_port": 1}, out_port=2)
    )  # unknown switch -> not counted
    assert programmer.stats.installed == 0