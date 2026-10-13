"""Network telemetry collector for AI-driven SDN traffic optimization.

This module provides real-time telemetry collection, counter rate computation,
active probe latency/loss measurement, and dataset logging as specified in
Phase 5 design rules:

1. Rate Computation:
   - Rates are computed from counter DIFFERENCES divided by actual elapsed time
     (monotonic clock), handling 64-bit wraps and counter resets/reboots.
   - Per-link directional metrics: (src_switch, dst_switch, direction).
   - Utilization = tx_rate_mbps / capacity_mbps.

2. Latency & Loss Measurement:
   - Active latency probes (controller-injected, nanosecond-timestamped frames),
     never derived from static port stats.
   - Composite loss derived from tx_dropped / rx_errors counters combined with
     probe loss over a configurable sliding window.

3. Resilience & Fallback:
   - Tracks telemetry freshness and flags stale data.
   - Detects telemetry stall when updates cease beyond the stall timeout.
   - Emits structured event logs and writes continuous telemetry snapshots
     to CSV for ML training (Phase 6/7) and API monitoring (Phase 12).
"""

from __future__ import annotations

import csv
import logging
import os
import struct
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from ryu.lib.packet import ethernet, packet

from common.config import Config, load_config
from common.logging_utils import get_event_log

LOGGER = logging.getLogger("controller.telemetry")

# --------------------------------------------------------------------------- #
# Probe Protocol Specification
# --------------------------------------------------------------------------- #
# IEEE 802 Local Experimental Ethertype 1
PROBE_ETHERTYPE = 0x88B5
PROBE_MAGIC = b"SDNP"

# Struct: magic (4s), src_dpid (Q), src_port (I), dst_dpid (Q), dst_port (I),
#         seq (Q), send_time_ns (Q)
PROBE_STRUCT = struct.Struct("!4sQIQIQQ")
PROBE_PAYLOAD_LEN = PROBE_STRUCT.size  # 44 bytes

UINT64_MAX = 2**64
# If previous counter is within 10^12 of 2^64, treat drop as 64-bit wrap.
# Otherwise, treat as switch reboot / counter clear.
COUNTER_WRAP_THRESHOLD = UINT64_MAX - 1_000_000_000_000

CSV_FIELDNAMES = [
    "run_id",
    "timestamp",
    "src_switch",
    "dst_switch",
    "direction",
    "src_port",
    "dst_port",
    "capacity_mbps",
    "tx_rate_mbps",
    "rx_rate_mbps",
    "tx_packet_rate_pps",
    "rx_packet_rate_pps",
    "utilization",
    "latency_ms",
    "loss_pct",
    "tx_bytes",
    "rx_bytes",
    "tx_packets",
    "rx_packets",
    "tx_dropped",
    "rx_errors",
    "probe_loss_pct",
    "is_stale",
    "is_congested",
    # P8's forecast for this link, as of this sample. Empty when no forecast was
    # available (no model, stale telemetry, or too little history) -- which is
    # deliberately distinguishable from a forecast of 0.0. Without this column
    # the predictor's output is invisible outside the controller, so no
    # experiment or demonstration can correlate a forecast with what followed.
    "p_congestion",
    "predicted_congested",
]


def diff_with_wrap(curr: int, prev: int, max_val: int = UINT64_MAX) -> int:
    """Compute difference between counter values, handling wrap and reset.

    Parameters
    ----------
    curr:
        Current counter value.
    prev:
        Previous counter value.
    max_val:
        Maximum integer representation for the counter (default: 2^64).

    Returns
    -------
    int:
        Monotonic delta. Returns 0 if counter was reset/restarted.
    """
    if curr >= prev:
        return curr - prev
    # Counter wrapped around 64-bit limit
    if prev > COUNTER_WRAP_THRESHOLD:
        return (curr + max_val) - prev
    # Switch restart or counter reset (e.g. ovs-ofctl replace-flows)
    LOGGER.warning("Counter reset detected: curr=%d < prev=%d; resetting delta to 0", curr, prev)
    return 0


