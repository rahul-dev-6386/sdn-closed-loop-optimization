#!/usr/bin/env python3
"""Capture a verifiable snapshot of project progress.

Purpose
-------
Every phase claims to be "done" with a pass count. Those claims rot silently:
a later change can break an earlier phase while the README still says green.
This script produces *evidence* -- a timestamped, machine-readable record of
what actually passed when it last ran.

For each target it runs the real verification command, records the exit status
and any ``SUMMARY: n passed, m failed`` line it finds, and writes:

* ``results/snapshots/MANIFEST.json`` -- the machine-readable ledger
* ``results/snapshots/PROGRESS.md``    -- a human-readable summary to show
* ``results/snapshots/logs/<name>.log`` -- full raw output

Re-running is safe: each snapshot is archived under a UTC timestamp, and
MANIFEST.json always reflects the latest run.

Usage
-----
    python scripts/snapshot.py                # quick targets (no root needed)
    python scripts/snapshot.py --all         # everything incl. root targets
    python scripts/snapshot.py --only p3 p7  # a subset
    python scripts/snapshot.py --list        # show targets, run nothing
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
SNAP_DIR = ROOT / "results" / "snapshots"
LOG_DIR = SNAP_DIR / "logs"
MANIFEST = SNAP_DIR / "MANIFEST.json"
PROGRESS_MD = SNAP_DIR / "PROGRESS.md"

SUMMARY_RE = re.compile(r"SUMMARY:\s*(\d+)\s*passed,\s*(\d+)\s*failed")
PASSED_RE = re.compile(r"^\s*\[PASS\]", re.MULTILINE)
FAILED_RE = re.compile(r"^\s*\[FAIL\]", re.MULTILINE)

#: pytest's own summary line, e.g. "270 passed, 2 failed, 1 skipped in 5.71s".
#: The counts are optional and their order is not fixed, so each is matched
#: independently and a missing one defaults to 0.
PYTEST_LINE_RE = re.compile(r"^.*?(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)\b.*$", re.MULTILINE)
PYTEST_COUNTS = {
    "passed": "passed",
    "failed": "failed",
    "error": "failed",
    "errors": "failed",
    "skipped": "skipped",
    "xfailed": "xfailed",
    "xpassed": "xpassed",
}


def parse_pytest_counts(output: str) -> Optional[Dict[str, int]]:
    """Extract pytest's tally from its final summary line.

    Returns ``None`` when no pytest-style line is present, so the caller can
    fall back to counting ``[PASS]``/``[FAIL]`` lines instead.
    """
    # The summary is the last non-empty line of a pytest run.
    for line in reversed([ln for ln in output.splitlines() if ln.strip()]):
        if "passed" not in line and "failed" not in line and "error" not in line:
            continue
        counts: Dict[str, int] = {}
        for match in re.finditer(r"(\d+)\s+(\w+)", line):
            number, word = int(match.group(1)), match.group(2)
            key = PYTEST_COUNTS.get(word)
            if key:
                counts[key] = counts.get(key, 0) + number
        if "passed" in counts or "failed" in counts:
            counts.setdefault("passed", 0)
            counts.setdefault("failed", 0)
            return counts
    return None


@dataclass
class Target:
    """One verification target that can be snapshotted."""

    key: str
    phase: str
    title: str
    argv: List[str]
    needs_root: bool = False
    timeout: int = 1800

    @property
    def script(self) -> Optional[Path]:
        """The repo script this target runs, when it names one.

        Only tokens that look like repo-relative script paths are considered, so
        a target invoking a plain command (``true``, ``sleep``) is not mistaken
        for a missing file.
        """
        for token in self.argv:
            candidate = Path(token)
            if candidate.suffix in {".py", ".sh"} and not candidate.is_absolute():
                return ROOT / candidate
        return None

    @property
    def available(self) -> bool:
        """Whether the target can actually run.

        A catalogue entry whose script has not been written yet is reported as
        ``planned`` rather than silently skipped, so an unbuilt phase shows up as
        unbuilt instead of vanishing.
        """
        script = self.script
        return script is None or script.is_file()


def targets() -> Dict[str, Target]:
    """The snapshot catalogue.

    ``argv`` is the *real* verification command, so a snapshot can never
    disagree with what CI or the Makefile runs.
    """
    py = sys.executable
    return {
        "p1": Target("p1", "P1", "Environment + remote-controller pingall",
                     ["bash", "scripts/verify_p1.sh"]),
        "p2": Target("p2", "P2", "Multipath topology, both paths isolated",
                     [py, "scripts/verify_p2.py"], needs_root=True),
        "p3": Target("p3", "P3", "Loop-safe controller: pingall, storm, flows, restart",
                     [py, "scripts/verify_p3.py"], needs_root=True),
        "p4": Target("p4", "P4", "Live LLDP topology discovery",
                     [py, "scripts/verify_p4_live.py"], needs_root=True),
        "p5": Target("p5", "P5", "Telemetry rate/latency/loss, live",
                     [py, "scripts/verify_p5_live.py"], needs_root=True),
        "p6": Target("p6", "P6", "Dataset labelling + class balance",
                     [py, "scripts/verify_p6.py"]),
        "p7": Target("p7", "P7", "ML training + evaluation",
                     [py, "scripts/verify_p7.py"]),
        "p10": Target("p10", "P10", "Experiments: 4 modes x scenarios x reps",
                      [py, "scripts/verify_p10.py"], needs_root=True),
        "unit": Target("unit", "--", "Unit test suite",
                       [py, "-m", "pytest", "tests/", "-q"]),
    }


def git_commit() -> str:
    """Current short commit hash, or ``unknown`` outside a repo."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=ROOT, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def run_target(target: Target) -> Dict[str, Any]:
    """Execute one target and summarise its outcome."""
    label = f"{target.phase} {target.title}"
    print(f"--> {label}", flush=True)

    started = time.monotonic()
    argv = list(target.argv)
    if not target.available:
        return {
            "key": target.key, "phase": target.phase, "title": target.title,
            "status": "planned",
            "reason": "verification script not written yet",
            "needs_root": target.needs_root, "ran_at": _now(),
        }
    # Escalate to sudo only for root targets, and only when not already root.
    if target.needs_root and os.geteuid() != 0:
        argv = ["sudo", "-n"] + argv
        if not _sudo_available():
            return {
                "key": target.key, "phase": target.phase, "title": target.title,
                "status": "skipped",
                "reason": "needs root but passwordless sudo is unavailable",
                "needs_root": True, "ran_at": _now(),
            }

    try:
        proc = subprocess.run(
            argv, cwd=ROOT, capture_output=True, text=True,
            timeout=target.timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "key": target.key, "phase": target.phase, "title": target.title,
            "status": "timeout", "needs_root": target.needs_root,
            "ran_at": _now(), "seconds": target.timeout,
        }

    output = (proc.stdout or "") + (proc.stderr or "")
    passed = failed = skipped = None
    match = SUMMARY_RE.search(output)
    if match:
        # The project's own acceptance harnesses print an explicit SUMMARY line.
        passed, failed = int(match.group(1)), int(match.group(2))
    else:
        pytest_counts = parse_pytest_counts(output)
        if pytest_counts:
            passed = pytest_counts["passed"]
            failed = pytest_counts["failed"]
            skipped = pytest_counts.get("skipped")
        else:
            # Last resort: count individual verdict lines.
            n_pass = len(PASSED_RE.findall(output))
            n_fail = len(FAILED_RE.findall(output))
            if n_pass or n_fail:
                passed, failed = n_pass, n_fail

    status = "pass" if proc.returncode == 0 and (failed in (0, None)) else "fail"

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = LOG_DIR / f"{target.key}-{stamp}.log"
    log_path.write_text(output, encoding="utf-8", errors="replace")

    return {
        "key": target.key, "phase": target.phase, "title": target.title,
        "status": status, "exit_code": proc.returncode,
        "passed": passed, "failed": failed, "skipped": skipped,
        "needs_root": target.needs_root,
        "ran_at": _now(),
        "seconds": round(time.monotonic() - started, 1),
        "log": _relative(log_path),
    }


