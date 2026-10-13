"""Tests for the progress image.

The image is a summary, and a summary that overstates is worse than none. These
tests pin the rule that matters: an unverified phase must never render as green.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import snapshot as snap  # noqa: E402
from scripts import snapshot_image as image  # noqa: E402


def write_manifest(tmp_path: Path, targets: Dict[str, Dict[str, Any]]) -> None:
    """Write a MANIFEST.json for the renderer to read."""
    path = tmp_path / "MANIFEST.json"
    path.write_text(json.dumps({"targets": targets, "commit": "abc",
                                "updated_at": "2026-01-01T00:00:00+00:00"}))


def test_every_catalogue_target_gets_a_tile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(image, "MANIFEST", tmp_path / "absent.json")
    monkeypatch.setattr(image, "load_history", list)
    tiles = image.collect_tiles()
    assert len(tiles) == len(snap.targets())


def test_a_passing_target_reads_as_pass(monkeypatch, tmp_path: Path) -> None:
    write_manifest(tmp_path, {"p8": {"status": "pass", "passed": 28,
                                     "failed": 0}})
    monkeypatch.setattr(image, "MANIFEST", tmp_path / "MANIFEST.json")
    monkeypatch.setattr(image, "load_history", list)
    tile = next(t for t in image.collect_tiles() if t["key"] == "p8")
    assert tile["status"] == "pass"
    assert tile["passed"] == 28


def test_a_failing_target_is_distinguishable_from_a_passing_one(
    monkeypatch, tmp_path: Path,
) -> None:
    write_manifest(tmp_path, {"p7": {"status": "fail", "passed": 16,
                                     "failed": 1}})
    monkeypatch.setattr(image, "MANIFEST", tmp_path / "MANIFEST.json")
    monkeypatch.setattr(image, "load_history", list)
    tile = next(t for t in image.collect_tiles() if t["key"] == "p7")
    assert image._colour_for(tile["status"]) != image._colour_for("pass")
    assert image._colour_for(tile["status"])[1] == "FAIL"


def test_an_unrun_phase_is_not_green(monkeypatch, tmp_path: Path) -> None:
    """The core honesty property of the picture."""
    write_manifest(tmp_path, {})
    monkeypatch.setattr(image, "MANIFEST", tmp_path / "MANIFEST.json")
    monkeypatch.setattr(image, "load_history", list)
    unrun = [t for t in image.collect_tiles() if t["status"] == "not run"]
    assert unrun, "phases with no recorded run must appear"
    for tile in unrun:
        assert image._colour_for(tile["status"])[0] == "#5a5a5a"


def test_a_planned_target_reads_as_not_built(monkeypatch, tmp_path: Path) -> None:
    write_manifest(tmp_path, {"p10": {"status": "planned",
                                      "reason": "script not written yet"}})
    monkeypatch.setattr(image, "MANIFEST", tmp_path / "MANIFEST.json")
    monkeypatch.setattr(image, "load_history", list)
    tile = next(t for t in image.collect_tiles() if t["key"] == "p10")
    assert image._colour_for(tile["status"])[1] == "not built"


def test_a_corrupt_manifest_does_not_break_rendering(
    monkeypatch, tmp_path: Path,
) -> None:
    path = tmp_path / "MANIFEST.json"
    path.write_text("{not json")
    monkeypatch.setattr(image, "MANIFEST", path)
    monkeypatch.setattr(image, "load_history", list)
    assert image.collect_tiles(), "must still produce tiles"


def test_counts_sum_only_measured_targets(monkeypatch, tmp_path: Path) -> None:
    write_manifest(tmp_path, {})
    monkeypatch.setattr(image, "MANIFEST", tmp_path / "MANIFEST.json")
    monkeypatch.setattr(image, "load_history", list)
    passed, failed = image._fmt_count(image.collect_tiles())
    assert passed == 0 and failed == 0, "unmeasured phases contribute nothing"


def test_rendering_writes_a_png(monkeypatch, tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    write_manifest(tmp_path, {"p8": {"status": "pass", "passed": 28,
                                     "failed": 0}})
    monkeypatch.setattr(image, "MANIFEST", tmp_path / "MANIFEST.json")
    monkeypatch.setattr(image, "load_history", lambda: [])
    out = image.render(tmp_path / "progress.png", dpi=72)
    assert out is not None and out.is_file()
    assert out.stat().st_size > 5000, "a blank canvas is not a progress picture"
