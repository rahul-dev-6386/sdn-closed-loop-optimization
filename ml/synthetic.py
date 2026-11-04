"""Physics-informed synthetic telemetry and future-congestion dataset generator.

Enables hermetic data generation, data augmentation, and cross-topology training
without requiring Mininet, Ryu, or root permissions.

Features:
1. Physical queue buffer dynamics (M/M/1 / finite buffer drop modeling).
2. 8 distinct traffic load profiles:
   - ramp: linear ramp past capacity (replicates iperf3 ramp)
   - bursty_onoff: alternating high-intensity bursts and idle periods
   - poisson: stochastic packet arrivals with time-varying intensity
   - tcp_udp_mix: TCP AIMD sawteeth with competing UDP flood
   - elephant_mice: sporadic massive elephant flows amidst background traffic
   - microburst: short sharp spikes testing fast response
   - oscillating: sinusoidal diurnal / load wave
   - step_stress: sudden step surge to 135% link capacity
3. Exact future-horizon labeling:
   - label_future_congested: 1 if link congests within [t + gap, t + gap + horizon]
   - lead_time_s: seconds before congestion onset
   - horizon_valid: false for tail samples where future is unobservable
4. Full CSV schema parity with live collector output (P6/P7 compliant).
"""

from __future__ import annotations

import csv
import math
import os
import random
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import Config, load_config
from controller.telemetry import CSV_FIELDNAMES

SYNTHETIC_FIELDNAMES = CSV_FIELDNAMES + [
    "pattern",
    "seed",
    "label_future_congested",
    "lead_time_s",
    "horizon_valid",
]

DEFAULT_PATTERNS = [
    "ramp",
    "bursty_onoff",
    "poisson",
    "tcp_udp_mix",
    "elephant_mice",
    "microburst",
    "oscillating",
    "step_stress",
]


@dataclass(frozen=True)
class LinkSpec:
    src: str
    dst: str
    capacity_mbps: float = 10.0
    base_delay_ms: float = 5.0
    max_queue_pkts: int = 100
    src_port: int = 1
    dst_port: int = 1


DEFAULT_DIAMOND_LINKS = [
    LinkSpec("s1", "s2", capacity_mbps=10.0, base_delay_ms=5.0, src_port=1, dst_port=1),
    LinkSpec("s1", "s3", capacity_mbps=10.0, base_delay_ms=5.0, src_port=2, dst_port=1),
    LinkSpec("s2", "s4", capacity_mbps=10.0, base_delay_ms=5.0, src_port=2, dst_port=1),
    LinkSpec("s3", "s4", capacity_mbps=10.0, base_delay_ms=5.0, src_port=2, dst_port=2),
]


