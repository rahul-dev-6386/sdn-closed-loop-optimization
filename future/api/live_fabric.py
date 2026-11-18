"""Live SDN Mininet Fabric & Ryu Controller Manager for the Web Dashboard.

Provides backend orchestration for real packet injection and dynamic OpenFlow rerouting:
- Starts / stops Ryu OpenFlow 1.3 controller with ML-aware reroute loop
- Starts / stops Mininet multipath diamond topology (s1-s2-s4 and s1-s3-s4)
- Launches real iperf3 traffic spikes from host h1 to host h4 (14 Mbps saturation)
- Allows switching routing modes dynamically (ml_aware vs static_shortest)
- Exposes fabric status, switch port stats, and active traffic state
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]

from common.config import load_config
from common.proc import ControllerLog, controller_log, mn_clean, venv_bin
from controller.reroute_loop import LoopConfig

LOGGER = logging.getLogger("api.live_fabric")


def host_exec(node: Any, cmd: str) -> None:
    """Execute a shell command inside a Mininet host namespace using mnexec."""
    mnexec = venv_bin("mnexec")
    try:
        subprocess.run(
            [mnexec, "-a", str(node.pid), "bash", "-c", cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            check=False,
        )
    except Exception as exc:
        LOGGER.warning("host_exec on %s failed: %s", getattr(node, "name", "node"), exc)


class LiveFabricManager:
    """Singleton managing the live Mininet network, Ryu controller, and iperf3 traffic."""

    _instance: Optional["LiveFabricManager"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.config = load_config()
        self.ryu_proc: Optional[subprocess.Popen] = None
        self.topo: Any = None
        self.net: Any = None
        self.spike_proc: Optional[subprocess.Popen] = None
        self.current_mode: str = "ml_aware"
        self.is_running: bool = False
        self.spike_active: bool = False
        self.spike_start_time: float = 0.0
        self.spike_duration_s: float = 30.0
        self.spike_rate_mbps: float = 14.0
        self._start_lock = threading.RLock()
        self._spike_lock = threading.RLock()

    @classmethod
    def get_instance(cls) -> "LiveFabricManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = LiveFabricManager()
            return cls._instance

    # ------------------------------------------------------------- detection --
    @staticmethod
    def _controller_pids() -> List[int]:
        """PIDs of ryu-manager processes running our controller app.

        A fabric survives an API restart (ryu is spawned with ``setsid``), so a
        freshly started API process must be able to discover a fabric it did
        not spawn itself — otherwise ``is_running`` lies after any restart.
        """
        try:
            out = subprocess.run(
                ["pgrep", "-f", "ryu-manager.*sdn_controller"],
                capture_output=True, timeout=2, check=False).stdout.decode().split()
            return [int(p) for p in out if p.strip().isdigit()]
        except Exception:
            return []

    @staticmethod
    def _host_pid(name: str) -> Optional[int]:
        """PID of a Mininet host namespace process (e.g. ``h1``).

        Mininet names the host shells ``mininet:h1`` (no space), so match that
        exact token rather than the friendlier ``mininet: h1`` which never
        matches.
        """
        try:
            out = subprocess.run(
                ["pgrep", "-f", rf"mininet:{name}$"],
                capture_output=True, timeout=2, check=False).stdout.decode().split()
            for token in out:
                if token.strip().isdigit():
                    return int(token)
            return None
        except Exception:
            return None

    def running(self) -> bool:
        """True if a live fabric exists — ours, or a detached one from before an API restart."""
        if self.is_running and self.net is not None:
            return True
        return bool(self._controller_pids())

    # ------------------------------------------------------------- lifecycle --
    def start_fabric(self, mode: str = "ml_aware") -> Dict[str, Any]:
        """Start the Ryu controller and Mininet fabric if not already running."""
        with self._start_lock:
            if self.running():
                return {
                    "status": "running",
                    "mode": self.current_mode,
                    "message": "Fabric is already active",
                }

            LOGGER.info("Starting live Ryu controller and Mininet fabric...")
            self.current_mode = mode

            # 1. Clean slate FIRST. `mn -c` runs `killall ryu-manager`, so
            #    calling mn_clean() any time after the controller spawn kills
            #    the very Ryu we just started -- which is exactly what left every
            #    UI-started fabric with a dead controller, 100% ping loss and
            #    zero telemetry while reporting "status: running".
            try:
                mn_clean()
            except Exception:
                pass

            # 2. Start Ryu controller
            from ml.collect_dataset import wait_for_port

            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env[LoopConfig.MODE_ENV_VAR] = self.current_mode
            log_sink = controller_log(f"live_{self.current_mode}", log_dir=ROOT / "data/logs")
            log_handle = ControllerLog(log_sink)
            log_fd = log_handle.__enter__()

            try:
                self.ryu_proc = subprocess.Popen(
                    [
                        venv_bin("ryu-manager"),
                        "--ofp-listen-host",
                        "127.0.0.1",
                        "--ofp-tcp-listen-port",
                        "6653",
                        "controller.sdn_controller",
                    ],
                    cwd=str(ROOT),
                    env=env,
                    stdout=log_fd,
                    stderr=log_fd,
                    preexec_fn=os.setsid,
                )
                log_handle.__exit__()

                if not wait_for_port(6653, timeout_s=12.0):
                    self.stop_fabric()
                    raise RuntimeError("Ryu controller failed to listen on port 6653 within 12s")

                LOGGER.info("Ryu controller listening on 127.0.0.1:6653")

                # 3. Build Mininet Fabric
                from topology.multipath_topology import MultipathTopology, build_spec

                spec = build_spec(self.config)
                self.topo = MultipathTopology(
                    spec,
                    controller=("127.0.0.1", 6653),
                    datapath_type=str(self.config.get("topology.datapath_type", "kernel")),
                )
                self.net = self.topo.build()

                # Allow switches to establish OpenFlow handshake and LLDP exchange
                time.sleep(3.0)

                # Prime ARP and forward paths with a ping
                try:
                    self.net.pingAll()
                except Exception as exc:
                    LOGGER.warning("Initial pingAll encountered warning: %s", exc)


                # 3. Start iperf3 server on h4 in daemon mode
                h4 = self.net.get("h4")
                if h4:
                    iperf = venv_bin("iperf3")
                    host_exec(h4, f"pkill -9 iperf3 2>/dev/null || true")
                    time.sleep(0.3)
                    host_exec(h4, f"{iperf} -s -D -p 5201")
                    LOGGER.info("iperf3 server active on h4:5201")

                self.is_running = True
                return {
                    "status": "running",
                    "mode": self.current_mode,
                    "switches": ["s1", "s2", "s3", "s4"],
                    "hosts": ["h1", "h4"],
                }

            except Exception as exc:
                LOGGER.error("Failed to start live fabric: %s", exc)
                self.stop_fabric()
                return {"status": "error", "error": str(exc)}

    def stop_fabric(self) -> Dict[str, Any]:
        """Tear down Mininet and kill the Ryu controller cleanly."""
        with self._start_lock:
            LOGGER.info("Tearing down Mininet fabric and stopping controller...")
            self.stop_spike()

            if self.topo is not None:
                try:
                    self.topo.stop()
                except Exception:
                    pass
                self.topo = None

            if self.net is not None:
                try:
                    self.net.stop()
                except Exception:
                    pass
                self.net = None

            if self.ryu_proc is not None:
                try:
                    os.killpg(os.getpgid(self.ryu_proc.pid), signal.SIGTERM)
                    self.ryu_proc.wait(timeout=3.0)
                except Exception:
                    try:
                        os.killpg(os.getpgid(self.ryu_proc.pid), signal.SIGKILL)
                    except Exception:
                        pass
                self.ryu_proc = None
            else:
                # No handle from this API process (fabric started before an
                # API restart) — kill the detached controller(s) by detection.
                for pid in self._controller_pids():
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        continue
                deadline = time.time() + 3.0
                while time.time() < deadline and self._controller_pids():
                    time.sleep(0.2)
                for pid in self._controller_pids():
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        continue

            try:
                mn_clean()
            except Exception:
                pass

            self.is_running = False
            return {"status": "stopped"}

    # ------------------------------------------------------------- traffic --
    def start_spike(self, rate_mbps: float = 14.0, duration_s: float = 45.0) -> Dict[str, Any]:
        """Inject real 14 Mbps UDP traffic from h1 to h4 using iperf3."""
        with self._spike_lock:
            if self.net is not None and self.is_running:
                pass  # managed path — Mininet object available
            elif self.running():
                # Detached fabric from before an API restart: no Mininet object,
                # but hosts exist as steady namespaces; drive them via mnexec.
                return self._start_spike_detached(rate_mbps=rate_mbps, duration_s=duration_s)
            else:
                # Auto-start fabric if stopped
                res = self.start_fabric(mode=self.current_mode)
                if res.get("status") == "error":
                    return res
                if self.net is None:
                    return {"status": "error", "error": "fabric started but hosts unavailable"}

            h1 = self.net.get("h1")
            h4 = self.net.get("h4")
            if not h1 or not h4:
                return {"status": "error", "error": "Hosts h1/h4 unavailable"}

            # Ensure server is running on h4
            iperf = venv_bin("iperf3")
            host_exec(h4, f"{iperf} -s -D -p 5201 2>/dev/null || true")
            time.sleep(0.2)

            # Stop any existing spike
            self._stop_spike_unlocked()

            LOGGER.info("Starting real %s Mbps iperf3 traffic spike from h1 to h4...", rate_mbps)
            cmd = [
                iperf,
                "-u",
                "-c",
                "10.0.0.4",
                "-b",
                f"{rate_mbps}M",
                "-t",
                str(int(duration_s)),
                "-p",
                "5201",
            ]
            self.spike_proc = h1.popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.spike_active = True
            self.spike_start_time = time.time()
            self.spike_duration_s = duration_s
            self.spike_rate_mbps = rate_mbps

            return {
                "status": "spike_started",
                "rate_mbps": rate_mbps,
                "duration_s": duration_s,
                "src": "h1 (10.0.0.1)",
                "dst": "h4 (10.0.0.4)",
            }

    def _start_spike_detached(self, rate_mbps: float, duration_s: float) -> Dict[str, Any]:
        """Start the spike on a fabric this API process did not spawn.

        Finds the Mininet host namespaces by process name and drives them with
        ``mnexec`` — the same mechanism Mininet itself uses. Returns an explicit
        failure (never a fabricated success) if the namespaces are gone.
        """
        h1_pid = self._host_pid("h1")
        h4_pid = self._host_pid("h4")
        if not h1_pid or not h4_pid:
            return {"status": "error",
                    "error": "host namespaces not found (fabric down or unrecognised)"}

        iperf = venv_bin("iperf3")
        mnexec = venv_bin("mnexec")

        # (Re)start the iperf3 server on h4
        subprocess.run([mnexec, "-d", "-a", str(h4_pid), iperf, "-s", "-D", "-p", "5201"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, check=False)
        time.sleep(0.3)

        # Kill any detached client first
        subprocess.run(["pkill", "-9", "-f", "iperf3 -u"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=3, check=False)
        time.sleep(0.2)

        cmd = [
            mnexec, "-d", "-a", str(h1_pid),
            iperf, "-u", "-c", "10.0.0.4", "-b", f"{rate_mbps}M",
            "-t", str(int(duration_s)), "-p", "5201",
        ]
        self.spike_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        self.spike_active = True
        self.spike_start_time = time.time()
        self.spike_duration_s = duration_s
        self.spike_rate_mbps = rate_mbps
        self.net = None  # explicitly detached
        LOGGER.info("Detached-fabric spike started: %s Mbps h1->h4 for %ss (h1 pid=%s)",
                    rate_mbps, duration_s, h1_pid)
        return {
            "status": "spike_started",
            "rate_mbps": rate_mbps,
            "duration_s": duration_s,
            "src": "h1 (10.0.0.1)",
            "dst": "h4 (10.0.0.4)",
            "detached": True,
        }

    def _stop_spike_unlocked(self) -> None:
        """Internal helper to kill active spike processes without acquiring lock."""
        if self.spike_proc is not None:
            try:
                self.spike_proc.terminate()
                self.spike_proc.wait(timeout=1.5)
            except Exception:
                try:
                    self.spike_proc.kill()
                except Exception:
                    pass
            self.spike_proc = None

        # Kill any detached client/server too (namespace membership doesn't
        # hide processes from the host's process table).
        subprocess.run(["pkill", "-9", "-f", "iperf3 -u"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=3, check=False)

        if self.net is not None:
            try:
                h1 = self.net.get("h1")
                if h1:
                    host_exec(h1, "pkill -9 -f 'iperf3 -u' 2>/dev/null || true")
            except Exception:
                pass

        self.spike_active = False

    def stop_spike(self) -> Dict[str, Any]:
        """Terminate active iperf3 client traffic."""
        with self._spike_lock:
            self._stop_spike_unlocked()
            return {"status": "spike_stopped"}

    # --------------------------------------------------------------- mode --
    def set_routing_mode(self, mode: str) -> Dict[str, Any]:
        """Switch routing mode (ml_aware vs static_shortest)."""
        if mode not in ("ml_aware", "static_shortest", "reactive_threshold", "ecmp"):
            return {"status": "error", "error": f"Invalid mode: {mode}"}

        self.current_mode = mode
        # If fabric is running, restart controller in new mode to apply changes cleanly
        if self.running():
            LOGGER.info("Switching live controller mode to %s...", mode)
            # Restart controller with new mode env var
            if self.ryu_proc is not None:
                try:
                    os.killpg(os.getpgid(self.ryu_proc.pid), signal.SIGTERM)
                    self.ryu_proc.wait(timeout=2.0)
                except Exception:
                    pass
                self.ryu_proc = None
            else:
                # Detached fabric from before an API restart: kill by detection
                for pid in self._controller_pids():
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        continue
                deadline = time.time() + 3.0
                while time.time() < deadline and self._controller_pids():
                    time.sleep(0.2)

            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env[LoopConfig.MODE_ENV_VAR] = mode
            log_sink = controller_log(f"live_{mode}", log_dir=ROOT / "data/logs")
            log_handle = ControllerLog(log_sink)
            log_fd = log_handle.__enter__()

            self.ryu_proc = subprocess.Popen(
                [
                    venv_bin("ryu-manager"),
                    "--ofp-listen-host",
                    "127.0.0.1",
                    "--ofp-tcp-listen-port",
                    "6653",
                    "controller.sdn_controller",
                ],
                cwd=str(ROOT),
                env=env,
                stdout=log_fd,
                stderr=log_fd,
                preexec_fn=os.setsid,
            )
            log_handle.__exit__()

            # Confirm the replacement actually bound — otherwise the UI would
            # claim a mode that isn't live.
            from ml.collect_dataset import wait_for_port
            if not wait_for_port(6653, timeout_s=10.0):
                return {"status": "error",
                        "error": "controller failed to bind 6653 after mode switch"}

        return {"status": "mode_updated", "mode": self.current_mode}

    # ------------------------------------------------------------- status --
    def get_status(self) -> Dict[str, Any]:
        """Return the current live fabric state.

        After an API restart the flag is gone, but the fabric itself survives —
        so the status is derived from what's actually running (controller
        process + OpenFlow listener), not from this process's memory.
        """
        if self.spike_active and self.spike_proc is not None:
            if self.spike_proc.poll() is not None:
                self.spike_active = False
        elif self.spike_active:
            # Detached spike: check the detached client process
            out = subprocess.run(["pgrep", "-f", "iperf3 -u -c 10.0.0.4"],
                                 capture_output=True, timeout=2, check=False).stdout
            if not out.strip():
                self.spike_active = False

        elapsed = time.time() - self.spike_start_time if self.spike_active else 0.0
        running = self.running()

        return {
            "fabric_running": running,
            "routing_mode": self.current_mode,
            "spike_active": self.spike_active,
            "spike_rate_mbps": self.spike_rate_mbps if self.spike_active else 0.0,
            "spike_elapsed_s": round(elapsed, 1) if self.spike_active else 0.0,
            "spike_duration_s": self.spike_duration_s if self.spike_active else 0.0,
        }
