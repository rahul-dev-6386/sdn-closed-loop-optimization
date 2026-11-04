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

    # ------------------------------------------------------------- lifecycle --
    def start_fabric(self, mode: str = "ml_aware") -> Dict[str, Any]:
        """Start the Ryu controller and Mininet fabric if not already running."""
        with self._start_lock:
            if self.is_running and self.net is not None:
                return {
                    "status": "running",
                    "mode": self.current_mode,
                    "message": "Fabric is already active",
                }

            LOGGER.info("Starting live Ryu controller and Mininet fabric...")
            self.current_mode = mode

            # 1. Start Ryu controller
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

                # 2. Build Mininet Fabric
                from topology.multipath_topology import MultipathTopology, build_spec

                try:
                    mn_clean()
                except Exception:
                    pass

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
            if not self.is_running or self.net is None:
                # Auto-start fabric if stopped
                res = self.start_fabric(mode=self.current_mode)
                if res.get("status") == "error":
                    return res

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
        if self.is_running:
            LOGGER.info("Switching live controller mode to %s...", mode)
            # Restart controller with new mode env var
            if self.ryu_proc is not None:
                try:
                    os.killpg(os.getpgid(self.ryu_proc.pid), signal.SIGTERM)
                    self.ryu_proc.wait(timeout=2.0)
                except Exception:
                    pass

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

        return {"status": "mode_updated", "mode": self.current_mode}

    # ------------------------------------------------------------- status --
    def get_status(self) -> Dict[str, Any]:
        """Return the current live fabric state."""
        # Check if spike is still running
        if self.spike_active and self.spike_proc is not None:
            if self.spike_proc.poll() is not None:
                self.spike_active = False

        elapsed = time.time() - self.spike_start_time if self.spike_active else 0.0

        return {
            "fabric_running": self.is_running,
            "routing_mode": self.current_mode,
            "spike_active": self.spike_active,
            "spike_rate_mbps": self.spike_rate_mbps if self.spike_active else 0.0,
            "spike_elapsed_s": round(elapsed, 1) if self.spike_active else 0.0,
            "spike_duration_s": self.spike_duration_s if self.spike_active else 0.0,
        }
