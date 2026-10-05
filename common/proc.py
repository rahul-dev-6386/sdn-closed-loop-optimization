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
