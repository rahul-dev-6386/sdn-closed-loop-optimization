"""Flow programming primitives for the SDN controller (OpenFlow 1.3).

Wraps the Ryu ``ofproto_v1_3_parser`` calls so the rest of the controller deals
in small, testable value objects instead of message constructors.

The objects here are pure data; :class:`FlowProgrammer` performs the actual
``datapath.send_msg`` calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ryu.ofproto import ofproto_v1_3 as ofproto
from ryu.ofproto import ofproto_v1_3_parser as parser

LOGGER = logging.getLogger("controller.flow_manager")

#: Table-miss rule installed on every switch so unmatched packets reach the
#: controller. Priority 0, matching everything, action = CONTROLLER.
#: This is the only rule that is *not* exact-match, and it never floods.
TABLE_MISS_PRIORITY = 0


@dataclass(frozen=True)
class FlowSpec:
    """One exact-match flow, described independently of Ryu.

    Attributes
    ----------
    switch:
        Target switch (datapath) name.
    match:
        Match fields as a plain dict, e.g. ``{'in_port': 1, 'eth_dst': 'aa:..'}``.
    out_port:
        Single output port. ``None`` means the packet goes to the controller
        (used by the table-miss rule).
    priority:
        OpenFlow priority; higher wins.
    idle_timeout, hard_timeout:
        Seconds. Idle timeout reclaims flows for flows that go silent, so stale
        forwarding rules cannot pin traffic to a dead path (design rule 6).
    """

    switch: str
    match: Dict[str, Any]
    out_port: Optional[int]
    priority: int = 100
    idle_timeout: int = 30
    hard_timeout: int = 0
    # A human label used in log lines and as the flow cookie's low bits.
    reason: str = ""

    def __post_init__(self) -> None:
        if self.out_port is None and not self.match:
            # A catch-all that sends to the controller is legitimate only at the
            # lowest priority, and it must be the table-miss rule.
            if self.priority != TABLE_MISS_PRIORITY:
                raise ValueError(
                    "a matchless flow is only valid as the table-miss rule "
                    f"(priority {TABLE_MISS_PRIORITY}), got priority {self.priority}"
                )

    def describe(self) -> str:
        """Compact one-line description for logs."""
        match = ",".join(f"{k}={v}" for k, v in sorted(self.match.items())) or "*"
        target = "CONTROLLER" if self.out_port is None else f"output:{self.out_port}"
        return f"{self.switch} [{match}] -> {target} prio={self.priority} ({self.reason})"


@dataclass
class FlowStats:
    """Counters describing what the controller has installed."""

    installed: int = 0
    deleted: int = 0
    replies: int = 0

    def as_dict(self) -> Dict[str, int]:
        """Return the counters as a plain dict for JSON logging."""
        return {
            "installed": self.installed,
            "deleted": self.deleted,
            "replies": self.replies,
        }


class FlowProgrammer:
    """Installs and deletes :class:`FlowSpec` flows on live datapaths.

    Parameters
    ----------
    datapaths:
        Mapping of switch name -> ``ryu.controller.controller.Datapath``.
    idle_timeout, hard_timeout:
        Default timeouts applied to flows that do not override them.
    """

    def __init__(
        self,
        datapaths: Dict[str, Any],
        idle_timeout: int = 30,
        hard_timeout: int = 0,
    ) -> None:
        self.datapaths = datapaths
        self.idle_timeout = idle_timeout
        self.hard_timeout = hard_timeout
        self.stats = FlowStats()

    # ------------------------------------------------------------- helpers --
    @staticmethod
    def build_match(match: Dict[str, Any]) -> Any:
        """Build an OF1.3 ``OFPMatch`` from a plain dict.

        Raises
        ------
        ValueError
            If a field is not a legal OF1.3 match key, so a typo fails loudly
            instead of silently producing a wildcard rule.
        """
        try:
            return parser.OFPMatch(**match)
        except (TypeError, KeyError, ValueError) as exc:
            # OFPMatch signals an unknown field with KeyError ("unknown OXM
            # field: ...") rather than TypeError, so catch all three.
            raise ValueError(f"invalid OpenFlow match fields {match!r}: {exc}") from exc

    # -------------------------------------------------------------- actions --
    def install(self, spec: FlowSpec) -> bool:
        """Add one flow. Returns True on success."""
        datapath = self.datapaths.get(spec.switch)
        if datapath is None:
            LOGGER.warning("install: unknown switch %s", spec.switch)
            return False

        ofp = datapath.ofproto
        parser_for = datapath.ofproto_parser

        match = self.build_match(spec.match)
        if spec.out_port is None:
            # max_len is a 16-bit field: it must be OFPCML_MAX (65535), NOT
            # OFPP_ANY (0xFFFFFFFF), which overflows during serialization with
            # "struct.error: 'H' format requires 0 <= number <= 65535".
            actions = [
                parser_for.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_MAX)
            ]
        else:
            actions = [parser_for.OFPActionOutput(spec.out_port)]

        # OFPFlowMod requires `instructions` to be OFPInstruction objects, not
        # bare OFPAction objects -- passing actions directly trips
        # `assert isinstance(i, OFPInstruction)` inside ryu's parser.
        instructions = [
            parser_for.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)
        ]

        mod = parser_for.OFPFlowMod(
            datapath=datapath,
            cookie=self._cookie(spec),
            # OFPTT_ALL (0xff) is only meaningful for delete/modify-strict. Using
            # it for OFPFC_ADD makes the switch silently ignore the rule, so the
            # flow table stays empty and every packet is dropped.
            table_id=ofp.OFPTT_TABLE0 if hasattr(ofp, "OFPTT_TABLE0") else 0,
            command=ofp.OFPFC_ADD,
            idle_timeout=spec.idle_timeout or self.idle_timeout,
            hard_timeout=spec.hard_timeout or self.hard_timeout,
            priority=spec.priority,
            match=match,
            instructions=instructions,
        )
        datapath.send_msg(mod)
        self.stats.installed += 1
        LOGGER.debug("installed flow: %s", spec.describe())
        return True

    def install_many(self, specs: Iterable[FlowSpec]) -> int:
        """Install several flows; returns how many were accepted."""
        installed = 0
        for spec in specs:
            if self.install(spec):
                installed += 1
        if installed:
            LOGGER.info("installed %d flow(s)", installed)
        return installed

    def delete(self, spec: FlowSpec) -> bool:
        """Remove a flow by exact match (OFPFC_DELETE)."""
        datapath = self.datapaths.get(spec.switch)
        if datapath is None:
            return False
        mod = datapath.ofproto_parser.OFPFlowMod(
            datapath=datapath,
            cookie=self._cookie(spec),
            table_id=datapath.ofproto.OFPTT_ALL,
            command=datapath.ofproto.OFPFC_DELETE,
            priority=spec.priority,
            match=self.build_match(spec.match),
        )
        datapath.send_msg(mod)
        self.stats.deleted += 1
        LOGGER.debug("deleted flow: %s", spec.describe())
        return True

    def delete_match(self, switch: str, match: Dict[str, Any], priority: int = 100) -> bool:
        """Delete every flow on ``switch`` matching ``match`` at ``priority``."""
        datapath = self.datapaths.get(switch)
        if datapath is None:
            return False
        mod = datapath.ofproto_parser.OFPFlowMod(
            datapath=datapath,
            table_id=datapath.ofproto.OFPTT_ALL,
            command=datapath.ofproto.OFPFC_DELETE,
            priority=priority,
            match=self.build_match(match),
        )
        datapath.send_msg(mod)
        self.stats.deleted += 1
        LOGGER.debug("deleted flows on %s matching %s", switch, match)
        return True

    def clear_switch(self, switch: str) -> bool:
        """Remove all non-table-miss flows by wildcard delete at min priority.

        A wildcard match with OFPFC_DELETE removes every flow of that priority;
        we therefore delete priorities above the table-miss priority in a small
        loop rather than trying to express "everything but table-miss" in one
        message.
        """
        datapath = self.datapaths.get(switch)
        if datapath is None:
            return False
        cleared = 0
        for priority in range(TABLE_MISS_PRIORITY + 1, 65535):
            if not self.delete_match(switch, {}, priority=priority):
                return False
            cleared += 1
            if cleared > 64:  # safety valve; real flows use low priorities
                break
        LOGGER.info("cleared forwarding flows on %s", switch)
        return True

    # ------------------------------------------------------------ internals --
    @staticmethod
    def _cookie(spec: FlowSpec) -> int:
        """Derive a stable cookie from the spec so flows are identifiable.

        Uses a fixed base plus the priority; the exact value is not
        semantically meaningful, only stable and non-zero.
        """
        return 0x5344_4E00 | (spec.priority & 0xFFFF)


def table_miss_spec(switch: str, idle_timeout: int = 0) -> FlowSpec:
    """Build the per-switch table-miss rule: match nothing, send to controller."""
    return FlowSpec(
        switch=switch,
        match={},
        out_port=None,
        priority=TABLE_MISS_PRIORITY,
        idle_timeout=idle_timeout,
        reason="table-miss -> controller",
    )