class LinkQueueSimulator:
    """Simulates physical queue buildup, packet loss, and latency for a link."""

    def __init__(self, spec: LinkSpec, mtu_bytes: int = 1500) -> None:
        self.spec = spec
        self.mtu_bytes = mtu_bytes
        self.max_queue_bytes = spec.max_queue_pkts * mtu_bytes
        self.queue_bytes = 0.0

        # Cumulative counters
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.tx_packets = 0
        self.rx_packets = 0
        self.tx_dropped = 0
        self.rx_errors = 0

    def step(
        self, offered_rate_mbps: float, dt_s: float
    ) -> Dict[str, Any]:
        """Step queue forward by dt_s with offered traffic rate."""
        capacity_bytes_per_s = (self.spec.capacity_mbps * 1e6) / 8.0
        service_bytes = capacity_bytes_per_s * dt_s

        offered_bytes = (offered_rate_mbps * 1e6 / 8.0) * dt_s
        offered_pkts = int(math.ceil(offered_bytes / self.mtu_bytes))

        # Queue buildup
        new_queue = self.queue_bytes + offered_bytes - service_bytes
        dropped_bytes = 0.0
        if new_queue > self.max_queue_bytes:
            dropped_bytes = new_queue - self.max_queue_bytes
            self.queue_bytes = self.max_queue_bytes
        else:
            self.queue_bytes = max(0.0, new_queue)

        dropped_pkts = int(math.floor(dropped_bytes / self.mtu_bytes))
        transmitted_bytes = max(0.0, offered_bytes - dropped_bytes)
        transmitted_pkts = max(0, offered_pkts - dropped_pkts)

        # Update cumulative counters
        self.tx_bytes += int(offered_bytes)
        self.rx_bytes += int(transmitted_bytes)
        self.tx_packets += offered_pkts
        self.rx_packets += transmitted_pkts
        self.tx_dropped += dropped_pkts

        # Utilization & rates
        tx_rate_mbps = round((offered_bytes * 8.0) / (dt_s * 1e6), 4)
        rx_rate_mbps = round((transmitted_bytes * 8.0) / (dt_s * 1e6), 4)
        utilization = min(1.0, round(tx_rate_mbps / self.spec.capacity_mbps, 4))

        # Latency = base propagation delay + queuing delay + minor jitter
        queuing_delay_s = self.queue_bytes / capacity_bytes_per_s
        queuing_delay_ms = queuing_delay_s * 1000.0
        jitter_ms = random.uniform(-0.4, 0.4)
        latency_ms = max(1.0, round(self.spec.base_delay_ms + queuing_delay_ms + jitter_ms, 3))

        # Loss percentage
        loss_pct = 0.0
        if offered_pkts > 0:
            loss_pct = round((dropped_pkts / offered_pkts) * 100.0, 3)

        return {
            "tx_rate_mbps": tx_rate_mbps,
            "rx_rate_mbps": rx_rate_mbps,
            "tx_packet_rate_pps": round(offered_pkts / dt_s, 2),
            "rx_packet_rate_pps": round(transmitted_pkts / dt_s, 2),
            "utilization": utilization,
            "latency_ms": latency_ms,
            "loss_pct": loss_pct,
            "tx_bytes": self.tx_bytes,
            "rx_bytes": self.rx_bytes,
            "tx_packets": self.tx_packets,
            "rx_packets": self.rx_packets,
            "tx_dropped": self.tx_dropped,
            "rx_errors": self.rx_errors,
        }


def generate_offered_rate(
    pattern: str,
    t: float,
    duration_s: float,
    capacity_mbps: float,
    seed: int,
    is_bottleneck: bool = True,
) -> float:
    """Generate synthetic offered traffic rate in Mbps for a given pattern and time."""
    random.seed(seed + int(t * 10))

    if not is_bottleneck:
        # Cross-traffic / background traffic on non-bottleneck links
        base = capacity_mbps * 0.15
        return max(0.05, base + random.uniform(-0.1, 0.2))

    cap = capacity_mbps

    if pattern == "ramp":
        # Linear ramp from 0.20 C to 1.35 C starting at 20% duration
        ramp_start = duration_s * 0.20
        ramp_end = duration_s * 0.70
        if t < ramp_start:
            rate = cap * 0.25
        elif t <= ramp_end:
            progress = (t - ramp_start) / (ramp_end - ramp_start)
            rate = cap * (0.25 + progress * 1.15)
        else:
            rate = cap * 1.40
        return rate + random.uniform(-0.15, 0.15)

    elif pattern == "bursty_onoff":
        # Alternating ON/OFF periods with Poisson cycle
        period = 16.0
        phase = (t % period) / period
        if phase > 0.45:
            return cap * 1.30 + random.uniform(-0.2, 0.2)
        return cap * 0.20 + random.uniform(-0.05, 0.05)

    elif pattern == "poisson":
        # Stochastic arrivals with drifting intensity
        intensity = 0.35 + 0.95 * (0.5 + 0.5 * math.sin(2 * math.pi * t / (duration_s * 0.6)))
        return max(0.1, cap * intensity + random.gauss(0, 0.15 * cap))

    elif pattern == "tcp_udp_mix":
        # TCP AIMD sawteeth + steady UDP flood
        udp_flood = cap * 0.55 if t > duration_s * 0.25 else 0.1
        sawtooth = (t % 12.0) / 12.0 * (cap * 0.8)
        return udp_flood + sawtooth + random.uniform(-0.1, 0.1)

    elif pattern == "elephant_mice":
        # Background mice flows + sporadic elephant burst
        base = cap * 0.18 + random.uniform(-0.05, 0.05)
        # Elephant flows around t in [25, 45] and [65, 80]
        if (25.0 <= t <= 45.0) or (65.0 <= t <= 80.0):
            return base + cap * 1.15
        return base

    elif pattern == "microburst":
        # High-frequency short-lived spikes (every 14s for 3s)
        cycle = t % 14.0
        if cycle < 3.2 and t > 10.0:
            return cap * 1.45 + random.uniform(-0.1, 0.1)
        return cap * 0.30 + random.uniform(-0.05, 0.05)

    elif pattern == "oscillating":
        # Smooth sinusoidal wave oscillating between 0.30 C and 1.30 C
        wave = 0.5 + 0.5 * math.sin(2 * math.pi * t / 28.0)
        return cap * (0.30 + wave * 1.0) + random.uniform(-0.1, 0.1)

    elif pattern == "step_stress":
        # Flat baseline then sudden harsh step jump to 135% at 35% duration
        if t >= duration_s * 0.35:
            return cap * 1.35 + random.uniform(-0.15, 0.15)
        return cap * 0.22 + random.uniform(-0.05, 0.05)

    # Fallback
    return cap * 0.5


