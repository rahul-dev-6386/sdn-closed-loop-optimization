"""Unit tests for Phase 5 Telemetry System.

Tests cover:
1. Rate computation from counter differences over monotonic elapsed time.
2. 64-bit unsigned integer counter wrap handling.
3. Counter reset / switch reboot handling (delta resets safely, no huge spikes).
4. Directional link telemetry keying: (src_switch, dst_switch, direction).
5. Utilization calculation relative to configured link capacity.
6. Active probe frame serialization, parsing, latency calculation, and probe loss.
7. Composite loss calculation (port drops + probe packet loss).
8. Telemetry staleness flagging and stall detection.
9. CSV formatting, header validation, and continuous snapshot export.
"""

from __future__ import annotations

import csv
import os
import time
from typing import Dict

import pytest

from common.config import load_config
from controller.telemetry import (
    CSV_FIELDNAMES,
    ControlChannelEchoTracker,
    LinkProbeTracker,
    TelemetryCollector,
    build_probe_packet,
    diff_with_wrap,
    parse_probe_packet,
)


# --------------------------------------------------------------------------- #
# 1. Counter Differences & Wrap / Reset Handling
# --------------------------------------------------------------------------- #
def test_diff_with_wrap_monotonic_increment() -> None:
    """Standard monotonic counter increments calculate exact difference."""
    assert diff_with_wrap(1500, 1000) == 500
    assert diff_with_wrap(1000000, 0) == 1000000
    assert diff_with_wrap(50, 50) == 0


def test_diff_with_wrap_64bit_overflow() -> None:
    """When a 64-bit counter wraps near 2^64, difference accounts for wrap."""
    uint64_max = 2**64
    prev = uint64_max - 500  # near upper boundary
    curr = 300               # wrapped past zero
    expected_delta = 800     # 500 to reach 2^64 + 300
    assert diff_with_wrap(curr, prev) == expected_delta


def test_diff_with_wrap_counter_reset_does_not_spike() -> None:
    """When switch reboots or counter resets, delta safely drops to 0."""
    prev = 100_000_000       # normal non-overflow counter
    curr = 100               # sudden counter reset (e.g. OVS restart)
    # Must NOT produce ~1.84 * 10^19 bytes (73 Exabits)
    assert diff_with_wrap(curr, prev) == 0


# --------------------------------------------------------------------------- #
# 2. Rate Calculation over Actual Monotonic Elapsed Time
# --------------------------------------------------------------------------- #
def test_rate_computation_over_actual_dt() -> None:
    """Rates must be computed using actual elapsed monotonic time, not nominal interval."""
    cfg = load_config()
    collector = TelemetryCollector(cfg, run_id="test_rates")

    # t0: Initial baseline sample
    t0_mono = 100.0
    collector.record_port_stats_reply(
        switch="s1",
        port_no=2,
        tx_bytes=10_000_000,
        rx_bytes=5_000_000,
        tx_packets=10_000,
        rx_packets=5_000,
        tx_dropped=0,
        rx_dropped=0,
        tx_errors=0,
        rx_errors=0,
        timestamp_monotonic=t0_mono,
    )

    # t1: Sample arrives after 2.5 seconds (not nominal 2.0 s)
    t1_mono = 102.5
    dt = 2.5
    # Transferred 2.5 MB = 20 Megabits in 2.5 s -> exact rate = 8.0 Mbps
    delta_bytes = int(2.5 * 10**6)
    delta_packets = 2500

    rate_rec = collector.record_port_stats_reply(
        switch="s1",
        port_no=2,
        tx_bytes=10_000_000 + delta_bytes,
        rx_bytes=5_000_000,
        tx_packets=10_000 + delta_packets,
        rx_packets=5_000,
        tx_dropped=0,
        rx_dropped=0,
        tx_errors=0,
        rx_errors=0,
        timestamp_monotonic=t1_mono,
    )

    assert rate_rec is not None
    assert rate_rec.delta_s == pytest.approx(dt, rel=1e-3)
    # (2,500,000 * 8) / (2.5 * 1,000,000) = 8.0 Mbps
    assert rate_rec.tx_rate_mbps == pytest.approx(8.0, rel=1e-3)
    assert rate_rec.tx_packet_rate_pps == pytest.approx(1000.0, rel=1e-3)


