"""Traffic generation engine for SDN congestion modeling.

Implements the 5 traffic patterns specified in Phase 6:
1. ramp: Gradual staircase increase from base to peak load exceeding link capacity.
2. bursty_onoff: Alternating high-load burst phases and idle recovery phases.
3. poisson: Stochastic flow arrival processes with exponential inter-arrival times.
4. tcp_udp_mix: Heterogeneous traffic mixing unresponsive UDP and TCP backoff.
5. elephant_mice: Heavy sustained elephant flow combined with short-lived mice bursts.

Supports both:
- Physics-based realistic queue/telemetry simulation for reproducible dataset generation.
- Live iperf3 command generation for execution on Mininet host nodes.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from common.config import Config, load_config
from common.proc import venv_bin

PATTERNS = ["ramp", "bursty_onoff", "poisson", "tcp_udp_mix", "elephant_mice"]


@dataclass
class FlowSpec:
    """Specification of an active flow in an episode."""

    protocol: str  # "tcp" or "udp"
    src_host: str
    dst_host: str
    start_s: float
    duration_s: float
    rate_mbps: float
    port: int = 5201


class TrafficProfile:
    """Generates continuous offered load (Mbps) and packet rates over time for a pattern."""

    def __init__(
        self,
        pattern: str,
        config: Optional[Config] = None,
        seed: Optional[int] = None,
    ) -> None:
        if pattern not in PATTERNS:
            raise ValueError(f"Unknown traffic pattern: {pattern}. Valid: {PATTERNS}")
        self.pattern = pattern
        self.config = config if config is not None else load_config()
        self.seed = seed
        self.rng = random.Random(seed)

        self.duration_s = float(self.config.get("traffic.episode_duration_s", 120))
        self.base_rate = float(self.config.get("traffic.base_rate_mbps", 2.0))
        self.peak_rate = float(self.config.get("traffic.peak_rate_mbps", 12.0))
        self.ramp_step_s = float(self.config.get("traffic.ramp_step_s", 10.0))
        self.on_phase_s = float(self.config.get("traffic.on_phase_s", 30.0))
        self.off_phase_s = float(self.config.get("traffic.off_phase_s", 20.0))
        self.poisson_rate_hz = float(self.config.get("traffic.poisson_rate_hz", 20.0))
        self.udp_ratio = float(self.config.get("traffic.udp_ratio", 0.3))

    def offered_load_at(self, t: float) -> float:
        """Return the offered traffic rate in Mbps at time t (seconds)."""
        if t < 0 or t > self.duration_s:
            return 0.0

        if self.pattern == "ramp":
            # Staircase ramp: climbs in discrete steps every ramp_step_s
            cycle = self.ramp_step_s * 10.0
            t_cycle = t % cycle
            num_steps = 6
            step_idx = min(int(t_cycle / self.ramp_step_s), num_steps - 1)
            # Ramp up for first 6 steps, then ramp down
            if t_cycle <= (self.ramp_step_s * 5.0):
                fraction = step_idx / 5.0
            else:
                fraction = max(0.0, 1.0 - ((t_cycle - (self.ramp_step_s * 5.0)) / (self.ramp_step_s * 5.0)))
            noise = (self.rng.random() - 0.5) * 0.4
            rate = self.base_rate + fraction * (self.peak_rate - self.base_rate) + noise
            return max(0.2, rate)

        elif self.pattern == "bursty_onoff":
            cycle = self.on_phase_s + self.off_phase_s
            phase = t % cycle
            noise = (self.rng.random() - 0.5) * 0.5
            if phase < self.on_phase_s:
                # Active burst phase: near peak
                return max(0.5, self.peak_rate * 0.95 + noise)
            else:
                # Idle phase: near base
                return max(0.1, self.base_rate + noise)

        elif self.pattern == "poisson":
            # Flow count arrives via Poisson process
            # Average flow rate fluctuates with Poisson stochastic arrivals
            num_flows = max(1, int(self.rng.gauss(self.poisson_rate_hz / 3.0, 2.5)))
            rate_per_flow = 0.8
            rate = num_flows * rate_per_flow
            # Periodic bursts
            if int(t / 15.0) % 2 == 1:
                rate += (self.peak_rate - self.base_rate) * 0.6
            return max(0.5, min(self.peak_rate * 1.2, rate))

        elif self.pattern == "tcp_udp_mix":
            # Combined responsive TCP + unresponsive UDP load
            udp_load = self.peak_rate * self.udp_ratio
            # TCP oscillates due to congestion window dynamics
            tcp_cycle = 8.0
            tcp_phase = (t % tcp_cycle) / tcp_cycle
            tcp_load = self.base_rate + (self.peak_rate - self.base_rate) * (0.3 + 0.6 * math.sin(tcp_phase * math.pi))
            noise = (self.rng.random() - 0.5) * 0.3
            return max(0.5, udp_load + tcp_load + noise)

        elif self.pattern == "elephant_mice":
            # 1 continuous elephant flow (6.5 Mbps) + random bursts of mice flows
            elephant_load = 6.5
            # Mice flows burst randomly every few seconds
            mice_active = self.rng.random() < 0.45
            mice_load = (self.rng.randint(2, 5) * 1.5) if mice_active else 0.2
            return max(0.5, elephant_load + mice_load)

        return self.base_rate

    def generate_flow_plan(self) -> List[FlowSpec]:
        """Generate discrete iperf3 flows for live execution on Mininet hosts."""
        flows: List[FlowSpec] = []
        port = 5201

        if self.pattern == "ramp":
            cur_t = 0.0
            rate = self.base_rate
            while cur_t < self.duration_s:
                step_rate = min(self.peak_rate, self.base_rate + (cur_t / self.duration_s) * (self.peak_rate - self.base_rate))
                flows.append(FlowSpec("udp", "h1", "h4", cur_t, self.ramp_step_s, step_rate, port))
                cur_t += self.ramp_step_s

        elif self.pattern == "bursty_onoff":
            cur_t = 0.0
            cycle = self.on_phase_s + self.off_phase_s
            while cur_t < self.duration_s:
                dur_on = min(self.on_phase_s, self.duration_s - cur_t)
                flows.append(FlowSpec("udp", "h1", "h4", cur_t, dur_on, self.peak_rate, port))
                cur_t += self.on_phase_s
                if cur_t >= self.duration_s:
                    break
                dur_off = min(self.off_phase_s, self.duration_s - cur_t)
                flows.append(FlowSpec("udp", "h1", "h4", cur_t, dur_off, self.base_rate, port))
                cur_t += self.off_phase_s

        elif self.pattern == "elephant_mice":
            # Long sustained elephant flow
            flows.append(FlowSpec("tcp", "h1", "h4", 0.0, self.duration_s, 6.5, port))
            # Mice bursts
            for t in range(5, int(self.duration_s), 10):
                flows.append(FlowSpec("udp", "h1", "h4", float(t), 3.0, 5.0, port + 1))

        else:
            # Default / TCP UDP mix
            flows.append(FlowSpec("udp", "h1", "h4", 0.0, self.duration_s, self.base_rate * 2.0, port))
            flows.append(FlowSpec("tcp", "h1", "h4", 10.0, self.duration_s - 10.0, 5.0, port + 1))

        return flows


# --------------------------------------------------------------------------- #
# Unit-Test Fixture ONLY (Not for Training Data)
# --------------------------------------------------------------------------- #
class SimulatedLinkFixtureForUnitTests:
    """FOR UNIT TESTING ONLY: Synthesizes deterministic queue telemetry.

    Used exclusively by offline unit tests in tests/test_dataset_p6.py to verify
    future-congestion labeling and non-leakage invariants without requiring root
    privileges or live Mininet execution.

    TRAINING DATA MUST COME EXCLUSIVELY FROM LIVE RUNS (Mininet + Ryu + iperf3).
    """

    def __init__(
        self,
        capacity_mbps: float = 10.0,
        base_delay_ms: float = 5.0,
        buffer_packets: int = 100,
        packet_size_bytes: int = 1500,
    ) -> None:
        self.capacity_mbps = capacity_mbps
        self.base_delay_ms = base_delay_ms
        self.buffer_packets = buffer_packets
        self.packet_size_bytes = packet_size_bytes
        self.queue_packets: float = 0.0

    def step(self, offered_mbps: float, dt_s: float) -> Tuple[float, float, float, int]:
        """Advance simulated queue by dt_s seconds under offered load."""
        packet_bits = self.packet_size_bytes * 8.0
        arriving_packets = (offered_mbps * 1e6 * dt_s) / packet_bits
        service_capacity_packets = (self.capacity_mbps * 1e6 * dt_s) / packet_bits

        self.queue_packets += arriving_packets
        served_packets = min(self.queue_packets, service_capacity_packets)
        self.queue_packets -= served_packets

        dropped_packets = 0
        if self.queue_packets > self.buffer_packets:
            dropped_packets = int(self.queue_packets - self.buffer_packets)
            self.queue_packets = float(self.buffer_packets)

        tx_rate_mbps = (served_packets * packet_bits) / (dt_s * 1e6)
        queuing_delay_ms = (self.queue_packets * packet_bits / (self.capacity_mbps * 1e6)) * 1000.0
        total_latency_ms = self.base_delay_ms + queuing_delay_ms

        total_attempted = served_packets + dropped_packets
        loss_pct = (dropped_packets / total_attempted * 100.0) if total_attempted > 0 else 0.0

        return tx_rate_mbps, total_latency_ms, loss_pct, dropped_packets


# Alias for backwards compatibility in existing unit test imports
LinkSimulator = SimulatedLinkFixtureForUnitTests


@dataclass(frozen=True)
class ScheduledFlow:
    """One iperf3 flow: when it starts, which port, how fast, how long."""

    start_s: float
    port: int
    rate_mbps: float
    life_s: float


def ramp_plan(
    profile: Sequence[float],
    step_s: float,
    duration_s: float,
    unit_mbps: float,
) -> List["ScheduledFlow"]:
    """Turn a per-step rate profile into overlapping long-lived iperf3 flows.

    The problem this solves
    -----------------------
    The obvious implementation starts one iperf3 per step at that step's rate,
    sleeps, and repeats. On a real fabric that does not produce a ramp at all: it
    produces isolated bursts separated by idle gaps. Two reasons, both
    unavoidable:

    * Mininet's ``node.cmd()`` costs several seconds (it spawns a shell and a
      pty), so steps are skipped entirely -- a profile of 2,4,6,8,10,12 arrived
      at the switches as 2,6,10.
    * Each flow runs for its own ``-t`` and then exits, so if the next step's
      start is delayed even slightly there is a gap at zero.

    The aggregate of several concurrent flows, on the other hand, can hold any
    shape: adding a flow raises the rate, letting one expire lowers it, and
    nothing in between ever drops to zero.

    Algorithm
    ---------
    Split the profile into units of ``unit_mbps``. Unit ``i`` is started at the
    first step that needs more than ``i`` units and expires at the last step that
    still needs it, so the sum of live flows equals the requested rate
    throughout. Each unit therefore gets exactly one flow with a computed
    lifetime, and every flow is started once and never re-launched.

    Parameters
    ----------
    profile:
        Per-step offered rates in Mbps. Need not reach zero and need not be
        monotonic in either direction.
    step_s:
        Duration of one step.
    duration_s:
        Total run duration; the profile is cycled to fill it.
    unit_mbps:
        Granularity. Each flow carries this rate, so smaller units give a finer
        approximation of the profile.

    Returns
    -------
    list of ScheduledFlow
        One entry per flow, carrying its own lifetime.

    Notes
    -----
    Ports are handed out from 5201 upward. The caller must run one iperf3 server
    per port used, and must not reuse a port across flows -- a lingering flow on
    a port would silently add its rate to whatever else uses it.
    """
    n_steps = max(1, int(round(duration_s / step_s)))
    rates = [float(profile[k % len(profile)]) for k in range(n_steps)]
    needs = [max(1, int(round(r / unit_mbps))) for r in rates]

    # For each unit, the first and last step at which it is required.
    first_step: Dict[int, int] = {}
    last_step: Dict[int, int] = {}
    for step, count in enumerate(needs):
        for unit in range(count):
            first_step.setdefault(unit, step)
            last_step[unit] = step

    flows: List["ScheduledFlow"] = []
    for unit in sorted(first_step):
        start_s = first_step[unit] * step_s
        life_steps = last_step[unit] - first_step[unit] + 1
        life_s = min(life_steps * step_s, duration_s - start_s)
        if life_s <= 0:
            continue
        flows.append(ScheduledFlow(
            start_s=start_s, port=5201 + unit, rate_mbps=unit_mbps,
            life_s=life_s,
        ))
    return flows


def run_profiled_load_on_hosts(
    net: Any,
    profile: Sequence[float],
    duration_s: float,
    step_s: float,
    unit_mbps: float = 2.0,
) -> List[Dict[str, Any]]:
    """Offer a continuous, step-varying load as overlapping iperf3 flows.

    Returns a small schedule describing what was launched, so a caller (or an
    acceptance test) can assert the *intended* shape even before any telemetry
    arrives.
    """
    h1 = net.get("h1")
    h4 = net.get("h4")
    iperf = venv_bin("iperf3")

    flows = ramp_plan(profile, step_s, duration_s, unit_mbps)
    ports = sorted({flow.port for flow in flows})

    h4.cmd(f"killall -9 {iperf} 2>/dev/null; true")
    for port in ports:
        h4.cmd(f"{iperf} -s -D -p {port}")
    h1.cmd(f"killall -9 {iperf} 2>/dev/null; true")
    time.sleep(0.5)

    scheduled: List[Dict[str, Any]] = []
    started = 0.0
    for flow in sorted(flows, key=lambda f: f.start_s):
        delay = flow.start_s - started
        if delay > 0:
            time.sleep(delay)
        started = flow.start_s
        h1.cmd(
            f"{iperf} -u -c 10.0.0.4 -b {flow.rate_mbps}M "
            f"-t {max(1, int(round(flow.life_s)))} "
            f"-p {flow.port} >/dev/null 2>&1 &"
        )
        scheduled.append(asdict(flow))
    # Absorb the tail of the run so the caller does not have to.
    tail = duration_s - started
    if tail > 0:
        time.sleep(tail)
    return scheduled


def run_live_pattern_on_hosts(
    net: Any,
    pattern: str,
    duration_s: float,
    seed: int,
    base_rate_mbps: float = 2.0,
    peak_rate_mbps: float = 12.0,
) -> None:
    """Execute live iperf3 flows on Mininet hosts h1 -> h4 matching pattern."""
    h1 = net.get("h1")
    h4 = net.get("h4")

    # Start iperf3 servers on h4 (ports 5201 and 5202)
    iperf = venv_bin("iperf3")
    h4.cmd(f"killall -9 {iperf} 2>/dev/null; true")
    time_sleep = time.sleep
    h4.cmd(f"{iperf} -s -D -p 5201")
    h4.cmd(f"{iperf} -s -D -p 5202")
    time_sleep(0.5)

    rng = random.Random(seed)

    if pattern == "ramp":
        # A true monotonic ramp, delivered as overlapping long-lived flows.
        #
        # The previous version started one iperf3 per step at that step's rate.
        # On a real fabric that is not a ramp: Mininet's node.cmd() costs several
        # seconds, so whole steps were skipped (a profile of 2,4,6,8,10,12
        # arrived at the switches as 2,6,10) and each flow exited before the
        # next began, leaving idle gaps. Measured link rate oscillated between
        # zero and a handful of discrete levels.
        #
        # That matters beyond aesthetics: with load stepping from idle to
        # saturated inside one 2 s sample there is nothing to predict, so the
        # congestion model could only ever react. The aggregate of concurrent
        # flows holds any shape and never drops to zero.
        ramp_profile = [2.0, 4.0, 6.0, 8.0, 10.0, peak_rate_mbps, 10.0, 8.0,
                        6.0, 4.0, 2.0]
        run_profiled_load_on_hosts(
            net, ramp_profile, duration_s=duration_s, step_s=10.0,
            unit_mbps=2.0,
        )
        return

    elif pattern == "bursty_onoff":
        # Alternating 30s on at 12M, 20s off at 2M
        elapsed = 0.0
        while elapsed < duration_s:
            on_dur = min(30.0, duration_s - elapsed)
            h1.cmd(f"{iperf} -u -c 10.0.0.4 -b {peak_rate_mbps}M -t {int(on_dur)} -p 5201 >/dev/null 2>&1 &")
            time_sleep(on_dur)
            elapsed += on_dur
            if elapsed >= duration_s:
                break
            off_dur = min(20.0, duration_s - elapsed)
            h1.cmd(f"{iperf} -u -c 10.0.0.4 -b {base_rate_mbps}M -t {int(off_dur)} -p 5201 >/dev/null 2>&1 &")
            time_sleep(off_dur)
            elapsed += off_dur

    elif pattern == "poisson":
        # Stochastic arrivals rendered as a continuous load. The previous
        # per-step implementation produced the same square wave as `ramp`:
        # discrete bursts separated by idle gaps, rather than the fluctuating
        # background load the pattern is meant to represent.
        # Generated here rather than via TrafficProfile so the live path and the
        # offline planner cannot drift apart: both read the same PRNG stream.
        poisson_rng = random.Random(seed)
        steps = max(2, int(round(duration_s / 5.0)))
        poisson_profile: List[float] = []
        for _ in range(steps):
            # A background load that fluctuates, occasionally spiking. Every
            # step is non-zero, which is the whole point: an idle gap is not
            # "stochastic arrival", it is a bug in the load generator.
            rate = poisson_rng.uniform(base_rate_mbps, peak_rate_mbps * 0.6)
            if poisson_rng.random() < 0.25:
                rate = poisson_rng.uniform(peak_rate_mbps * 0.6, peak_rate_mbps)
            poisson_profile.append(round(max(base_rate_mbps, rate), 1))
        run_profiled_load_on_hosts(
            net, poisson_profile, duration_s=duration_s, step_s=5.0,
            unit_mbps=1.0,
        )
        return

    elif pattern == "_legacy_poisson":
        # Stochastic arrivals with periodic bursts
        elapsed = 0.0
        while elapsed < duration_s:
            burst_rate = peak_rate_mbps if rng.random() < 0.5 else base_rate_mbps * 2.0
            dur = min(5.0 + rng.random() * 5.0, duration_s - elapsed)
            h1.cmd(f"{iperf} -u -c 10.0.0.4 -b {burst_rate:.1f}M -t {int(dur)} -p 5201 >/dev/null 2>&1 &")
            time_sleep(dur)
            elapsed += dur

    elif pattern == "tcp_udp_mix":
        # Parallel UDP (3 Mbps) + TCP flow over the full duration
        h1.cmd(f"{iperf} -u -c 10.0.0.4 -b 3.0M -t {int(duration_s)} -p 5201 >/dev/null 2>&1 &")
        h1.cmd(f"{iperf} -c 10.0.0.4 -t {int(duration_s)} -p 5202 >/dev/null 2>&1 &")
        time_sleep(duration_s)

    elif pattern == "elephant_mice":
        # Elephant flow (TCP) + periodic mice UDP bursts
        h1.cmd(f"{iperf} -c 10.0.0.4 -b 6.5M -t {int(duration_s)} -p 5201 >/dev/null 2>&1 &")
        elapsed = 0.0
        while elapsed < duration_s:
            time_sleep(min(8.0, duration_s - elapsed))
            elapsed += 8.0
            if elapsed < duration_s:
                mice_dur = min(3.0, duration_s - elapsed)
                h1.cmd(f"{iperf} -u -c 10.0.0.4 -b 5.0M -t {int(mice_dur)} -p 5202 >/dev/null 2>&1 &")
                time_sleep(mice_dur)
                elapsed += mice_dur

    h4.cmd(f"killall -9 {iperf} 2>/dev/null; true")
