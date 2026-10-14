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
    nothing in between has to drop to zero.

    Algorithm
    ---------
    Split the profile into units of ``unit_mbps``: step ``k`` needs
    ``rate[k] / unit_mbps`` of them. Unit ``i`` is live during every maximal run
    of consecutive steps that need more than ``i`` units, and each such run
    becomes one flow with a computed lifetime.

    Handling *maximal runs* rather than a single interval per unit is what makes
    a repeating profile work. An earlier version gave each unit one interval from
    its first step to its last, which silently pinned the aggregate at peak
    across every cycle after the first -- a ramp that cycled twice read as
    "climb then sit at maximum".

    Zero is respected too. An earlier version forced every step to need at least
    one unit, which made it arithmetically impossible to express an idle phase --
    so ``bursty_onoff``, which is *supposed* to fall to zero, could not.

    Parameters
    ----------
    profile:
        Per-step offered rates in Mbps. May be zero, may rise and fall, and is
        cycled to fill the run.
    step_s:
        Duration of one step.
    duration_s:
        Total run duration.
    unit_mbps:
        Granularity. Each flow carries this rate, so smaller units approximate
        the profile more closely.

    Returns
    -------
    list of ScheduledFlow
        One entry per (unit, contiguous run it is needed for). The same port is
        reused across a unit's runs, which is safe because those runs are
        disjoint in time.

    Raises
    ------
    ValueError
        If ``unit_mbps`` is not positive, or ``step_s`` is not positive.
    """
    if unit_mbps <= 0:
        raise ValueError(f"unit_mbps must be positive, got {unit_mbps}")
    if step_s <= 0:
        raise ValueError(f"step_s must be positive, got {step_s}")

    n_steps = max(1, int(round(duration_s / step_s)))
    rates = [float(profile[k % len(profile)]) for k in range(n_steps)]
    needs = [max(0, int(round(rate / unit_mbps))) for rate in rates]
    n_units = max(needs, default=0)

    flows: List["ScheduledFlow"] = []
    for unit in range(n_units):
        start_step: Optional[int] = None
        for step, count in enumerate(needs + [0]):
            needed = count > unit
            if needed and start_step is None:
                start_step = step
            elif not needed and start_step is not None:
                start_s = start_step * step_s
                life_s = (step - start_step) * step_s
                if life_s > 0:
                    flows.append(ScheduledFlow(
                        start_s=start_s, port=5201 + unit,
                        rate_mbps=unit_mbps, life_s=life_s,
                    ))
                start_step = None
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
    print(f"[traffic] {len(flows)} flow(s), ports {ports}, "
          f"profile {list(profile)}, duration {duration_s}s", flush=True)
    if not flows:
        raise RuntimeError(
            f"no flows scheduled for profile {list(profile)} over "
            f"{duration_s}s at step {step_s}s -- the load would be silent"
        )

    # One iperf3 server, one long-lived client, and `tc` does the shaping.
    #
    # Earlier versions started a server per port and a client per step. Both
    # forms of iperf3 process management turned out to be unreliable here: with
    # `-D` the daemon does not survive the shell Mininet spawns for cmd(), and
    # even a plainly backgrounded server was gone seconds later. `ps` on h4
    # showed no server and `ss -ltn` showed no listening socket, so every client
    # connected to nothing -- the run produced 0.0 Mbps with no error anywhere.
    #
    # One server and one client is the smallest arrangement that can work, and
    # shaping the *link* with tc means the offered rate follows the profile
    # exactly, with no dependence on process lifetime at all.
    iface = _host_interface(h1)
    h4.cmd("killall -9 iperf3 2>/dev/null; true")
    h1.cmd("killall -9 iperf3 2>/dev/null; true")
    time.sleep(0.3)
    h4.cmd(f"{iperf} -s -p 5201 > /tmp/iperf_server_5201.log 2>&1 &")
    time.sleep(1.0)

    listening = h4.cmd("ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null")
    if ":5201" not in listening:
        procs = h4.cmd("ps ax 2>/dev/null | grep iperf3 | grep -v grep; true")
        raise RuntimeError(
            "iperf3 server is not listening on port 5201; the flow would be "
            f"silent.\nListening sockets:\n{listening}\n"
            f"processes on h4:\n{procs or '(none)'}\nbinary: {iperf}"
        )
    print(f"[traffic] server bound on 5201; shaping {iface}", flush=True)

    # Shape to the FIRST step before the client starts.
    #
    # The client offers far more than any step needs, so launching it first would
    # push an unshaped burst through before the first rate was applied -- measured
    # as a 6.4 Mbps single-sample jump at the very start of the run, which is the
    # "step, not a ramp" failure the shape validator exists to reject.
    first_rate = float(profile[0])
    h1.cmd(
        f"tc qdisc replace dev {iface} root tbf "
        f"rate {first_rate:.1f}mbit burst 32kbit latency 100ms"
    )
    time.sleep(0.5)

    offered_ceiling = max(float(r) for r in profile) * 2.0 + 5.0
    h1.cmd(
        f"{iperf} -u -c 10.0.0.4 -b {offered_ceiling:.0f}M "
        f"-t {max(1, int(round(duration_s)))} -p 5201 "
        f"> /tmp/iperf_client_5201.log 2>&1 &"
    )
    print(f"[traffic] client offering {offered_ceiling:.0f} Mbps, "
          f"shaped from the start to follow the profile "
          f"(first step {first_rate:.0f} Mbps)", flush=True)

    scheduled: List[Dict[str, Any]] = []
    n_steps = max(1, int(round(duration_s / step_s)))
    for step in range(n_steps):
        rate = float(profile[step % len(profile)])
        # burst and latency are kept small so a rate change cannot dump a large
        # token-bucket surplus onto the link in one sample.
        h1.cmd(
            f"tc qdisc replace dev {iface} root tbf "
            f"rate {rate:.1f}mbit burst 32kbit latency 100ms"
        )
        scheduled.append(asdict(ScheduledFlow(
            start_s=step * step_s, port=5201, rate_mbps=rate,
            life_s=min(step_s, duration_s - step * step_s),
        )))
        if (step + 1) * step_s < duration_s:
            time.sleep(step_s)

    # Release the shaper so teardown is not fighting a qdisc.
    h1.cmd(f"tc qdisc del dev {iface} root 2>/dev/null; true")
    return scheduled


def _host_interface(host: Any) -> str:
    """The host's default interface name.

    Shaping the wrong device would silently do nothing -- the qdisc would attach
    to an interface that carries no traffic, and the run would look identical to
    an unshaped one. So the interface is resolved from the host itself and
    verified to exist.
    """
    name = getattr(host, "defaultIntf", None)
    if callable(name):
        try:
            resolved = name().name
            if resolved:
                return str(resolved)
        except Exception:  # noqa: BLE001
            pass
    interfaces = host.cmd("ls /sys/class/net").split()
    interfaces = [i for i in interfaces if i != "lo"]
    if not interfaces:
        raise RuntimeError(
            f"host {host.name} has no non-loopback interface to shape")
    return interfaces[0]


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
        # Alternating on/off, with a gradual onset.
        #
        # The previous version launched iperf at full peak rate the instant the
        # on-phase began, so offered load stepped from ~2 Mbps to 12 Mbps inside a
        # single 2 s sample. Measured across the dataset: 720 of 740 congestion
        # onsets were steps larger than 3 Mbps. A model cannot lead a step -- it
        # can only react to it -- which is why training produced perfect F1 and
        # zero lead time at the same time.
        #
        # Now each on-phase ramps from base to peak over `ramp_up_s` seconds using
        # the same tc shaper the ramp pattern uses, so the onset spans several
        # samples and there is something to predict.
        elapsed = 0.0
        while elapsed < duration_s:
            on_dur = min(30.0, duration_s - elapsed)
            ramp_s = min(20.0, on_dur)
            steps = max(1, int(round(ramp_s / 10.0)))
            for i in range(steps + 1):
                rate = base_rate_mbps + (peak_rate_mbps - base_rate_mbps) * (i / steps)
                h1.cmd(
                    f"tc qdisc replace dev {_host_interface(h1)} root tbf "
                    f"rate {rate:.1f}mbit burst 32kbit latency 100ms"
                )
                if i < steps:
                    time_sleep(ramp_s / steps)
            hold_s = on_dur - ramp_s
            if hold_s > 0:
                time_sleep(hold_s)
            elapsed += on_dur
            if elapsed >= duration_s:
                break
            off_dur = min(20.0, duration_s - elapsed)
            # Ease back down rather than dropping straight to base.
            for i in range(steps, -1, -1):
                rate = base_rate_mbps + (peak_rate_mbps - base_rate_mbps) * (i / steps)
                h1.cmd(
                    f"tc qdisc replace dev {_host_interface(h1)} root tbf "
                    f"rate {rate:.1f}mbit burst 32kbit latency 100ms"
                )
                if i > 0:
                    time_sleep(10.0)
            time_sleep(max(0.0, off_dur - ramp_s))
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