# --------------------------------------------------------------------------- #
# 3. Directional Keying, Capacity & Utilization
# --------------------------------------------------------------------------- #
def test_directional_link_utilization() -> None:
    """Link record is keyed by (src, dst, direction) and computes utilization = rate / cap."""
    cfg = load_config()
    collector = TelemetryCollector(cfg, run_id="test_util")
    collector.register_link(
        src_switch="s1",
        src_port=2,
        dst_switch="s2",
        dst_port=1,
        capacity_mbps=10.0,
        delay_ms=5.0,
    )

    # Prime baseline
    collector.record_port_stats_reply(
        switch="s1",
        port_no=2,
        tx_bytes=0,
        rx_bytes=0,
        tx_packets=0,
        rx_packets=0,
        tx_dropped=0,
        rx_dropped=0,
        tx_errors=0,
        rx_errors=0,
        timestamp_monotonic=10.0,
    )

    # 5.0 Mbps transmitted over 10.0 Mbps link -> utilization = 0.50
    collector.record_port_stats_reply(
        switch="s1",
        port_no=2,
        tx_bytes=1_250_000,  # 10 Megabits in 2.0 s = 5.0 Mbps
        rx_bytes=0,
        tx_packets=1000,
        rx_packets=0,
        tx_dropped=0,
        rx_dropped=0,
        tx_errors=0,
        rx_errors=0,
        timestamp_monotonic=12.0,
    )

    links = collector.update_link_telemetry()
    key = ("s1", "s2", "forward")
    assert key in links
    rec = links[key]

    assert rec.src_switch == "s1"
    assert rec.dst_switch == "s2"
    assert rec.direction == "forward"
    assert rec.capacity_mbps == 10.0
    assert rec.tx_rate_mbps == pytest.approx(5.0, rel=1e-2)
    assert rec.utilization == pytest.approx(0.50, rel=1e-2)
    assert not rec.is_congested


# --------------------------------------------------------------------------- #
# 4. Active Latency Probes & Frame Serialization
# --------------------------------------------------------------------------- #
def test_probe_serialization_and_parsing() -> None:
    """Probe frame builds with valid header and round-trips correctly."""
    t_send = 1234567890
    data = build_probe_packet(
        src_dpid=1,
        src_port=2,
        dst_dpid=2,
        dst_port=1,
        seq=42,
        src_mac="00:00:00:00:00:01",
        send_time_ns=t_send,
    )

    assert len(data) >= 60  # Min Ethernet frame length
    parsed = parse_probe_packet(data)
    assert parsed is not None
    src_dpid, src_p, dst_dpid, dst_p, seq, send_time = parsed

    assert src_dpid == 1
    assert src_p == 2
    assert dst_dpid == 2
    assert dst_p == 1
    assert seq == 42
    assert send_time == t_send


def test_probe_corrupt_data_returns_none() -> None:
    """Non-probe frames return None safely."""
    assert parse_probe_packet(b"") is None
    assert parse_probe_packet(b"\x00" * 30) is None


def test_probe_tracker_latency_and_loss() -> None:
    """LinkProbeTracker correctly measures latency and expires lost probes."""
    tracker = LinkProbeTracker(window_size=10, timeout_s=0.5)

    # Probe 1: successful, 10.0 ms
    t0_ns = 1_000_000_000
    tracker.record_sent(seq=1, send_time_ns=t0_ns)
    lat = tracker.record_received(seq=1, recv_time_ns=t0_ns + 10_000_000)
    assert lat == pytest.approx(10.0, rel=1e-3)
    assert tracker.mean_latency_ms == pytest.approx(10.0, rel=1e-3)
    assert tracker.loss_pct == 0.0

    # Probe 2: sent but times out (lost)
    t1_ns = 2_000_000_000
    tracker.record_sent(seq=2, send_time_ns=t1_ns)
    # Advance time past timeout (0.5 s = 500,000,000 ns)
    expired = tracker.check_timeouts(current_time_ns=t1_ns + 600_000_000)
    assert expired == 1
    assert tracker.loss_pct == pytest.approx(50.0, rel=1e-2)  # 1 lost out of 2 samples