class SyntheticTelemetryGenerator:
    """Generates synthetic telemetry CSV datasets with ground-truth future labeling."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config if config is not None else load_config()
        self.sample_interval_s = float(self.config.get("telemetry.sample_interval_s", 2.0))
        self.horizon_n_s = float(self.config.get("ml.horizon_n_s", 10.0))
        self.label_gap_s = float(self.config.get("ml.label_gap_s", 2.0))
        self.cong_util = float(self.config.get("telemetry.congestion_utilization_threshold", 0.80))
        self.cong_loss = float(self.config.get("telemetry.congestion_loss_threshold_pct", 2.0))
        self.cong_latency = float(self.config.get("telemetry.congestion_latency_threshold_ms", 40.0))

    def generate_episode(
        self,
        pattern: str,
        seed: int,
        duration_s: float = 90.0,
        links: Optional[List[LinkSpec]] = None,
        base_timestamp: float = 1700000000.0,
    ) -> List[Dict[str, Any]]:
        """Generate a single time-series episode for all links in a network."""
        links = links or DEFAULT_DIAMOND_LINKS
        run_id = f"syn_{pattern}_s{seed}"
        steps = int(math.ceil(duration_s / self.sample_interval_s))

        simulators = {link: LinkQueueSimulator(link) for link in links}
        raw_rows: List[Dict[str, Any]] = []

        for step_idx in range(steps):
            t_rel = step_idx * self.sample_interval_s
            ts = base_timestamp + t_rel

            for link in links:
                sim = simulators[link]
                # Primary bottleneck link is s1-s2
                is_bottleneck = (link.src == "s1" and link.dst == "s2")

                offered = generate_offered_rate(
                    pattern=pattern,
                    t=t_rel,
                    duration_s=duration_s,
                    capacity_mbps=link.capacity_mbps,
                    seed=seed + step_idx,
                    is_bottleneck=is_bottleneck,
                )

                metrics = sim.step(offered_rate_mbps=offered, dt_s=self.sample_interval_s)

                is_congested = (
                    metrics["utilization"] >= self.cong_util
                    or metrics["loss_pct"] >= self.cong_loss
                    or metrics["latency_ms"] >= self.cong_latency
                )

                # Both forward and reverse records for symmetry
                row = {
                    "run_id": run_id,
                    "timestamp": round(ts, 4),
                    "src_switch": link.src,
                    "dst_switch": link.dst,
                    "direction": "forward",
                    "src_port": link.src_port,
                    "dst_port": link.dst_port,
                    "capacity_mbps": link.capacity_mbps,
                    "tx_rate_mbps": metrics["tx_rate_mbps"],
                    "rx_rate_mbps": metrics["rx_rate_mbps"],
                    "tx_packet_rate_pps": metrics["tx_packet_rate_pps"],
                    "rx_packet_rate_pps": metrics["rx_packet_rate_pps"],
                    "utilization": metrics["utilization"],
                    "latency_ms": metrics["latency_ms"],
                    "loss_pct": metrics["loss_pct"],
                    "tx_bytes": metrics["tx_bytes"],
                    "rx_bytes": metrics["rx_bytes"],
                    "tx_packets": metrics["tx_packets"],
                    "rx_packets": metrics["rx_packets"],
                    "tx_dropped": metrics["tx_dropped"],
                    "rx_errors": metrics["rx_errors"],
                    "probe_loss_pct": metrics["loss_pct"],
                    "is_stale": False,
                    "is_congested": is_congested,
                    "p_congestion": "",
                    "predicted_congested": False,
                    "pattern": pattern,
                    "seed": seed,
                }
                raw_rows.append(row)

        # Apply strict future horizon labeling
        return self._label_episode_rows(raw_rows, duration_s)

    def _label_episode_rows(
        self, raw_rows: List[Dict[str, Any]], duration_s: float
    ) -> List[Dict[str, Any]]:
        """Label rows with future congestion according to strict P6/P7 horizon."""
        # Group by link
        groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        for r in raw_rows:
            key = (r["src_switch"], r["dst_switch"], r["direction"])
            groups.setdefault(key, []).append(r)

        labeled: List[Dict[str, Any]] = []

        for _, rows in groups.items():
            rows.sort(key=lambda r: float(r["timestamp"]))
            n = len(rows)
            max_t = float(rows[-1]["timestamp"])

            for i, curr in enumerate(rows):
                t_curr = float(curr["timestamp"])
                t_horizon_start = t_curr + self.label_gap_s
                t_horizon_end = t_horizon_start + self.horizon_n_s

                # Check if full horizon can be observed
                horizon_valid = t_horizon_end <= (max_t + self.sample_interval_s * 0.5)

                future_congested = False
                lead_time_s = -1.0

                for j in range(i + 1, n):
                    t_future = float(rows[j]["timestamp"])
                    if t_future < t_horizon_start:
                        continue
                    if t_future > t_horizon_end:
                        break
                    if rows[j]["is_congested"]:
                        future_congested = True
                        if lead_time_s < 0:
                            lead_time_s = round(t_future - t_curr, 2)

                curr["label_future_congested"] = 1 if future_congested else 0
                curr["lead_time_s"] = lead_time_s
                curr["horizon_valid"] = horizon_valid
                labeled.append(curr)

        # Restore chronological order across all links
        labeled.sort(key=lambda r: float(r["timestamp"]))
        return labeled

    def generate_dataset(
        self,
        output_csv_path: str,
        patterns: Optional[List[str]] = None,
        seeds_per_pattern: int = 5,
        duration_s: float = 90.0,
    ) -> Dict[str, Any]:
        """Generate a full labeled dataset across multiple patterns and seeds."""
        patterns = patterns or DEFAULT_PATTERNS
        os.makedirs(os.path.dirname(os.path.abspath(output_csv_path)), exist_ok=True)

        all_rows: List[Dict[str, Any]] = []
        base_ts = 1710000000.0

        for p_idx, pattern in enumerate(patterns):
            for s_idx in range(seeds_per_pattern):
                seed = 100 + p_idx * 50 + s_idx
                # Stagger timestamps so episodes don't overlap
                episode_ts = base_ts + (p_idx * seeds_per_pattern + s_idx) * (duration_s + 60.0)
                rows = self.generate_episode(
                    pattern=pattern,
                    seed=seed,
                    duration_s=duration_s,
                    base_timestamp=episode_ts,
                )
                all_rows.extend(rows)

        with open(output_csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=SYNTHETIC_FIELDNAMES)
            writer.writeheader()
            for r in all_rows:
                writer.writerow(r)

        congested_count = sum(1 for r in all_rows if r["label_future_congested"] == 1 and r["horizon_valid"])
        valid_count = sum(1 for r in all_rows if r["horizon_valid"])

        summary = {
            "total_rows": len(all_rows),
            "valid_rows": valid_count,
            "future_congested_rows": congested_count,
            "future_congested_pct": round((congested_count / max(1, valid_count)) * 100.0, 2),
            "patterns": patterns,
            "seeds_per_pattern": seeds_per_pattern,
            "output_csv": output_csv_path,
        }
        return summary