def _relative(path: Path) -> str:
    """Path relative to the repo root, or absolute if it lies outside.

    The log directory is overridable, so a caller may legitimately point it
    outside the repo; the snapshot must still record a usable path rather than
    raise from ``relative_to``.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _sudo_available() -> bool:
    proc = subprocess.run(["sudo", "-n", "true"], capture_output=True,
                          text=True, timeout=10, check=False)
    return proc.returncode == 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_manifest() -> Dict[str, Any]:
    """Read the existing ledger, or start a fresh one."""
    if MANIFEST.is_file():
        try:
            return json.loads(MANIFEST.read_text())
        except json.JSONDecodeError:
            pass
    return {"project": "AI-Driven SDN Traffic Optimization", "targets": {}}


def write_manifest(manifest: Dict[str, Any]) -> None:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = _now()
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=False))


def render_progress(manifest: Dict[str, Any]) -> str:
    """Render the human-readable progress table."""
    rows = ["| Phase | Verification | Result | Passed | Failed | Checked (UTC) |",
            "|---|---|---|---|---|---|"]
    badge = {"pass": "pass", "fail": "**FAIL**", "skipped": "skipped",
             "timeout": "**TIMEOUT**", "planned": "not built"}
    order = ["p1", "p2", "p3", "p4", "p5", "p6", "p7", "p10", "unit"]
    targets_map = targets()
    for key in order + [k for k in manifest["targets"] if k not in order]:
        rec = manifest["targets"].get(key)
        tgt = targets_map.get(key)
        title = rec["title"] if rec else (tgt.title if tgt else key)
        if rec is None:
            state = "not run" if (tgt is None or tgt.available) else "not built"
            rows.append(f"| {tgt.phase if tgt else key} | {title} | {state} | - | - | - |")
            continue
        st = badge.get(rec.get("status", ""), rec.get("status", "?"))
        p = rec.get("passed")
        f = rec.get("failed")
        p_s = "-" if p is None else str(p)
        f_s = "-" if f is None else str(f)
        rows.append(
            f"| {rec['phase']} | {title} | {st} | {p_s} | {f_s} | {rec.get('ran_at','-')} |"
        )
    stamp = manifest.get("updated_at", _now())
    commit = manifest.get("commit", "unknown")
    return (
        "# Progress snapshot\n\n"
        f"Generated {stamp} at commit `{commit}`.\n\n"
        "This file is generated by `scripts/snapshot.py`. Re-run `make snapshot`\n"
        "to refresh it; it never reflects intent, only measured results.\n\n"
        + "\n".join(rows)
        + "\n"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true",
                        help="include root-only targets")
    parser.add_argument("--only", nargs="*", metavar="KEY",
                        help="run only these target keys")
    parser.add_argument("--list", action="store_true",
                        help="list target keys and exit")
    args = parser.parse_args(argv)

    catalogue = targets()
    if args.list:
        for key, t in catalogue.items():
            root = " (root)" if t.needs_root else ""
            print(f"  {key:5} {t.phase:4} {t.title}{root}")
        return 0

    if args.only:
        selected = [catalogue[k] for k in args.only if k in catalogue]
        missing = [k for k in args.only if k not in catalogue]
        if missing:
            print(f"unknown target(s): {missing}", file=sys.stderr)
            return 2
    elif args.all:
        selected = list(catalogue.values())
    else:
        selected = [t for t in catalogue.values() if not t.needs_root]

    manifest = load_manifest()
    manifest["commit"] = git_commit()

    for target in selected:
        record = run_target(target)
        manifest["targets"][target.key] = record
        status = record.get("status")
        detail = (f"{record['passed']}/{record['passed'] + record['failed']}"
                  if record.get("passed") is not None else status)
        if record.get("reason"):
            detail = f"{detail} ({record['reason']})"
        print(f"    {status.upper()} {detail}", flush=True)

    write_manifest(manifest)
    PROGRESS_MD.write_text(render_progress(manifest), encoding="utf-8")
    print(f"\nmanifest: {MANIFEST.relative_to(ROOT)}")
    print(f"progress: {PROGRESS_MD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())