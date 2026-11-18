"""Routing constraint file — shared between the API and the Ryu controller.

The agent is a side process; it cannot call Python objects on the
controller. It CAN write one small well-formed JSON file, and the
controller's decision loop reads it every tick (cheap: stat + parse).

This is the explicit seam for the design rule:

    the agent *reports* what it sees; the controller *decides* what to do.

Schema (``routing_constraints.json``)::

    {
      "block_links": ["s1--s2"],
      "valid_until_epoch": 1794889000,
      "reason": "agent-reported congestion",
      "pinned_by": "agent"
    }

A link key is the CPU-canonical ``min--max`` form used by telemetry
and the catalog: ``"s1--s2"`` for the physical link s1<->s2, regardless of
which direction is congested. Expired constraints are ignored by the reader.

"""
from __future__ import annotations

import json
import os
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("routing.constraints")

DEFAULT_CONSTRAINTS_PATH = str(Path(__file__).resolve().parents[1] / "data" / "routing_constraints.json")


def canonical_pair(a: str, b: str) -> str:
    return f"{min(a, b)}--{max(a, b)}"


def canonical_link_key(src: str, dst: str) -> str:
    return canonical_pair(src, dst)


def read_constraints(path: str | os.PathLike[str] = DEFAULT_CONSTRAINTS_PATH) -> Dict[str, Any]:
    """Read the constraint file. Missing/corrupt file means 'no constraint'."""
    p = Path(path)
    if not p.is_file():
        return {"block_links": [], "valid_until_epoch": 0, "reason": ""}
    try:
        with p.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception as exc:
        LOGGER.warning("routing constraint file unreadable (%s): treating as unconstrained", exc)
        return {"block_links": [], "valid_until_epoch": 0, "reason": ""}

    block_links = [str(x) for x in (raw.get("block_links") or [])]
    valid_until = float(raw.get("valid_until_epoch") or 0)
    import time
    if valid_until and time.time() > valid_until:
        # Expired — treat as absent so the controller reverts to normal costing
        return {"block_links": [], "valid_until_epoch": 0, "reason": "expired"}

    return {
        "block_links": block_links,
        "valid_until_epoch": valid_until,
        "reason": str(raw.get("reason") or ""),
        "pinned_by": str(raw.get("pinned_by") or ""),
    }


def write_constraints(
    path: str | os.PathLike[str],
    block_links: List[str],
    ttl_s: float,
    reason: str = "",
    pinned_by: str = "agent",
) -> Dict[str, Any]:
    """Write/replace the constraint file. Returns the written payload."""
    valid_until = 0.0
    if ttl_s and ttl_s > 0:
        import time
        valid_until = time.time() + float(ttl_s)
    payload = {
        "block_links": [str(x) for x in block_links],
        "valid_until_epoch": valid_until,
        "reason": str(reason),
        "pinned_by": str(pinned_by),
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(tmp, p)
    return payload


def clear_constraints(path: str | os.PathLike[str] = DEFAULT_CONSTRAINTS_PATH) -> Dict[str, Any]:
    return write_constraints(path, block_links=[], ttl_s=0, reason="manual-clear", pinned_by="api")


def blocked_pairs(constraints: Dict[str, Any]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for link in constraints.get("block_links") or []:
        parts = str(link).split("--", 1)
        if len(parts) == 2:
            out.append((parts[0], parts[1]))
    return out