# --------------------------------------------------------------------------- #
# 5. Composite Loss & Congestion Detection
# --------------------------------------------------------------------------- #
def test_composite_loss_and_congestion_trigger() -> None:
    """High port drops or latency trigger is_congested flag."""
    cfg = load_config()
    collector = TelemetryCollector(cfg, run_id="test_congestion")
    collector.register_link(
        src_switch="s1",
        src_port=2,
        dst_switch="s2",
        dst_port=1,
        capacity_mbps=10.0,
        delay_ms=5.0,
    )

    # Initial sample
    collector.record_port_stats_reply(
        switch="s1", port_no=2,
        tx_bytes=0, rx_bytes=0,
        tx_packets=0, rx_packets=0,
        tx_dropped=0, rx_dropped=0,
        tx_errors=0, rx_errors=0,
        timestamp_monotonic=1.0,
    )

    # Next sample: 9 Mbps (90% utilization) + 50 dropped packets
    collector.record_port_stats_reply(
        switch="s1", port_no=2,
        tx_bytes=2_250_000,  # 9 Mbps in 2.0 s
        rx_bytes=0,
        tx_packets=1000, rx_packets=0,
        tx_dropped=50, rx_dropped=0,
        tx_errors=0, rx_errors=0,
        timestamp_monotonic=3.0,
    )

    links = collector.update_link_telemetry()
    rec = links[("s1", "s2", "forward")]
    assert rec.utilization == pytest.approx(0.90, rel=1e-2)
    assert rec.loss_pct > 0.0
    assert rec.is_congested is True


# --------------------------------------------------------------------------- #
# 6. Staleness & Stall Detection
# --------------------------------------------------------------------------- #
def test_staleness_and_stall_timeout() -> None:
    """Samples arriving past stall threshold are marked stale and stall is detected."""
    cfg = load_config()
    collector = TelemetryCollector(cfg, run_id="test_stall")
    collector.stall_threshold_s = 6.0  # 3 * 2.0 s

    collector.record_port_stats_reply(
        switch="s1", port_no=2,
        tx_bytes=100, rx_bytes=0,
        tx_packets=1, rx_packets=0,
        tx_dropped=0, rx_dropped=0,
        tx_errors=0, rx_errors=0,
        timestamp_monotonic=10.0,
    )

    # Sample after 7.0 s (exceeds 6.0 s stall threshold)
    rec = collector.record_port_stats_reply(
        switch="s1", port_no=2,
        tx_bytes=200, rx_bytes=0,
        tx_packets=2, rx_packets=0,
        tx_dropped=0, rx_dropped=0,
        tx_errors=0, rx_errors=0,
        timestamp_monotonic=17.0,
    )

    assert rec is not None
    assert rec.is_stale is True


# --------------------------------------------------------------------------- #
# 7. CSV Export & Schema Validation
# --------------------------------------------------------------------------- #
def test_csv_export_format_and_schema(tmp_path: Any) -> None:
    """CSV output must include all specified fieldnames and valid records."""
    csv_file = tmp_path / "telemetry_test.csv"
    cfg = load_config()
    collector = TelemetryCollector(cfg, run_id="test_csv_run")
    collector.csv_path = str(csv_file)

    collector.register_link(
        src_switch="s1",
        src_port=2,
        dst_switch="s2",
        dst_port=1,
        capacity_mbps=10.0,
        delay_ms=5.0,
    )

    collector.record_port_stats_reply(
        switch="s1", port_no=2,
        tx_bytes=0, rx_bytes=0,
        tx_packets=0, rx_packets=0,
        tx_dropped=0, rx_dropped=0,
        tx_errors=0, rx_errors=0,
        timestamp_monotonic=1.0,
    )
    collector.record_port_stats_reply(
        switch="s1", port_no=2,
        tx_bytes=1_000_000, rx_bytes=0,
        tx_packets=800, rx_packets=0,
        tx_dropped=0, rx_dropped=0,
        tx_errors=0, rx_errors=0,
        timestamp_monotonic=3.0,
    )

    records = collector.write_csv_snapshot()
    assert len(records) >= 1
    assert os.path.exists(csv_file)

    with open(csv_file, "r") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == CSV_FIELDNAMES
        rows = list(reader)
        assert len(rows) >= 1
        first_row = rows[0]
        assert first_row["run_id"] == "test_csv_run"
        assert first_row["src_switch"] == "s1"
        assert first_row["dst_switch"] == "s2"
        assert first_row["direction"] == "forward"
        assert float(first_row["tx_rate_mbps"]) > 0.0