def build_probe_packet(
    src_dpid: int,
    src_port: int,
    dst_dpid: int,
    dst_port: int,
    seq: int,
    src_mac: str,
    send_time_ns: Optional[int] = None,
) -> bytes:
    """Build a serialized Ethernet probe frame containing high-res timestamp.

    Parameters
    ----------
    src_dpid:
        Source switch datapath ID.
    src_port:
        Source egress switch port.
    dst_dpid:
        Destination switch datapath ID.
    dst_port:
        Destination ingress switch port.
    seq:
        Monotonic sequence number.
    src_mac:
        Source MAC address for the Ethernet header.
    send_time_ns:
        Nanosecond monotonic timestamp. Defaults to current time.
    """
    if send_time_ns is None:
        send_time_ns = time.monotonic_ns()

    payload = PROBE_STRUCT.pack(
        PROBE_MAGIC,
        int(src_dpid),
        int(src_port),
        int(dst_dpid),
        int(dst_port),
        int(seq),
        int(send_time_ns),
    )

    eth = ethernet.ethernet(
        dst="ff:ff:ff:ff:ff:fe",
        src=src_mac,
        ethertype=PROBE_ETHERTYPE,
    )

    pkt = packet.Packet()
    pkt.add_protocol(eth)
    pkt.add_protocol(payload)
    pkt.serialize()
    return pkt.data


