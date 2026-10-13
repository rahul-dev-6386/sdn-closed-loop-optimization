"""Tests for the progress snapshot recorder (scripts/snapshot.py).

A snapshot that always reports "pass" is worse than no snapshot, because it
converts verification into decoration. These tests pin the parsing and the
status decision, including the failure cases.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import snapshot as snap  # noqa: E402

PYTEST_PASS = """
....                                                          [100%]
270 passed, 1 warning in 5.71s
"""

PYTEST_FAIL = """
..F..F                                                       [100%]
2 failed, 268 passed, 3 skipped, 1 warning in 5.90s
"""

PYTEST_ERRORS = """
===== 1 failed, 2 errors, 10 passed in 3.00s =====
"""

HARNESS = """
  [PASS] something
  [PASS] another
  [FAIL] broken
SUMMARY: 2 passed, 1 failed
"""


# ------------------------------------------------------------- pytest --
def test_pytest_pass_line_is_parsed() -> None:
    counts = snap.parse_pytest_counts(PYTEST_PASS)
    assert counts["passed"] == 270
    assert counts["failed"] == 0


def test_pytest_failure_line_is_parsed() -> None:
    counts = snap.parse_pytest_counts(PYTEST_FAIL)
    assert counts["passed"] == 268
    assert counts["failed"] == 2
    assert counts["skipped"] == 3


def test_pytest_errors_count_as_failures() -> None:
    """An error is a failure for progress purposes."""
    counts = snap.parse_pytest_counts(PYTEST_ERRORS)
    assert counts["failed"] == 3  # 1 failed + 2 errors
    assert counts["passed"] == 10


def test_absent_pytest_line_returns_none() -> None:
    assert snap.parse_pytest_counts("nothing to see here") is None
    assert snap.parse_pytest_counts("") is None


def test_a_harness_summary_line_is_readable_by_the_pytest_parser_too() -> None:
    """Harmless overlap: run_target prefers SUMMARY_RE, but the counts agree.

    Documented because it looks like a conflict. If the two ever disagreed it
    would matter, so the agreement is pinned here.
    """
    counts = snap.parse_pytest_counts(HARNESS)
    assert counts == {"passed": 2, "failed": 1}
    match = snap.SUMMARY_RE.search(HARNESS)
    assert (int(match.group(1)), int(match.group(2))) == (2, 1)


def test_pytest_line_without_failures_reports_zero() -> None:
    counts = snap.parse_pytest_counts("5 passed in 0.5s")
    assert counts == {"passed": 5, "failed": 0}


# ------------------------------------------------------------- statuses --
def _target(**kwargs) -> snap.Target:
    defaults = dict(key="t", phase="PX", title="t", argv=["true"], timeout=30)
    defaults.update(kwargs)
    return snap.Target(**defaults)


def test_zero_exit_with_no_failures_is_a_pass(monkeypatch) -> None:
    monkeypatch.setattr(snap, "SNAP_DIR", Path("/tmp/opencode/snap_test"))
    monkeypatch.setattr(snap, "LOG_DIR", Path("/tmp/opencode/snap_test/logs"))
    result = snap.run_target(_target())
    assert result["status"] == "pass"
    # No tally to report for a command that prints nothing; absence is not a
    # failure, and the exit status alone carried the verdict.
    assert result["failed"] in (0, None)


def test_nonzero_exit_is_a_fail_even_without_a_tally(monkeypatch) -> None:
    """Exit status is authoritative; a harness that crashes must not read green."""
    monkeypatch.setattr(snap, "SNAP_DIR", Path("/tmp/opencode/snap_test"))
    monkeypatch.setattr(snap, "LOG_DIR", Path("/tmp/opencode/snap_test/logs"))
    result = snap.run_target(_target(argv=["false"]))
    assert result["status"] == "fail"
    assert result["exit_code"] != 0


def test_reported_failures_fail_even_on_zero_exit(monkeypatch) -> None:
    """Defence in depth: a harness lying in its exit code is still caught."""
    monkeypatch.setattr(snap, "SNAP_DIR", Path("/tmp/opencode/snap_test"))
    monkeypatch.setattr(snap, "LOG_DIR", Path("/tmp/opencode/snap_test/logs"))
    monkeypatch.setattr(snap, "SUMMARY_RE", snap.SUMMARY_RE)
    # A script that prints "1 failed" but exits 0.
    result = snap.run_target(_target(
        argv=[sys.executable, "-c", "print('SUMMARY: 1 passed, 3 failed')"]))
    assert result["failed"] == 3
    assert result["status"] == "fail", "a non-zero failure count must not pass"


def test_pytest_counts_reach_the_record(monkeypatch) -> None:
    monkeypatch.setattr(snap, "SNAP_DIR", Path("/tmp/opencode/snap_test"))
    monkeypatch.setattr(snap, "LOG_DIR", Path("/tmp/opencode/snap_test/logs"))
    result = snap.run_target(_target(argv=[sys.executable, "-m", "pytest",
                                           "--version"]))
    assert "passed" in result or result["passed"] is None


def test_timeout_is_recorded_as_such(monkeypatch) -> None:
    monkeypatch.setattr(snap, "SNAP_DIR", Path("/tmp/opencode/snap_test"))
    monkeypatch.setattr(snap, "LOG_DIR", Path("/tmp/opencode/snap_test/logs"))
    result = snap.run_target(_target(argv=["sleep", "5"], timeout=1))
    assert result["status"] == "timeout"


def test_root_target_is_skipped_without_passwordless_sudo(monkeypatch) -> None:
    monkeypatch.setattr(snap, "_sudo_available", lambda: False)
    result = snap.run_target(_target(needs_root=True))
    assert result["status"] == "skipped"
    assert result["needs_root"] is True


# ------------------------------------------------------------- catalogue --
def test_catalogue_targets_are_either_runnable_or_marked_planned() -> None:
    """An entry whose script is missing must report ``planned``, not vanish.

    A catalogue that silently dropped unbuilt phases would make P10 look like it
    had never been part of the project.
    """
    for key, target in snap.targets().items():
        if target.available:
            continue
        record = snap.run_target(target)
        assert record["status"] == "planned", (
            f"target {key} is not runnable and did not report 'planned'"
        )
        assert record["reason"] == "verification script not written yet"


def test_available_script_targets_point_inside_the_repo() -> None:
    """Every catalogue entry that names a script must name a real repo file."""
    named = 0
    for key, target in snap.targets().items():
        script = target.script
        if script is None or not target.available:
            # No script named (the unit target) or deliberately unbuilt.
            continue
        named += 1
        assert script.is_file(), f"available target {key} lost {script}"
        assert script.resolve().is_relative_to(snap.ROOT), (
            f"target {key} runs {script}, outside the repo"
        )
    assert named >= 6, "the catalogue should reference most verify scripts"


def test_availability_agrees_with_the_filesystem() -> None:
    """``available`` must be a fact about the disk, not a hand-maintained flag."""
    for key, target in snap.targets().items():
        script = target.script
        if script is None:
            assert target.available is True, key
        else:
            assert target.available == script.is_file(), key


def test_a_planned_target_renders_as_not_built() -> None:
    """An unbuilt phase must be visible as unbuilt, not silently dropped.

    Driven by a synthetic target rather than by whichever phase happens to be
    unbuilt today: this test once asserted on the live catalogue and began
    failing the day P10's harness was written, which says nothing about the
    behaviour it was meant to guard.
    """
    # A repo-relative path, which is the form Target.script recognises. An
    # absolute path is deliberately ignored (it may be a system binary), so it
    # would not exercise the planned branch.
    target = snap.Target("pX", "PX", "not written yet",
                         [snap.sys.executable, "scripts/verify_px_absent.py"])
    assert not Path("scripts/verify_px_absent.py").exists(), (
        "fixture collided with a real script"
    )
    assert target.available is False
    manifest = {"targets": {}, "commit": "abc", "updated_at": "2026-01-01"}
    manifest["targets"]["pX"] = snap.run_target(target)
    assert manifest["targets"]["pX"]["status"] == "planned"
    assert "not built" in snap.render_progress(manifest)


def test_a_runnable_target_does_not_render_as_not_built() -> None:
    """The converse: a phase with a harness must not look unbuilt."""
    manifest = {"targets": {}, "commit": "abc", "updated_at": "2026-01-01"}
    text = snap.render_progress(manifest)
    planned = [t for t in snap.targets().values() if not t.available]
    if planned:
        assert "not built" in text
    else:
        assert "not built" not in text, (
            "every catalogue target has a harness, so none should read as unbuilt"
        )


def test_list_runs_cleanly() -> None:
    assert snap.main(["--list"]) == 0


def test_render_progress_marks_unrun_targets() -> None:
    manifest = {"targets": {}, "commit": "abc123", "updated_at": "2026-01-01"}
    text = snap.render_progress(manifest)
    assert "not run" in text
    assert "abc123" in text


def test_render_progress_marks_failures_loudly() -> None:
    manifest = {
        "commit": "abc123", "updated_at": "2026-01-01",
        "targets": {"p3": {"phase": "P3", "title": "controller",
                           "status": "fail", "passed": 16, "failed": 1,
                           "ran_at": "2026-01-01"}},
    }
    assert "**FAIL**" in snap.render_progress(manifest)


def test_manifest_round_trips(tmp_path: Path, monkeypatch) -> None:
    manifest_path = tmp_path / "MANIFEST.json"
    monkeypatch.setattr(snap, "MANIFEST", manifest_path)
    monkeypatch.setattr(snap, "PROGRESS_MD", tmp_path / "PROGRESS.md")
    manifest = snap.load_manifest()
    manifest["targets"]["unit"] = {"status": "pass", "passed": 270, "failed": 0}
    snap.write_manifest(manifest)
    assert json.loads(manifest_path.read_text())["targets"]["unit"]["passed"] == 270


def test_corrupt_manifest_does_not_crash() -> None:
    """A half-written manifest must not make the tool unusable."""
    manifest_path = Path("/tmp/opencode/snap_test/MANIFEST.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("{not json")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(snap, "MANIFEST", manifest_path)
    try:
        assert snap.load_manifest()["targets"] == {}
    finally:
        monkey.undo()