# --------------------------------------------------------------------------- #
# 8. OpenFlow Echo-RTT Latency Bias Compensation
# --------------------------------------------------------------------------- #
def test_echo_tracker_rtt_and_smoothing() -> None:
    """ControlChannelEchoTracker tracks request/reply RTT and smooths measurements."""
    tracker = ControlChannelEchoTracker(smoothing_alpha=0.5)

    # Initial request to dpid=1 at t=0 ms, reply at t=4.0 ms -> RTT = 4.0 ms
    tracker.record_echo_request(dpid=1, xid=101, send_time_ns=0)
    rtt1 = tracker.record_echo_reply(dpid=1, xid=101, recv_time_ns=int(4.0 * 1e6))
    assert rtt1 == pytest.approx(4.0, rel=1e-3)
    assert tracker.get_echo_rtt_ms(1) == pytest.approx(4.0, rel=1e-3)

    # Second request to dpid=1 at t=10 ms, reply at t=12.0 ms (instantaneous 2.0 ms)
    # Smoothed = 0.5 * 2.0 + 0.5 * 4.0 = 3.0 ms
    tracker.record_echo_request(dpid=1, xid=102, send_time_ns=int(10.0 * 1e6))
    rtt2 = tracker.record_echo_reply(dpid=1, xid=102, recv_time_ns=int(12.0 * 1e6))
    assert rtt2 == pytest.approx(3.0, rel=1e-3)
    assert tracker.get_echo_rtt_ms(1) == pytest.approx(3.0, rel=1e-3)


def test_link_control_overhead_calculation() -> None:
    """Control overhead across a link is average of src and dst switch echo RTTs."""
    tracker = ControlChannelEchoTracker()
    # Switch 1 echo RTT = 2.0 ms (one-way control latency = 1.0 ms)
    tracker.record_echo_request(dpid=1, xid=1, send_time_ns=0)
    tracker.record_echo_reply(dpid=1, xid=1, recv_time_ns=int(2.0 * 1e6))

    # Switch 2 echo RTT = 4.0 ms (one-way control latency = 2.0 ms)
    tracker.record_echo_request(dpid=2, xid=2, send_time_ns=0)
    tracker.record_echo_reply(dpid=2, xid=2, recv_time_ns=int(4.0 * 1e6))

    # Overhead for link 1 -> 2 is (2.0 + 4.0) / 2 = 3.0 ms
    overhead = tracker.get_link_control_overhead_ms(src_dpid=1, dst_dpid=2)
    assert overhead == pytest.approx(3.0, rel=1e-3)


def test_probe_latency_echo_compensation() -> None:
    """Active probe latency subtracts control overhead to isolate true data-link delay."""
    cfg = load_config()
    collector = TelemetryCollector(cfg, run_id="test_echo_comp")
    collector.register_link(src_switch="s1", src_port=2, dst_switch="s2", dst_port=1, delay_ms=5.0)

    # Establish echo RTTs: s1 (dpid=1) = 2.0 ms, s2 (dpid=2) = 4.0 ms -> overhead = 3.0 ms
    collector.record_echo_request(dpid=1, xid=1, send_time_ns=0)
    collector.record_echo_reply(dpid=1, xid=1, recv_time_ns=int(2.0 * 1e6))

    collector.record_echo_request(dpid=2, xid=2, send_time_ns=0)
    collector.record_echo_reply(dpid=2, xid=2, recv_time_ns=int(4.0 * 1e6))

    # Emit active probe with send_time = 100 ms
    t_send_ns = int(100.0 * 1e6)
    collector.record_probe_sent(src_switch="s1", dst_switch="s2", seq=1, send_time_ns=t_send_ns)

    # Probe returns at t=108.0 ms (raw trip time = 8.0 ms)
    t_recv_ns = int(108.0 * 1e6)
    compensated_lat = collector.record_probe_received(
        src_switch="s1",
        dst_switch="s2",
        seq=1,
        recv_time_ns=t_recv_ns,
    )

    # Raw trip (8.0 ms) - Control Overhead (3.0 ms) = True Data Link Latency (5.0 ms)
    assert compensated_lat == pytest.approx(5.0, rel=1e-3)
    tracker = collector.get_probe_tracker("s1", "s2")
    assert tracker.mean_latency_ms == pytest.approx(5.0, rel=1e-3)


def test_probe_latency_compensation_clamps_at_zero() -> None:
    """When raw probe time is less than estimated overhead, latency clamps at 0.0 ms."""
    tracker = LinkProbeTracker()
    tracker.record_sent(seq=1, send_time_ns=0)
    # Raw measured: 1.5 ms. Control overhead: 3.0 ms.
    lat = tracker.record_received(seq=1, recv_time_ns=int(1.5 * 1e6), control_overhead_ms=3.0)
    assert lat == 0.0


def test_verify_p5_live_dry_run_and_imports() -> None:
    """Verify that verify_p5_live.py dry-run passes and has no missing imports."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "scripts/verify_p5_live.py", "--dry-run"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert "Preflight passed successfully" in proc.stdout