def parse_probe_packet(raw_data: bytes) -> Optional[Tuple[int, int, int, int, int, int]]:
    """Parse an Ethernet probe frame.

    Returns
    -------
    Optional[Tuple[int, int, int, int, int, int]]:
        (src_dpid, src_port, dst_dpid, dst_port, seq, send_time_ns) or None if invalid.
    """
    if len(raw_data) < 14 + PROBE_PAYLOAD_LEN:
        return None
    try:
        pkt = packet.Packet(raw_data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype != PROBE_ETHERTYPE:
            return None

        payload_bytes = raw_data[14 : 14 + PROBE_PAYLOAD_LEN]
        magic, src_dpid, src_port, dst_dpid, dst_port, seq, send_time_ns = PROBE_STRUCT.unpack(
            payload_bytes
        )
        if magic != PROBE_MAGIC:
            return None
        return (src_dpid, src_port, dst_dpid, dst_port, seq, send_time_ns)
    except Exception as exc:
        LOGGER.debug("Failed to parse probe packet: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Data Classes
# --------------------------------------------------------------------------- #
@dataclass
class PortStatsSnapshot:
    """Raw port counter sample with monotonic and wall-clock timestamps."""

    port_no: int
    tx_bytes: int
    rx_bytes: int
    tx_packets: int
    rx_packets: int
    tx_dropped: int
    rx_dropped: int
    tx_errors: int
    rx_errors: int
    time_monotonic: float
    time_wall: float


@dataclass
class PortRateRecord:
    """Calculated rates and deltas over actual elapsed monotonic time."""

    switch: str
    port_no: int
    delta_s: float
    tx_bytes: int
    rx_bytes: int
    tx_packets: int
    rx_packets: int
    tx_dropped: int
    rx_errors: int
    tx_rate_mbps: float
    rx_rate_mbps: float
    tx_packet_rate_pps: float
    rx_packet_rate_pps: float
    is_stale: bool
    time_wall: float


@dataclass
class ProbeSample:
    """Individual probe measurement."""

    seq: int
    send_time_ns: int
    recv_time_ns: Optional[int]
    latency_ms: Optional[float]
    lost: bool


@dataclass
class LinkTelemetryRecord:
    """Unified telemetry record for a directional inter-switch link."""

    run_id: str
    timestamp: float
    src_switch: str
    dst_switch: str
    direction: str  # "forward" or "reverse"
    src_port: int
    dst_port: int
    capacity_mbps: float
    tx_rate_mbps: float
    rx_rate_mbps: float
    tx_packet_rate_pps: float
    rx_packet_rate_pps: float
    utilization: float
    latency_ms: float
    loss_pct: float
    tx_bytes: int
    rx_bytes: int
    tx_packets: int
    rx_packets: int
    tx_dropped: int
    rx_errors: int
    probe_loss_pct: float
    is_stale: bool
    is_congested: bool
    #: P8 forecast, or None when no forecast was available. None is not 0.0.
    p_congestion: Optional[float] = None
    #: True only when a forecast existed and met the decision threshold.
    predicted_congested: bool = False

    def as_csv_dict(self) -> Dict[str, Any]:
        """Convert to dictionary matching CSV_FIELDNAMES."""
        return {
            "run_id": self.run_id,
            "timestamp": f"{self.timestamp:.4f}",
            "src_switch": self.src_switch,
            "dst_switch": self.dst_switch,
            "direction": self.direction,
            "src_port": self.src_port,
            "dst_port": self.dst_port,
            "capacity_mbps": f"{self.capacity_mbps:.1f}",
            "tx_rate_mbps": f"{self.tx_rate_mbps:.4f}",
            "rx_rate_mbps": f"{self.rx_rate_mbps:.4f}",
            "tx_packet_rate_pps": f"{self.tx_packet_rate_pps:.2f}",
            "rx_packet_rate_pps": f"{self.rx_packet_rate_pps:.2f}",
            "utilization": f"{self.utilization:.4f}",
            "latency_ms": f"{self.latency_ms:.3f}",
            "loss_pct": f"{self.loss_pct:.3f}",
            "tx_bytes": self.tx_bytes,
            "rx_bytes": self.rx_bytes,
            "tx_packets": self.tx_packets,
            "rx_packets": self.rx_packets,
            "tx_dropped": self.tx_dropped,
            "rx_errors": self.rx_errors,
            "probe_loss_pct": f"{self.probe_loss_pct:.2f}",
            "is_stale": self.is_stale,
            "is_congested": self.is_congested,
            # Empty rather than 0.0 when no forecast was available, so "we could
            # not predict" never reads as "predicted fine".
            "p_congestion": ("" if self.p_congestion is None
                             else f"{self.p_congestion:.4f}"),
            "predicted_congested": self.predicted_congested,
        }


# --------------------------------------------------------------------------- #
# Control-Channel Echo RTT Tracker (Bias Compensation)
# --------------------------------------------------------------------------- #
class ControlChannelEchoTracker:
    """Measures and maintains OpenFlow control-channel echo RTT per switch.

    Active probe frames (0x88B5) travel:
        Controller -> Switch A -> (Data Link) -> Switch B -> Controller
    The raw timestamp delta includes control-channel roundtrip transit between
    the controller and the switches. By measuring OFPEchoRequest / OFPEchoReply
    roundtrips to each switch, the control overhead can be calculated as:
        control_overhead = (echo_rtt(Switch A) + echo_rtt(Switch B)) / 2
    Subtracting this overhead yields the unbiased data-plane link latency:
        t_link = max(0.0, t_probe_raw - control_overhead)
    """

    def __init__(self, smoothing_alpha: float = 0.3) -> None:
        self.alpha = smoothing_alpha
        # dpid -> smoothed echo RTT in ms
        self.smoothed_rtt_ms: Dict[int, float] = {}
        # (dpid, xid) -> send_time_ns
        self.pending_echos: Dict[Tuple[int, int], int] = {}

    def record_echo_request(self, dpid: int, xid: int, send_time_ns: Optional[int] = None) -> None:
        """Register transmission of an OFPEchoRequest."""
        now_ns = send_time_ns if send_time_ns is not None else time.monotonic_ns()
        self.pending_echos[(dpid, xid)] = now_ns

    def record_echo_reply(self, dpid: int, xid: int, recv_time_ns: Optional[int] = None) -> Optional[float]:
        """Process an OFPEchoReply and update smoothed RTT for datapath."""
        now_ns = recv_time_ns if recv_time_ns is not None else time.monotonic_ns()
        send_ns = self.pending_echos.pop((dpid, xid), None)
        if send_ns is None:
            return None
        rtt_ms = max(0.0, (now_ns - send_ns) / 1_000_000.0)
        prev = self.smoothed_rtt_ms.get(dpid)
        if prev is None:
            self.smoothed_rtt_ms[dpid] = rtt_ms
        else:
            self.smoothed_rtt_ms[dpid] = self.alpha * rtt_ms + (1.0 - self.alpha) * prev
        return self.smoothed_rtt_ms[dpid]

    def get_echo_rtt_ms(self, dpid: int) -> float:
        """Get latest smoothed echo RTT in ms for a datapath (0.0 if unmeasured)."""
        return self.smoothed_rtt_ms.get(dpid, 0.0)

    def get_link_control_overhead_ms(self, src_dpid: int, dst_dpid: int) -> float:
        """Compute estimated control-channel bias for a probe between src and dst.

        Downlink bias: echo_rtt(src_dpid) / 2
        Uplink bias: echo_rtt(dst_dpid) / 2
        Total bias: (echo_rtt(src) + echo_rtt(dst)) / 2
        """
        rtt_src = self.get_echo_rtt_ms(src_dpid)
        rtt_dst = self.get_echo_rtt_ms(dst_dpid)
        return (rtt_src + rtt_dst) / 2.0


# --------------------------------------------------------------------------- #
# Probe Tracker
# --------------------------------------------------------------------------- #
class LinkProbeTracker:
    """Maintains a sliding window of active latency/loss probes per directional link."""

    def __init__(self, window_size: int = 30, timeout_s: float = 0.5) -> None:
        self.window_size = window_size
        self.timeout_s = timeout_s
        self.samples: Deque[ProbeSample] = deque(maxlen=window_size)
        self.pending_probes: Dict[int, ProbeSample] = {}
        self.last_latency_ms: float = 0.0

    def record_sent(self, seq: int, send_time_ns: int) -> None:
        """Register a newly transmitted probe."""
        sample = ProbeSample(
            seq=seq,
            send_time_ns=send_time_ns,
            recv_time_ns=None,
            latency_ms=None,
            lost=False,
        )
        self.pending_probes[seq] = sample

    def record_received(
        self,
        seq: int,
        recv_time_ns: int,
        control_overhead_ms: float = 0.0,
    ) -> Optional[float]:
        """Record receipt of a probe and update latency, subtracting control overhead."""
        sample = self.pending_probes.pop(seq, None)
        if sample is None:
            return None
        sample.recv_time_ns = recv_time_ns
        raw_latency_ms = (recv_time_ns - sample.send_time_ns) / 1_000_000.0
        # Compensate for control-plane loopback / controller-switch transit bias
        compensated_ms = max(0.0, raw_latency_ms - control_overhead_ms)
        sample.latency_ms = compensated_ms
        self.last_latency_ms = compensated_ms
        self.samples.append(sample)
        return compensated_ms

    def check_timeouts(self, current_time_ns: int) -> int:
        """Expire pending probes older than timeout_s and record as lost."""
        timeout_ns = int(self.timeout_s * 1e9)
        expired_seqs = [
            seq
            for seq, sample in self.pending_probes.items()
            if (current_time_ns - sample.send_time_ns) > timeout_ns
        ]
        for seq in expired_seqs:
            sample = self.pending_probes.pop(seq)
            sample.lost = True
            self.samples.append(sample)
        return len(expired_seqs)

    @property
    def mean_latency_ms(self) -> float:
        """Mean latency across non-lost probes in the window."""
        valid = [s.latency_ms for s in self.samples if not s.lost and s.latency_ms is not None]
        if not valid:
            return self.last_latency_ms
        return sum(valid) / len(valid)

    @property
    def loss_pct(self) -> float:
        """Probe loss percentage in the sliding window."""
        if not self.samples:
            return 0.0
        lost_count = sum(1 for s in self.samples if s.lost)
        return (lost_count / len(self.samples)) * 100.0


# --------------------------------------------------------------------------- #
# Telemetry Collector
# --------------------------------------------------------------------------- #
class TelemetryCollector:
    """Central telemetry collector aggregating port stats and active probes.

    Parameters
    ----------
    config:
        Parsed system configuration.
    run_id:
        Identifier for the experiment run (used in CSV output).
    """

    def __init__(self, config: Optional[Config] = None, run_id: Optional[str] = None) -> None:
        self.config = config if config is not None else load_config()
        self.run_id = run_id or f"run_{int(time.time())}"
        self.sample_interval_s = float(self.config.get("telemetry.sample_interval_s", 2.0))
        self.stall_timeout_multiple = float(
            self.config.get("telemetry.stall_timeout_multiple", 3.0)
        )
        self.stall_threshold_s = self.sample_interval_s * self.stall_timeout_multiple

        self.congestion_util_thresh = float(
            self.config.get("telemetry.congestion_utilization_threshold", 0.80)
        )
        self.congestion_loss_thresh = float(
            self.config.get("telemetry.congestion_loss_threshold_pct", 2.0)
        )
        self.congestion_latency_thresh = float(
            self.config.get("telemetry.congestion_latency_threshold_ms", 40.0)
        )

        probe_window = int(self.config.get("telemetry.probe.window", 30))
        probe_timeout = float(self.config.get("telemetry.probe.timeout_s", 0.5))

        self.csv_path = str(self.config.get("telemetry.csv_path", "data/telemetry.csv"))
        self.event_log = get_event_log(self.config)

        # Port counter histories: (switch, port_no) -> PortStatsSnapshot
        self.prev_port_stats: Dict[Tuple[str, int], PortStatsSnapshot] = {}
        # Latest computed port rates: (switch, port_no) -> PortRateRecord
        self.latest_port_rates: Dict[Tuple[str, int], PortRateRecord] = {}

        # Probe trackers per directional link: (src_switch, dst_switch) -> LinkProbeTracker
        self.probe_trackers: Dict[Tuple[str, str], LinkProbeTracker] = {}
        self.probe_window = probe_window
        self.probe_timeout = probe_timeout
        self.probe_seq = 0

        # Link metadata: (src_switch, src_port) -> (dst_switch, dst_port, capacity_mbps, default_delay_ms)
        self.link_map: Dict[Tuple[str, int], Tuple[str, int, float, float]] = {}

        # Latest computed link records: (src_switch, dst_switch, direction) -> LinkTelemetryRecord
        self.latest_link_records: Dict[Tuple[str, str, str], LinkTelemetryRecord] = {}
        # Latest P8 forecast per link, set by the reroute loop each tick. A link
        # absent from this dict has *no* forecast, which is different from a
        # forecast of zero -- see the CSV comment.
        self.forecasts: Dict[Tuple[str, str, str], Tuple[float, bool]] = {}

        # Control-channel echo RTT tracker for active probe latency bias compensation
        self.echo_tracker = ControlChannelEchoTracker()

        self.last_sample_monotonic = time.monotonic()
        self._csv_header_written = False

    def apply_forecasts(
        self, forecasts: Dict[Tuple[str, str, str], Tuple[Optional[float], bool]]
    ) -> int:
        """Record the latest per-link congestion forecast.

        Parameters
        ----------
        forecasts:
            ``(src, dst, direction) -> (probability, predicted_congested)``.
            A ``None`` probability means the predictor declined to score that
            link (stale, short history, or degraded) and is stored as "no
            forecast" rather than as zero.

        Returns
        -------
        int
            How many links carried a usable forecast, so a caller can see a
            completely unscored tick rather than assume it succeeded.

        Notes
        -----
        The previous tick's forecasts are dropped first. Carrying them forward
        would let a link keep reporting a confident probability derived from
        telemetry that has since gone stale, which is exactly the fiction P8
        exists to avoid.
        """
        self.forecasts = {}
        scored = 0
        for key, (probability, flagged) in forecasts.items():
            if probability is None:
                self.forecasts[key] = (None, False)
                continue
            self.forecasts[key] = (float(probability), bool(flagged))
            scored += 1
        return scored

    def register_link(
        self,
        src_switch: str,
        src_port: int,
        dst_switch: str,
        dst_port: int,
        capacity_mbps: float = 10.0,
        delay_ms: float = 5.0,
    ) -> None:
        """Register an inter-switch physical link for telemetry tracking."""
        self.link_map[(src_switch, src_port)] = (dst_switch, dst_port, capacity_mbps, delay_ms)
        if (src_switch, dst_switch) not in self.probe_trackers:
            self.probe_trackers[(src_switch, dst_switch)] = LinkProbeTracker(
                window_size=self.probe_window, timeout_s=self.probe_timeout
            )
            self.probe_trackers[(src_switch, dst_switch)].last_latency_ms = delay_ms

    def record_port_stats_reply(
        self,
        switch: str,
        port_no: int,
        tx_bytes: int,
        rx_bytes: int,
        tx_packets: int,
        rx_packets: int,
        tx_dropped: int,
        rx_dropped: int,
        tx_errors: int,
        rx_errors: int,
        timestamp_monotonic: Optional[float] = None,
        timestamp_wall: Optional[float] = None,
    ) -> Optional[PortRateRecord]:
        """Record a port stats sample and compute rate from counter difference over actual dt.

        Parameters
        ----------
        switch:
            Name of the switch (e.g. 's1').
        port_no:
            OpenFlow port number.
        tx_bytes, rx_bytes, tx_packets, rx_packets, tx_dropped, rx_dropped, tx_errors, rx_errors:
            Raw 64-bit counters from OFPPortStats.
        timestamp_monotonic:
            Monotonic time of reception. Defaults to time.monotonic().
        timestamp_wall:
            Wall-clock epoch time. Defaults to time.time().
        """
        now_mono = timestamp_monotonic if timestamp_monotonic is not None else time.monotonic()
        now_wall = timestamp_wall if timestamp_wall is not None else time.time()

        curr_snap = PortStatsSnapshot(
            port_no=port_no,
            tx_bytes=tx_bytes,
            rx_bytes=rx_bytes,
            tx_packets=tx_packets,
            rx_packets=rx_packets,
            tx_dropped=tx_dropped,
            rx_dropped=rx_dropped,
            tx_errors=tx_errors,
            rx_errors=rx_errors,
            time_monotonic=now_mono,
            time_wall=now_wall,
        )

        prev_snap = self.prev_port_stats.get((switch, port_no))
        self.prev_port_stats[(switch, port_no)] = curr_snap

        if prev_snap is None:
            # First sample: cannot compute rate yet, baseline recorded
            return None

        delta_s = now_mono - prev_snap.time_monotonic
        if delta_s <= 0.0001:
            LOGGER.debug("%s:%d dt too small (%.4f s); skipping", switch, port_no, delta_s)
            return None

        delta_tx_bytes = diff_with_wrap(curr_snap.tx_bytes, prev_snap.tx_bytes)
        delta_rx_bytes = diff_with_wrap(curr_snap.rx_bytes, prev_snap.rx_bytes)
        delta_tx_pkts = diff_with_wrap(curr_snap.tx_packets, prev_snap.tx_packets)
        delta_rx_pkts = diff_with_wrap(curr_snap.rx_packets, prev_snap.rx_packets)
        delta_tx_drop = diff_with_wrap(curr_snap.tx_dropped, prev_snap.tx_dropped)
        delta_rx_err = diff_with_wrap(curr_snap.rx_errors, prev_snap.rx_errors)

        # Rate calculations (bps -> Mbps)
        tx_rate_mbps = (delta_tx_bytes * 8.0) / (delta_s * 1e6)
        rx_rate_mbps = (delta_rx_bytes * 8.0) / (delta_s * 1e6)
        tx_packet_rate_pps = delta_tx_pkts / delta_s
        rx_packet_rate_pps = delta_rx_pkts / delta_s

        is_stale = delta_s > self.stall_threshold_s

        rate_rec = PortRateRecord(
            switch=switch,
            port_no=port_no,
            delta_s=delta_s,
            tx_bytes=curr_snap.tx_bytes,
            rx_bytes=curr_snap.rx_bytes,
            tx_packets=curr_snap.tx_packets,
            rx_packets=curr_snap.rx_packets,
            tx_dropped=curr_snap.tx_dropped,
            rx_errors=curr_snap.rx_errors,
            tx_rate_mbps=tx_rate_mbps,
            rx_rate_mbps=rx_rate_mbps,
            tx_packet_rate_pps=tx_packet_rate_pps,
            rx_packet_rate_pps=rx_packet_rate_pps,
            is_stale=is_stale,
            time_wall=now_wall,
        )

        self.latest_port_rates[(switch, port_no)] = rate_rec
        self.last_sample_monotonic = now_mono
        return rate_rec

    def get_probe_tracker(self, src_switch: str, dst_switch: str) -> LinkProbeTracker:
        """Get or create the LinkProbeTracker for a switch pair."""
        key = (src_switch, dst_switch)
        if key not in self.probe_trackers:
            self.probe_trackers[key] = LinkProbeTracker(
                window_size=self.probe_window, timeout_s=self.probe_timeout
            )
        return self.probe_trackers[key]

    def record_probe_sent(self, src_switch: str, dst_switch: str, seq: int, send_time_ns: int) -> None:
        """Register that a probe was sent from src_switch to dst_switch."""
        tracker = self.get_probe_tracker(src_switch, dst_switch)
        tracker.record_sent(seq, send_time_ns)

    def record_echo_request(self, dpid: int, xid: int, send_time_ns: Optional[int] = None) -> None:
        """Register transmission of an OFPEchoRequest."""
        self.echo_tracker.record_echo_request(dpid, xid, send_time_ns)

    def record_echo_reply(self, dpid: int, xid: int, recv_time_ns: Optional[int] = None) -> Optional[float]:
        """Record receipt of an OFPEchoReply and update smoothed RTT."""
        return self.echo_tracker.record_echo_reply(dpid, xid, recv_time_ns)

    def get_link_control_overhead_ms(self, src_switch: str, dst_switch: str) -> float:
        """Compute estimated control-channel overhead between src_switch and dst_switch."""
        try:
            src_dpid = int(src_switch.replace("s", ""))
            dst_dpid = int(dst_switch.replace("s", ""))
            return self.echo_tracker.get_link_control_overhead_ms(src_dpid, dst_dpid)
        except Exception:
            return 0.0

    def record_probe_received(
        self,
        src_switch: str,
        dst_switch: str,
        seq: int,
        recv_time_ns: Optional[int] = None,
        control_overhead_ms: Optional[float] = None,
    ) -> Optional[float]:
        """Register that a probe returned from dst_switch and compute latency."""
        if recv_time_ns is None:
            recv_time_ns = time.monotonic_ns()
        if control_overhead_ms is None:
            control_overhead_ms = self.get_link_control_overhead_ms(src_switch, dst_switch)
        tracker = self.get_probe_tracker(src_switch, dst_switch)
        return tracker.record_received(seq, recv_time_ns, control_overhead_ms=control_overhead_ms)

    def check_probe_timeouts(self) -> int:
        """Expire timed-out probes across all links."""
        now_ns = time.monotonic_ns()
        total_expired = 0
        for tracker in self.probe_trackers.values():
            total_expired += tracker.check_timeouts(now_ns)
        return total_expired

    def update_link_telemetry(self) -> Dict[Tuple[str, str, str], LinkTelemetryRecord]:
        """Aggregate latest port rates and probe stats into per-link directional records.

        Returns
        -------
        Dict[Tuple[str, str, str], LinkTelemetryRecord]:
            Keyed by (src_switch, dst_switch, direction), where direction is 'forward'
            for traffic flowing from src_switch to dst_switch.
        """
        self.check_probe_timeouts()
        results: Dict[Tuple[str, str, str], LinkTelemetryRecord] = {}

        for (src_sw, src_p), (dst_sw, dst_p, cap_mbps, def_delay) in self.link_map.items():
            rate_rec = self.latest_port_rates.get((src_sw, src_p))
            probe_tracker = self.probe_trackers.get((src_sw, dst_sw))

            # Skip links with no rate record yet. A rate needs two counter
            # readings, so during the first poll after a switch connects there is
            # legitimately nothing to report. Emitting a row here fabricates a
            # zero-valued observation flagged is_stale=True -- one per link per
            # run -- and the labeller treats those fakes as real samples. That
            # single warm-up artefact was a 7.1% stale fraction in every run.
            if rate_rec is None:
                LOGGER.debug(
                    "%s:%d has no rate record yet (first poll); skipping row",
                    src_sw, src_p,
                )
                continue

            tx_rate = rate_rec.tx_rate_mbps if rate_rec else 0.0
            rx_rate = rate_rec.rx_rate_mbps if rate_rec else 0.0
            tx_pps = rate_rec.tx_packet_rate_pps if rate_rec else 0.0
            rx_pps = rate_rec.rx_packet_rate_pps if rate_rec else 0.0
            is_stale = rate_rec.is_stale if rate_rec else True
            timestamp = rate_rec.time_wall if rate_rec else time.time()

            tx_bytes = rate_rec.tx_bytes if rate_rec else 0
            rx_bytes = rate_rec.rx_bytes if rate_rec else 0
            tx_packets = rate_rec.tx_packets if rate_rec else 0
            rx_packets = rate_rec.rx_packets if rate_rec else 0
            tx_dropped = rate_rec.tx_dropped if rate_rec else 0
            rx_errors = rate_rec.rx_errors if rate_rec else 0

            # Utilization
            utilization = tx_rate / cap_mbps if cap_mbps > 0 else 0.0

            # Latency from active probes
            latency_ms = probe_tracker.mean_latency_ms if probe_tracker else def_delay
            if latency_ms <= 0.0:
                latency_ms = def_delay

            # Loss computation
            probe_loss_pct = probe_tracker.loss_pct if probe_tracker else 0.0

            # Port drop percentage
            port_loss_pct = 0.0
            if rate_rec and (rate_rec.tx_packets + rate_rec.tx_dropped) > 0:
                port_loss_pct = (
                    (rate_rec.tx_dropped + rate_rec.rx_errors)
                    / float(rate_rec.tx_packets + rate_rec.tx_dropped)
                ) * 100.0

            # Combined loss: maximum of interface drops and probe packet loss
            loss_pct = min(100.0, max(port_loss_pct, probe_loss_pct))

            # Congestion classification
            is_congested = (
                utilization >= self.congestion_util_thresh
                or loss_pct >= self.congestion_loss_thresh
                or latency_ms >= self.congestion_latency_thresh
            )

            rec = LinkTelemetryRecord(
                run_id=self.run_id,
                timestamp=timestamp,
                src_switch=src_sw,
                dst_switch=dst_sw,
                direction="forward",
                src_port=src_p,
                dst_port=dst_p,
                capacity_mbps=cap_mbps,
                tx_rate_mbps=tx_rate,
                rx_rate_mbps=rx_rate,
                tx_packet_rate_pps=tx_pps,
                rx_packet_rate_pps=rx_pps,
                utilization=utilization,
                latency_ms=latency_ms,
                loss_pct=loss_pct,
                tx_bytes=tx_bytes,
                rx_bytes=rx_bytes,
                tx_packets=tx_packets,
                rx_packets=rx_packets,
                tx_dropped=tx_dropped,
                rx_errors=rx_errors,
                probe_loss_pct=probe_loss_pct,
                is_stale=is_stale,
                is_congested=is_congested,
            )

            key = (src_sw, dst_sw, "forward")
            probability, flagged = self.forecasts.get(key, (None, False))
            rec.p_congestion = probability
            rec.predicted_congested = bool(flagged) if probability is not None \
                else False
            results[key] = rec
            self.latest_link_records[key] = rec

        return results

    def write_csv_snapshot(self) -> List[LinkTelemetryRecord]:
        """Write current link records to CSV file."""
        records = list(self.update_link_telemetry().values())
        if not records:
            return []

        os.makedirs(os.path.dirname(self.csv_path) or ".", exist_ok=True)
        write_header = not os.path.exists(self.csv_path) or os.path.getsize(self.csv_path) == 0

        with open(self.csv_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
            if write_header:
                writer.writeheader()
                self._csv_header_written = True
            for r in records:
                writer.writerow(r.as_csv_dict())

        return records

    def is_telemetry_stalled(self) -> bool:
        """Check if telemetry updates have stalled beyond the stall timeout."""
        elapsed = time.monotonic() - self.last_sample_monotonic
        return elapsed > self.stall_threshold_s

    def export_snapshot(self) -> Dict[str, Any]:
        """Export JSON-serializable telemetry state for API and controller snapshot."""
        return {
            "run_id": self.run_id,
            "stalled": self.is_telemetry_stalled(),
            "sample_interval_s": self.sample_interval_s,
            "links": {
                f"{src}->{dst}:{dir_}": asdict(rec)
                for (src, dst, dir_), rec in self.latest_link_records.items()
            },
        }
