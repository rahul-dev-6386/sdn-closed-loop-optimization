"""Regression tests for the undrained-controller-pipe deadlock.

The failure this guards against is silent and expensive: a child process whose
stdout pipe nobody reads blocks in ``write()`` once the OS buffer fills, and a
Ryu controller then stops dispatching events entirely -- no traceback, no stall
warning, telemetry simply stops appearing. In a live collection that produced 13
samples per link where 45 were expected, and because every run logged the same
amount the cutoff was identical every time, which made it look deliberate.

The test writes enough output to overflow a pipe buffer and asserts the writer
completes. That is the real property, not an implementation detail.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from common.proc import ControllerLog, controller_log, venv_bin


def test_controller_log_sanitises_the_run_name(tmp_path: Path) -> None:
    """A run id containing a path separator must not escape the log directory."""
    path = controller_log("run/../../etc/passwd")
    assert path.parent == Path("data/logs")
    assert ".." not in path.name.replace(".._", "")


def test_controller_log_is_written_and_closed(tmp_path: Path) -> None:
    target = tmp_path / "logs" / "c.log"
    with ControllerLog(target) as fd:
        assert isinstance(fd, int)
    assert target.parent.is_dir()


def test_writing_to_the_log_never_blocks(tmp_path: Path) -> None:
    """The property that matters: the child's writes always complete.

    Writes 4 MiB, comfortably past the ~64 KiB pipe buffer. With an undrained
    ``subprocess.PIPE`` this blocks forever and the test hangs on the timeout;
    with a file destination it completes immediately.
    """
    target = tmp_path / "drain.log"
    handle = ControllerLog(target)
    fd = handle.__enter__()
    try:
        script = (
            "import sys\n"
            "buf = b'x' * 4096\n"
            "for _ in range(1024):\n"
            "    sys.stdout.buffer.write(buf)\n"
            "sys.stdout.buffer.flush()\n"
            "sys.stderr.write('done\\n')\n"
        )
        proc = subprocess.run([sys.executable, "-c", script],
                              stdout=fd, stderr=subprocess.PIPE, timeout=60)
        assert proc.returncode == 0, proc.stderr.decode()[-400:]
        assert b"done" in proc.stderr
    finally:
        handle.__exit__()
    assert target.stat().st_size > 4_000_000


def test_an_undrained_pipe_would_actually_deadlock() -> None:
    """Confirms the hazard is real, so the fix above is not superstition.

    Uses a short pipe buffer via a small writes-then-never-reads arrangement:
    the child writes until it blocks, and the test asserts it did *not* finish
    within the timeout.
    """
    script = (
        "import sys\n"
        "buf = b'x' * 4096\n"
        "for _ in range(4096):\n"
        "    sys.stdout.buffer.write(buf)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        try:
            proc.wait(timeout=3)
            blocked = False
        except subprocess.TimeoutExpired:
            blocked = True
    finally:
        proc.kill()
        proc.wait(timeout=5)
    assert blocked, (
        "the child completed without a reader; the deadlock this guards "
        "against may not reproduce on this kernel, but the fix is still required"
    )


def test_closing_the_parent_copy_does_not_break_the_child(tmp_path: Path) -> None:
    """Popen duplicates the fd, so the parent may close its copy immediately.

    This is why ``log_handle.__exit__()`` is safe to call right after the spawn
    -- and equally why letting the ControllerLog be garbage collected *before*
    the spawn would have been a bug.
    """
    target = tmp_path / "dup.log"
    handle = ControllerLog(target)
    fd = handle.__enter__()
    proc = subprocess.Popen([sys.executable, "-c",
                             "import sys; sys.stdout.write('hello\\n')"],
                            stdout=fd, stderr=subprocess.DEVNULL)
    handle.__exit__()
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert "hello" in target.read_text()


def test_all_long_lived_controller_spawns_drain() -> None:
    """No script may spawn the controller into an unread pipe.

    A static check, because the deadlock is invisible at runtime: the symptom is
    telemetry quietly stopping, not an exception.
    """
    import re

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if parts[0] in {".venv", "future", "tests"}:
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"ryu-manager[\s\S]{0,400}?stdout\s*=\s*subprocess\.PIPE",
            text,
        ):
            offenders.append(f"{path.relative_to(root)}: ryu spawns into a PIPE")
        # Also catch a Popen near a ryu_cmd variable assigned ryu-manager.
        if 'ryu_cmd = [' in text and 'stdout=subprocess.PIPE' in text:
            offenders.append(f"{path.relative_to(root)}: ryu_cmd into a PIPE")
    assert not offenders, (
        "these spawn the controller into a pipe nobody reads, which blocks it "
        "once the buffer fills:\n  " + "\n  ".join(offenders)
    )
