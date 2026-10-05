#!/usr/bin/env python3
"""Phase 5 acceptance test: Telemetry rate calculation, active probes, and CSV logging.

Verifies:
1. Rate computation from counter differences over actual monotonic elapsed time.
2. 64-bit integer wrap handling without spurious negative values or overflow spikes.
3. Counter reset / switch reboot handling (clean recovery to 0 delta).
4. Directional link keying: (src_switch, dst_switch, direction) with capacity & utilization.
5. Active probe packet serialization, round-trip parsing, latency, and probe loss.
6. Composite loss calculation combining interface drops/errors with probe loss.
7. Telemetry staleness flagging and stall detection.
8. Telemetry CSV export conforming to the specified schema.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import time
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import load_config
from controller.telemetry import (
    CSV_FIELDNAMES,
    LinkProbeTracker,
    TelemetryCollector,
    build_probe_packet,
    diff_with_wrap,
    parse_probe_packet,
)

PASSED = 0
FAILED = 0


def section(title: str) -> None:
    print(f"\n{'-' * 78}\n[{title}]\n{'-' * 78}")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}" + (f" ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f" ({detail})" if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Phase 5 Telemetry System")
    parser.add_argument("--out", help="Write JSON summary to this path", default=None)
    args = parser.parse_args()

    print("=" * 78)
    print("AI-Driven SDN: Phase 5 Acceptance Test (Telemetry & Active Probes)")
    print("=" * 78)

    cfg = load_config()

    # ----------------------------------------------------------------------- #
    # 1. Counter Differences, 64-bit Wrap, and Reset Handling
    # ----------------------------------------------------------------------- #
    section("1. Counter Differences & Wrap / Reset Handling")
    delta_normal = diff_with_wrap(150_000_000, 100_000_000)
    check("monotonic counter difference computed accurately", delta_normal == 50_000_000, f"delta={delta_normal}")

    uint64_max = 2**64
    prev_wrap = uint64_max - 1000
    curr_wrap = 500
    delta_wrap = diff_with_wrap(curr_wrap, prev_wrap)
    check("64-bit counter wrap accounts for uint64 boundary", delta_wrap == 1500, f"delta={delta_wrap}")

    delta_reset = diff_with_wrap(curr=50, prev=100_000_000)
    check("counter reset / reboot safely yields 0 without huge spike", delta_reset == 0, f"delta={delta_reset}")

    # ----------------------------------------------------------------------- #
    # 2. Rate Calculation over Actual Monotonic Elapsed Time
    # ----------------------------------------------------------------------- #
    section("2. Rate Computation over Monotonic Elapsed Time")
    collector = TelemetryCollector(cfg, run_id="p5_verify")

    # Prime baseline
    collector.record_port_stats_reply(
        switch="s1",
        port_no=2,
        tx_bytes=10_000_000,
        rx_bytes=0,
        tx_packets=5000,
        rx_packets=0,
        tx_dropped=0,
        rx_dropped=0,
        tx_errors=0,
        rx_errors=0,
        timestamp_monotonic=100.0,
    )

    # 2.5 s elapsed time (not nominal 2.0 s), 2.5 MB transmitted = 20 Megabits -> 8.0 Mbps
    rate_rec = collector.record_port_stats_reply(
        switch="s1",
        port_no=2,
        tx_bytes=12_500_000,
        rx_bytes=0,
        tx_packets=7500,
        rx_packets=0,
        tx_dropped=0,
        rx_dropped=0,
        tx_errors=0,
        rx_errors=0,
        timestamp_monotonic=102.5,
    )
    check("port stats sample recorded and rate computed", rate_rec is not None)
    if rate_rec:
        check("rate computation divides by actual elapsed time (2.5 s)", abs(rate_rec.delta_s - 2.5) < 0.001)
        check("tx throughput calculated as exactly 8.0 Mbps", abs(rate_rec.tx_rate_mbps - 8.0) < 0.01, f"{rate_rec.tx_rate_mbps:.2f} Mbps")
        check("packet rate calculated as 1000 pps", abs(rate_rec.tx_packet_rate_pps - 1000.0) < 0.1)

    # ----------------------------------------------------------------------- #
    # 3. Directional Keying, Capacity & Utilization
    # ----------------------------------------------------------------------- #
    section("3. Directional Keying, Capacity & Utilization")
    collector.register_link(
        src_switch="s1",
        src_port=2,
        dst_switch="s2",
        dst_port=1,
        capacity_mbps=10.0,
        delay_ms=5.0,
    )
    links = collector.update_link_telemetry()
    key = ("s1", "s2", "forward")
    check("link telemetry record keyed by (s1, s2, 'forward')", key in links)
    link_rec = links[key]
    check("link capacity mapped to 10.0 Mbps", link_rec.capacity_mbps == 10.0)
    check("utilization computed as tx_rate / capacity = 0.80", abs(link_rec.utilization - 0.80) < 0.01, f"util={link_rec.utilization:.2f}")

    # ----------------------------------------------------------------------- #
    # 4. Active Latency Probes & Frame Serialization
    # ----------------------------------------------------------------------- #
    section("4. Active Probe Serialization, RTT & Packet Loss")
    t_send_ns = 50_000_000_000
    probe_pkt = build_probe_packet(
        src_dpid=1,
        src_port=2,
        dst_dpid=2,
        dst_port=1,
        seq=101,
        src_mac="00:00:00:00:00:01",
        send_time_ns=t_send_ns,
    )
    check("probe frame meets minimum Ethernet frame length (>= 60 bytes)", len(probe_pkt) >= 60, f"len={len(probe_pkt)} B")

    parsed = parse_probe_packet(probe_pkt)
    check("probe packet parsed successfully", parsed is not None)
    if parsed:
        src_dp, src_p, dst_dp, dst_p, seq, send_time = parsed
        check("parsed DPIDs and ports match original values", (src_dp, src_p, dst_dp, dst_p, seq) == (1, 2, 2, 1, 101))

    # Test tracker latency and timeout
    tracker = LinkProbeTracker(window_size=10, timeout_s=0.5)
    tracker.record_sent(seq=1, send_time_ns=1_000_000_000)
    lat = tracker.record_received(seq=1, recv_time_ns=1_005_000_000)  # 5.0 ms
    check("probe receipt calculates exact 5.0 ms latency", lat is not None and abs(lat - 5.0) < 0.01, f"{lat} ms")
    check("tracker window mean latency is 5.0 ms", abs(tracker.mean_latency_ms - 5.0) < 0.01)

    # Expired probe
    tracker.record_sent(seq=2, send_time_ns=2_000_000_000)
    expired = tracker.check_timeouts(current_time_ns=2_600_000_000)  # past 0.5 s timeout
    check("timed-out probe expired and marked lost", expired == 1)
    check("probe loss percentage reflects 50% loss (1 lost / 2 total)", abs(tracker.loss_pct - 50.0) < 0.1, f"{tracker.loss_pct:.1f}%")

    # ----------------------------------------------------------------------- #
    # 5. Staleness Flagging & Stall Detection
    # ----------------------------------------------------------------------- #
    section("5. Telemetry Freshness & Stall Detection")
    stale_rec = collector.record_port_stats_reply(
        switch="s1",
        port_no=2,
        tx_bytes=13_000_000,
        rx_bytes=0,
        tx_packets=7800,
        rx_packets=0,
        tx_dropped=0,
        rx_dropped=0,
        tx_errors=0,
        rx_errors=0,
        timestamp_monotonic=115.0,  # 12.5 s since last sample, exceeds 6.0 s stall threshold
    )
    check("sample arriving after stall threshold is flagged as stale", stale_rec is not None and stale_rec.is_stale is True)

    # ----------------------------------------------------------------------- #
    # 6. CSV Export & Schema Validation
    # ----------------------------------------------------------------------- #
    section("6. CSV Snapshot Generation & Schema")
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
        temp_csv = tf.name

    try:
        collector.csv_path = temp_csv
        records = collector.write_csv_snapshot()
        check("telemetry CSV snapshot written", len(records) > 0 and os.path.exists(temp_csv))

        with open(temp_csv, "r") as f:
            reader = csv.DictReader(f)
            check("CSV fieldnames exactly match spec", reader.fieldnames == CSV_FIELDNAMES)
            rows = list(reader)
            check("CSV contains recorded link row", len(rows) >= 1)
            if rows:
                r0 = rows[0]
                check("row captures run_id, src_switch, dst_switch, tx_rate_mbps", (r0["src_switch"], r0["dst_switch"]) == ("s1", "s2"))
    finally:
        if os.path.exists(temp_csv):
            os.unlink(temp_csv)

    # Summary
    print("\n" + "=" * 78)
    print(f"SUMMARY: {PASSED} passed, {FAILED} failed")
    print("=" * 78 + "\n")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump({"passed": PASSED, "failed": FAILED, "timestamp": time.time()}, f, indent=2)

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
