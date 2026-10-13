"""Process and binary resolution utilities for the SDN test harness.

Ensures that tools invoked under ``sudo`` or across subshells (``mn``,
``ovs-vsctl``, ``ovs-ofctl``, ``ryu-manager``, ``iperf3``, ``mnexec``)
are resolved explicitly to their active virtualenv or system paths,
preventing PATH truncation errors under ``sudo``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
import sys
from typing import Dict, List, Optional, Sequence


def venv_bin(name: str) -> str:
    """Resolve an executable by name, prioritizing the active Python venv.

    Order of resolution:
    1. Directory containing ``sys.executable`` (e.g. ``.venv/bin/<name>``)
    2. Standard ``shutil.which(name)``
    3. Common system binary paths: ``/usr/local/bin``, ``/usr/bin``, ``/usr/sbin``
    4. Fallback to ``name``
    """
    # 1. Active venv bin directory
    venv_dir = os.path.dirname(sys.executable)
    candidate = os.path.join(venv_dir, name)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return os.path.abspath(candidate)

    # 2. System PATH via shutil.which
    which_path = shutil.which(name)
    if which_path:
        return os.path.abspath(which_path)

    # 3. Standard fallback locations
    for directory in ("/usr/local/bin", "/usr/bin", "/usr/sbin", "/bin", "/sbin"):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.abspath(candidate)

    return name


def check_required_tools(tools: Sequence[str]) -> Dict[str, Optional[str]]:
    """Verify that required tools exist and are executable.

    Returns a dict mapping tool name to its resolved path, or None if missing.
    """
    results: Dict[str, Optional[str]] = {}
    for tool in tools:
        path = venv_bin(tool)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            results[tool] = path
        elif shutil.which(path):
            results[tool] = shutil.which(path)
        else:
            results[tool] = None
    return results


def mn_clean() -> subprocess.CompletedProcess:
    """Execute 'mn -c' using the explicitly resolved mininet binary."""
    mn_path = venv_bin("mn")
    return subprocess.run(
        [mn_path, "-c"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------- #
# Long-lived child processes
# --------------------------------------------------------------------------- #
class ControllerLog:
    """A drained log destination for a long-lived child process.

    Why this exists
    ---------------
    Spawning the controller with ``stdout=subprocess.PIPE`` and never reading it
    is a silent deadlock. The OS pipe buffer is finite (~64 KiB on Linux); once
    the child fills it, the child blocks in ``write()``, and since the
    controller's event loop runs in that same process it stops dispatching
    events entirely.

    The symptom is deeply misleading: no error, no traceback, no stall warning
    -- the controller is simply alive but inert. In practice telemetry stopped
    being written about 24 s into every 90 s collection run, yielding 13 samples
    per link where 45 were expected. Because each run logged the same amount, the
    cutoff was identical every time, which made it look like a deliberate setting
    rather than a resource limit.

    Writing to a file instead keeps the child's output visible for diagnosis
    *and* drains the pipe, since a file never applies back-pressure. Redirecting
    to ``DEVNULL`` would also drain but would throw away the log that is needed
    to work out why a run behaved oddly.

    Usage::

        with controller_log("data/logs/run_42.log") as sink:
            proc = subprocess.Popen([...], stdout=sink, stderr=sink)

        proc.wait()
    """

    def __init__(self, path: str | os.PathLike[str], *, append: bool = False) -> None:
        self.path = Path(path)
        self.append = append
        self._handle = None

    def __enter__(self) -> int:
        """Open the log and return a file descriptor for ``subprocess``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a" if self.append else "w",
                                     encoding="utf-8", errors="replace")
        return self._handle.fileno()

    def __exit__(self, *exc_info: Any) -> None:
        """Close the log file."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def controller_log(name: str, *, log_dir: str | os.PathLike[str] = "data/logs") -> Path:
    """A conventional path for one run's controller log."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return Path(log_dir) / f"controller_{safe}.log"
