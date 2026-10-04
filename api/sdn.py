"""Read-only access to the live SDN project state for the dashboard.

Sources, in order of preference:

* ``config.yaml``      -- authoritative topology and thresholds
* ``data/portmap.json``-- switch/host port adjacency (written by P3)
* ``data/logs/events.jsonl`` -- the controller's JSONL event stream
* ``ovs-ofctl``        -- live flow tables, **only if the API runs as root**

The last one matters: the dashboard must not 500 just because the API was
started without sudo. :func:`read_flows` returns a structured "unavailable"
result instead of raising.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

LOGGER = logging.getLogger("api.sdn")


class ProjectData:
    """Loads and caches project artefacts, refreshing on demand."""

    def __init__(self, config_path: str, port_map_path: str,
                 events_path: str) -> None:
        self.config_path = Path(config_path)
        self.port_map_path = Path(port_map_path)
        self.events_path = Path(events_path)

    # ------------------------------------------------------------ config --
    def config(self) -> Dict[str, Any]:
        """Parsed ``config.yaml`` (empty dict if unreadable)."""
        try:
            with self.config_path.open() as handle:
                return yaml.safe_load(handle) or {}
        except Exception as exc:
            LOGGER.warning("could not read %s: %s", self.config_path, exc)
            return {}

    # ---------------------------------------------------------- topology --
    def topology(self) -> Dict[str, Any]:
        """Nodes and links for the dashboard graph.

        Ports come from ``portmap.json`` when present (P3 side channel); without
        it the graph still renders, just without port numbers.
        """
        cfg = self.config()
        topo = cfg.get("topology", {}) or {}
        hosts = [
            {"name": h["name"], "ip": h["ip"].split("/")[0], "switch": h["switch"]}
            for h in topo.get("hosts", [])
        ]
        switches = [s["name"] for s in topo.get("switches", [])]
        links = [
            {
                "a": link[0] if isinstance(link, (list, tuple)) else link["a"],
                "b": link[1] if isinstance(link, (list, tuple)) else link["b"],
                "bw_mbps": (link.get("bw_mbps")
                            if isinstance(link, dict) else None)
                           or topo.get("link_bw_mbps"),
                "delay_ms": (link.get("delay_ms")
                             if isinstance(link, dict) else None)
                            or topo.get("link_delay_ms"),
            }
            for link in topo.get("links", [])
        ]
        # Host<->switch edges are derived, not declared in `links`.
        for host in hosts:
            links.append({"a": host["switch"], "b": host["name"],
                          "host_link": True})

        return {
            "name": topo.get("name", "topology"),
            "switches": switches,
            "hosts": hosts,
            "links": links,
            "link_bw_mbps": topo.get("link_bw_mbps"),
            "link_delay_ms": topo.get("link_delay_ms"),
            "datapath_type": topo.get("datapath_type"),
            "ports_available": self.port_map_path.is_file(),
        }

    def port_map(self) -> Dict[str, Any]:
        """Raw port map, or ``{}`` when absent."""
        try:
            with self.port_map_path.open() as handle:
                return json.load(handle)
        except Exception:
            return {}

    # ------------------------------------------------------------ events --
    def events(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Tail of the controller's JSONL event stream, newest last."""
        if not self.events_path.is_file():
            return []
        try:
            lines = self.events_path.read_text(errors="replace").splitlines()
        except Exception as exc:
            LOGGER.warning("could not read events: %s", exc)
            return []
        out: List[Dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # ------------------------------------------------------------- flows --
    def flows(self) -> Dict[str, Any]:
        """Live flow tables via ``ovs-ofctl``, if permitted.

        Returns ``{"available": False, "reason": ...}`` when the API lacks the
        privileges to query OVS, so the dashboard can say so plainly instead of
        showing an empty table that looks like a failure.
        """
        if not self.port_map_path.is_file():
            return {"available": False,
                    "reason": "no port map yet (run `make portmap`)"}

        result: Dict[str, Any] = {"available": True, "switches": {}}
        for switch in self.port_map().get("switch_ports", {}):
            proc = subprocess.run(
                ["ovs-ofctl", "-OOpenFlow13", "dump-flows", switch],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if proc.returncode != 0:
                return {
                    "available": False,
                    "reason": (proc.stderr.strip()
                               or f"ovs-ofctl exited {proc.returncode}"),
                }
            entries = [
                line.strip() for line in proc.stdout.splitlines()
                if "cookie" in line
            ]
            result["switches"][switch] = {
                "count": len(entries),
                "forwarding": sum(1 for e in entries if "output:" in e),
                "entries": entries[:40],
            }
        return result


# --------------------------------------------------------------------------- #
# Controller state (lives in the ryu process, so read its artefacts)
# --------------------------------------------------------------------------- #
def controller_health(project: ProjectData) -> Dict[str, Any]:
    """Best-effort liveness signal for the controller.

    The controller runs in a separate ``ryu-manager`` process and does not expose
    a health endpoint until P12 proper. For now we report whether the OpenFlow
    port has an active listener, which is a decent proxy.
    """
    try:
        out = subprocess.run(["ss", "-ltn"], capture_output=True, text=True,
                             timeout=5, check=False).stdout
        listening = any(":6653 " in line for line in out.splitlines())
    except Exception:
        listening = False
    return {
        "listening": listening,
        "events_logged": (
            len(project.events(limit=10_000)) if project.events_path.is_file()
            else 0
        ),
        "checked_at": time.time(),
    }


def predictions_available() -> bool:
    """True once P7 has produced a model artefact."""
    root = Path(__file__).resolve().parents[1]
    return (root / "results" / "model.joblib").is_file()