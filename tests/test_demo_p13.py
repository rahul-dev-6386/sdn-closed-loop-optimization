"""Tests for the P13 demonstration.

A demo that can only succeed proves nothing, so the tests focus on the branch
where the run *fails* to show proactivity: the narration has to say so plainly
and return a non-zero exit code rather than describing a success.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.demo import (  # noqa: E402
    Episode,
    build_episode,
    classify_link,
    report_episode,
)


def write_telemetry(path: Path, rows: List[Dict[str, Any]]) -> Path:
    columns = ["timestamp", "src_switch", "dst_switch", "direction",
               "utilization", "is_congested", "predicted_congested"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def row(ts: float, util: float, *, flagged: bool = False,
        src: str = "s1", dst: str = "s2") -> Dict[str, Any]:
    return {
        "timestamp": f"{ts:.2f}", "src_switch": src, "dst_switch": dst,
        "direction": "forward", "utilization": f"{util:.3f}",
        "is_congested": str(util >= 0.80), "predicted_congested": str(flagged),
    }


# ------------------------------------------------------------ path naming --
def test_links_are_labelled_by_the_middle_switch() -> None:
    assert classify_link("s1", "s2") == "via s2"
    assert classify_link("s2", "s4") == "via s2"
    assert classify_link("s1", "s3") == "via s3"
    assert classify_link("s3", "s4") == "via s3"


# --------------------------------------------------------------- episodes --
def test_a_proactive_episode_is_detected(tmp_path: Path) -> None:
    rows = [row(t, 0.5, flagged=(t >= 40.0)) for t in range(0, 60, 2)]
    rows += [row(t, 0.95, flagged=True) for t in range(60, 100, 2)]
    episode = build_episode(write_telemetry(tmp_path / "t.csv", rows))
    assert episode.first_congested_ts == 60.0
    assert episode.first_flagged_ts == 40.0
    assert episode.lead_time_s == pytest.approx(20.0), (
        "positive lead = the flag preceded congestion by 20s"
    )


def test_lead_time_is_negative_when_the_flag_comes_late(tmp_path: Path) -> None:
    rows = [row(t, 0.5) for t in range(0, 40, 2)]
    rows += [row(t, 0.95, flagged=(t >= 60.0)) for t in range(40, 100, 2)]
    episode = build_episode(write_telemetry(tmp_path / "t.csv", rows))
    assert episode.lead_time_s < 0, "the flag trailed congestion"


def test_episode_of_an_empty_file_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "t.csv"
    path.write_text("timestamp,src_switch,dst_switch\n")
    assert build_episode(path).n_congested == 0


def test_episode_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert build_episode(tmp_path / "absent.csv").n_congested == 0


def test_congestion_is_recomputed_when_unlabelled(tmp_path: Path) -> None:
    """Works against an unlabelled CSV, which is what the demo produces."""
    rows = [row(t, 0.95) for t in range(0, 20, 2)]
    for r in rows:
        r["is_congested"] = ""
    episode = build_episode(write_telemetry(tmp_path / "t.csv", rows))
    assert episode.n_congested == 10


def test_utilisation_is_bucketed_by_path(tmp_path: Path) -> None:
    rows = [row(0.0, 0.3, src="s1", dst="s2")]
    rows += [row(2.0, 0.9, src="s1", dst="s3")]
    episode = build_episode(write_telemetry(tmp_path / "t.csv", rows))
    assert set(episode.util_by_path) == {"via s2", "via s3"}


def test_peak_utilisation_is_tracked(tmp_path: Path) -> None:
    rows = [row(0.0, 0.99), row(2.0, 0.85), row(4.0, 0.40)]
    assert build_episode(write_telemetry(tmp_path / "t.csv", rows)).peak_utilization \
        == pytest.approx(0.99)


def test_junk_cells_are_skipped_not_fatal(tmp_path: Path) -> None:
    rows = [row(0.0, 0.5), {"timestamp": "", "src_switch": "s1",
                             "dst_switch": "s2", "direction": "forward",
                             "utilization": "bad", "is_congested": "",
                             "predicted_congested": ""}]
    episode = build_episode(write_telemetry(tmp_path / "t.csv", rows))
    assert episode.n_congested == 0


# ------------------------------------------------------------- narration --
def test_a_proactive_run_exits_zero_and_says_so(capsys) -> None:
    episode = Episode(first_flagged_ts=40.0, first_congested_ts=60.0,
                      n_flagged=5, n_congested=5, peak_utilization=0.95)
    assert report_episode(episode, "ml_aware") == 0
    assert "led congestion by 20.0s" in capsys.readouterr().out


def test_a_reactive_run_exits_nonzero(capsys) -> None:
    """The control case must not be reported as a success."""
    episode = Episode(first_flagged_ts=90.0, first_congested_ts=60.0,
                      n_flagged=3, n_congested=5, peak_utilization=0.95)
    assert report_episode(episode, "ml_aware") == 1
    out = capsys.readouterr().out
    assert "AFTER congestion" in out
    assert "reaction, not anticipation" in out


def test_a_run_with_no_flags_exits_nonzero(capsys) -> None:
    episode = Episode(first_flagged_ts=None, first_congested_ts=60.0,
                      n_flagged=0, n_congested=5, peak_utilization=0.95)
    assert report_episode(episode, "ml_aware") == 1
    assert "never flagged" in capsys.readouterr().out


def test_a_run_with_no_congestion_exits_two(capsys) -> None:
    """Distinguishable from "ran but did not demonstrate the claim"."""
    episode = Episode(first_flagged_ts=10.0, first_congested_ts=None,
                      n_flagged=3, n_congested=0)
    assert report_episode(episode, "ml_aware") == 2
    assert "cannot be assessed" in capsys.readouterr().out


def test_an_empty_run_exits_two() -> None:
    assert report_episode(Episode(), "ml_aware") == 2


def test_narration_reports_both_path_peaks(capsys) -> None:
    episode = Episode(
        first_flagged_ts=10.0, first_congested_ts=50.0, n_flagged=4,
        n_congested=4, peak_utilization=0.97,
        util_by_path={"via s2": [0.2, 0.9], "via s3": [0.3, 0.4]},
    )
    report_episode(episode, "ml_aware")
    out = capsys.readouterr().out
    assert "via s2" in out and "via s3" in out
