"""Locating the Mininet CLI reliably.

Several scripts shell out to ``mn`` for teardown. Invoking it as a bare
``["mn", ...]`` only works when the project venv happens to be on root's
``PATH`` -- and ``sudo`` resets ``PATH`` to ``secure_path``, so under the
Makefile's ``sudo $(PY) scripts/...`` targets the call fails with
``FileNotFoundError: 'mn'``. That is exactly how ``ml/collect_dataset.py``
died on its first live run.

Resolution order:

1. ``$SDN_MN`` -- explicit override.
2. ``shutil.which("mn")`` -- a system-wide Mininet install.
3. ``<repo>/.venv/bin/mn`` -- this project's venv, which is where P1 puts it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]


class MininetNotFound(RuntimeError):
    """Raised when no usable ``mn`` executable can be located."""


def mn_path() -> str:
    """Return an absolute path to the ``mn`` executable.

    Raises
    ------
    MininetNotFound
        If none of the candidate locations exists.
    """
    override = os.environ.get("SDN_MN")
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        raise MininetNotFound(f"SDN_MN points at {override!r}, which is not executable")

    found = shutil.which("mn")
    if found:
        return found

    candidate = REPO_ROOT / ".venv" / "bin" / "mn"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)

    raise MininetNotFound(
        "could not find the Mininet CLI.\n"
        "Tried $SDN_MN, `which mn`, and "
        f"{candidate}.\n"
        "Run `make setup` to install Mininet into the venv, or set SDN_MN."
    )


def mn_available() -> bool:
    """True when ``mn`` can be located."""
    try:
        mn_path()
    except MininetNotFound:
        return False
    return True


def run_mn(args: Sequence[str], *, timeout: Optional[float] = 120,
           quiet: bool = True) -> subprocess.CompletedProcess:
    """Run ``mn`` with the given arguments.

    Never raises on a non-zero exit: teardown commands are best-effort and a
    failed cleanup must not abort the run that triggered it.
    """
    cmd: List[str] = [mn_path(), *args]
    return subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
        timeout=timeout,
        check=False,
    )


def mn_clean(**kwargs) -> subprocess.CompletedProcess:
    """Tear down any leftover Mininet state (``mn -c``)."""
    return run_mn(["-c"], **kwargs)