#!/usr/bin/env python3
"""Render the snapshot ledger as a progress image.

``results/snapshots/PROGRESS.md`` is precise but not glanceable. This turns the
same data into a single PNG: a tile per phase showing what was measured, whether
it passed, and how many checks that was -- plus a trend line so a reader can see
whether the project is actually improving or just being re-measured.

The image deliberately encodes *measured* state only. A tile is grey when a phase
has no harness yet and amber when a run was skipped or timed out, so the picture
cannot flatter: an unverified phase never looks green, and a phase whose evidence
is stale is visible as such.

Usage
-----
    python scripts/snapshot_image.py
    python scripts/snapshot_image.py --out results/snapshots/progress.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.snapshot import (  # noqa: E402
    MANIFEST,
    load_history,
    targets,
)

#: Status -> (face colour, label). Chosen so the three states a reader must
#: distinguish -- pass, not-yet-verified, and broken -- are separable at a glance
#: and remain distinguishable in greyscale via the label text.
STATUS_STYLE: Dict[str, Tuple[str, str]] = {
    "pass":     ("#2f7d4f", "pass"),
    "fail":     ("#b3352e", "FAIL"),
    "timeout":  ("#b3352e", "TIMEOUT"),
    "skipped":  ("#b8860b", "skipped"),
    "planned":  ("#5a5a5a", "not built"),
    "not run":  ("#5a5a5a", "not run"),
}

#: Tile order. Phases first in numeric order, then the cross-cutting suites.
TILE_ORDER = ["p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "p9",
              "p10", "p11", "unit"]

TITLES = {
    "p1": "Environment", "p2": "Topology", "p3": "Controller",
    "p4": "Discovery", "p5": "Telemetry", "p6": "Dataset",
    "p7": "ML training", "p8": "Inference", "p9": "Routing",
    "p10": "Experiments", "p11": "Analysis", "unit": "Unit suite",
}


def _colour_for(status: str) -> Tuple[str, str]:
    """Face colour and label for a status, defaulting to 'not run'."""
    return STATUS_STYLE.get(status, STATUS_STYLE["not run"])


def collect_tiles() -> List[Dict[str, Any]]:
    """Build one record per catalogue target, in display order."""
    catalogue = targets()
    manifest: Dict[str, Any] = {}
    if MANIFEST.is_file():
        import json

        try:
            manifest = json.loads(MANIFEST.read_text())
        except json.JSONDecodeError:
            manifest = {}
    recorded = manifest.get("targets", {})

    tiles: List[Dict[str, Any]] = []
    ordered = [k for k in TILE_ORDER if k in catalogue]
    ordered += [k for k in sorted(catalogue) if k not in ordered]
    for key in ordered:
        target = catalogue[key]
        record = recorded.get(key)
        if record is None:
            status = "not run" if target.available else "planned"
            record = {"status": status, "passed": None, "failed": None}
        tiles.append({
            "key": key,
            "phase": target.phase,
            "title": TITLES.get(key, target.title),
            "subtitle": target.title,
            "status": record.get("status", "not run"),
            "passed": record.get("passed"),
            "failed": record.get("failed"),
            "ran_at": record.get("ran_at"),
        })
    return tiles


def _fmt_count(tiles: List[Dict[str, Any]]) -> Tuple[int, int]:
    """Total checks passed and failed across tiles that reported a tally."""
    passed = failed = 0
    for tile in tiles:
        if tile["passed"] is not None:
            passed += int(tile["passed"])
        if tile["failed"] is not None:
            failed += int(tile["failed"])
    return passed, failed


def render(out_path: Path, dpi: int = 130) -> Optional[Path]:
    """Render the progress dashboard to ``out_path``.

    Returns the path written, or ``None`` if matplotlib is unavailable -- a
    missing plotting backend must not break the snapshot pipeline, since the
    text ledger is the authoritative artefact and the image is a convenience.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"skipping progress image: {exc}", file=sys.stderr)
        return None

    tiles = collect_tiles()
    total_pass, total_fail = _fmt_count(tiles)
    history = load_history()

    columns = 4
    rows = (len(tiles) + columns - 1) // columns
    fig = plt.figure(figsize=(13.5, 2.9 * rows + 2.2))
    grid = fig.add_gridspec(rows + 1, columns,
                            height_ratios=[1] * rows + [0.75],
                            hspace=0.30, wspace=0.20,
                            left=0.035, right=0.975, top=0.855, bottom=0.055)

    for index, tile in enumerate(tiles):
        row, col = divmod(index, columns)
        ax = fig.add_subplot(grid[row, col])
        colour, label = _colour_for(tile["status"])

        # A green fill only for a verified pass; everything else is muted, so an
        # unverified phase can never be mistaken for a working one.
        face = colour if tile["status"] == "pass" else "#efefef"
        edge = colour
        ax.add_patch(FancyBboxPatch(
            (0.0, 0.0), 1.0, 1.0,
            boxstyle="round,pad=0.012,rounding_size=0.045",
            transform=ax.transAxes, facecolor=face, edgecolor=edge,
            linewidth=2.4 if tile["status"] in {"fail", "timeout"} else 1.5,
            zorder=1,
        ))

        text_colour = "white" if tile["status"] == "pass" else "#1a1a1a"
        sub_colour = "#d8f0dd" if tile["status"] == "pass" else "#4a4a4a"

        heading = tile["phase"] if tile["phase"] not in {"--", ""} else "TEST"
        ax.text(0.5, 0.80, heading, transform=ax.transAxes,
                ha="center", va="center", fontsize=17, fontweight="bold",
                color=text_colour, zorder=2)
        ax.text(0.5, 0.56, tile["title"], transform=ax.transAxes,
                ha="center", va="center", fontsize=10.5,
                color=text_colour, zorder=2)

        if tile["passed"] is not None and tile["failed"] is not None:
            tally = (f"{tile['passed']} passed"
                     + (f", {tile['failed']} failed" if tile["failed"] else ""))
            ax.text(0.5, 0.33, tally, transform=ax.transAxes, ha="center",
                    va="center", fontsize=9.5, color=sub_colour, zorder=2)
        else:
            ax.text(0.5, 0.33, "--", transform=ax.transAxes, ha="center",
                    va="center", fontsize=9.5, color=sub_colour, zorder=2)

        ax.text(0.5, 0.13, label.upper(), transform=ax.transAxes, ha="center",
                va="center", fontsize=8.5, fontweight="bold",
                color=text_colour if tile["status"] == "pass" else colour,
                zorder=2)
        ax.set_axis_off()

    # ------------------------------------------------------------- trend --
    trend_ax = fig.add_subplot(grid[rows, :])
    if len(history) >= 2:
        xs = list(range(len(history)))
        net = [h.get("passing", 0) for h in history]
        checks = [(h.get("passed", 0) + h.get("failed", 0)) for h in history]
        trend_ax.plot(xs, checks, marker="o", color="#2f6f9f", linewidth=2,
                      label="checks recorded")
        trend_ax.plot(xs, net, marker="s", color="#2f7d4f", linewidth=2,
                      label="targets passing")
        trend_ax.set_xlabel("snapshot run")
        trend_ax.set_ylabel("count")
        trend_ax.legend(loc="upper left", fontsize=8, frameon=False)
        trend_ax.grid(alpha=0.3)
    else:
        trend_ax.text(
            0.5, 0.5,
            "trend appears from the second snapshot onward\n"
            "(this is the first recorded run)",
            ha="center", va="center", fontsize=10, color="#666666")
        trend_ax.set_axis_off()

    # ------------------------------------------------------------ header --
    verified = sum(1 for t in tiles if t["status"] == "pass")
    fig.suptitle(
        "AI-Driven SDN Traffic Optimization  --  verified progress",
        fontsize=17, fontweight="bold", y=0.972)
    fig.text(0.5, 0.925,
             f"{verified}/{len(tiles)} targets passing   |   "
             f"{total_pass} checks passed, {total_fail} failed   |   "
             f"{len(history)} snapshot run(s)",
             ha="center", fontsize=10.5, color="#333333")
    fig.text(0.5, 0.895,
             "measured only -- a phase with no passing acceptance run is never "
             "shown as green",
             ha="center", fontsize=8.5, color="#777777", style="italic")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=dpi, facecolor="white")
    import matplotlib.pyplot as _plt
    _plt.close(fig)
    return out_path


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(
        ROOT / "results" / "snapshots" / "progress.png"))
    args = parser.parse_args(argv)

    path = render(Path(args.out))
    if path is None:
        print("progress image not rendered (matplotlib unavailable); "
              "the text ledger in PROGRESS.md is unaffected")
        return 0
    print(f"progress image: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
