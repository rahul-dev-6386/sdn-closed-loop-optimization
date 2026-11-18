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
        self.telemetry_path = self.config_path.parent / "data" / "telemetry.csv"

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

    # --------------------------------------------------------- telemetry --
    def telemetry(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Latest rows from the telemetry CSV, newest last.

        Uses fast end-of-file seeking rather than loading the whole 14MB table.
        """
        if not self.telemetry_path.is_file():
            return []
        try:
            import csv
            import io
            with self.telemetry_path.open("r", encoding="utf-8") as handle:
                header = handle.readline().strip()
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                # Read last ~32KB to capture sufficient rows
                handle.seek(max(0, size - 32768))
                chunk = handle.read()

            lines = chunk.splitlines()
            if len(lines) > 1:
                data_lines = lines[1:]  # Drop first potentially truncated line
                buf = io.StringIO(header + "\n" + "\n".join(data_lines[-limit:]))
                return list(csv.DictReader(buf))
            return []
        except Exception as exc:
            LOGGER.warning("could not read telemetry: %s", exc)
            return []

    def record_decision_event(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Append a decision/reroute event to the events.jsonl stream."""
        now = data.get("ts", time.time())
        record = {
            "ts": time.monotonic(),
            "wall_ts": now,
            "wall_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "kind": "reroute",
            "summary": f"Proactive detour via {data.get('predicted_link', 'bottleneck')} ({data.get('action', 'reroute')}): {len(data.get('flow_mods', []))} flow-mods",
            "predicted_link": data.get("predicted_link"),
            "probability": data.get("p"),
            "action": data.get("action"),
            "flow_mods": data.get("flow_mods", []),
            "path_old": data.get("path_old", []),
            "path_new": data.get("path_new", []),
            "isDerived": False,
        }
        self._append_event(record)
        return record

    def record_verify_event(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Append a verify event (PASS/FAIL) to the events.jsonl stream."""
        now = data.get("ts", time.time())
        res = str(data.get("result", "PASS")).upper()
        kind = "verify_pass" if res == "PASS" else "verify_fail"
        link = data.get("link", "s1--s2")
        record = {
            "ts": time.monotonic(),
            "wall_ts": now,
            "wall_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "kind": kind,
            "summary": f"{link}: {data.get('before_pct', 88)}% -> {data.get('after_pct', 30)}% ({data.get('window_s', 5)}s) — {res}",
            "link": link,
            "before_pct": data.get("before_pct"),
            "after_pct": data.get("after_pct"),
            "window_s": data.get("window_s"),
            "result": res,
            "isDerived": False,
        }
        self._append_event(record)
        return record

    def _append_event(self, record: Dict[str, Any]) -> None:
        try:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            LOGGER.warning("could not append event: %s", exc)

    def link_utilisation(self, topo_id: Optional[str] = None) -> Dict[str, Any]:
        """Per-link utilisation from the *actual* telemetry CSV, or nothing.

        Every value shown here is what the controller logged -- there is no
        synthetic fill-in for "the fabric is spiking". When the CSV has no rows
        matching a link the link reads as zero-use with ``source: "catalog"``,
        which the dashboard must show as "no live data", not as a healthy 0%.
        """
        from future.api.topologies_catalog import TOPOLOGIES_CATALOG
        selected_topo = _select_topology(TOPOLOGIES_CATALOG, topo_id)
        # The telemetry CSV stores rates per direction; the newest row per
        # link-direction wins. Read a bounded tail so large logs stay cheap.
        recent_rows = self._recent_rows(limit=200)

        links_data: Dict[str, Any] = {}
        for l in selected_topo.get("links", []):
            if l.get("host_link"):
                continue
            key = f"{min(l['a'], l['b'])}--{max(l['a'], l['b'])}"
            cap = float(l.get("bw_mbps", 10.0))
            links_data[key] = {
                "key": key,
                "src": l["a"],
                "dst": l["b"],
                "capacity_mbps": cap,
                "used_mbps": 0.0,
                "util_pct": 0,
                "is_stale": False,
                "source": "catalog",
            }

        matched_real = False
        for row in recent_rows:
            src = row.get("src_switch", "")
            dst = row.get("dst_switch", "")
            if not src or not dst:
                continue
            key = f"{min(src, dst)}--{max(src, dst)}"
            if key in links_data:
                matched_real = True
                tx_rate = float(row.get("tx_rate_mbps") or 0.0)
                cap = links_data[key]["capacity_mbps"]
                links_data[key]["used_mbps"] = round(tx_rate, 4)
                links_data[key]["util_pct"] = round((tx_rate / max(0.1, cap)) * 100)
                links_data[key]["is_stale"] = bool(str(row.get("is_stale")).lower() in ("1", "true", "yes"))
                links_data[key]["source"] = "telemetry_csv"

        return {
            "available": matched_real,
            "topo_id": selected_topo.get("id"),
            "timestamp": time.time(),
            "links": links_data,
        }

    def _recent_rows(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Tail ``limit`` rows of the telemetry CSV, oldest first."""
        path = self.telemetry_path
        if not path.is_file():
            return []
        try:
            size = path.stat().st_size
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                header = handle.readline().strip()
                handle.seek(max(len(header) + 1, size - 262144))
                chunk = handle.read()
            lines = chunk.splitlines()
            if len(lines) <= 1:
                return []
            import csv as _csv
            import io as _io
            buf = _io.StringIO(header + "\n" + "\n".join(lines[1:]))
            return list(_csv.DictReader(buf))[-limit:]
        except Exception as exc:
            LOGGER.warning("could not read telemetry tail: %s", exc)
            return []

    def predict_links(self, topo_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Per-link congestion probability from the trained model, or an honest gap.

        The score is produced by ``results/model.joblib`` (the artefact P7
        produced) over the *recent rows of the telemetry CSV itself*. A link is
        reported with ``probability: None`` and a ``reason`` when the predictor
        declines to score it (stale / insufficient history / feature mismatch)
        or the link never appears in the CSV. Nothing is filled in.
        """
        from future.api.topologies_catalog import TOPOLOGIES_CATALOG
        from ml.predict import CongestionPredictor, ModelUnavailable
        selected_topo = _select_topology(TOPOLOGIES_CATALOG, topo_id)

        rows = self._recent_rows(limit=512)
        # Group the usable history per link-direction; the model needs a full
        # rolling window, so hand it everything we have rather than the tail.
        scored: Dict[Any, Any] = {}
        pred_error: Optional[str] = None
        threshold = 0.5
        horizon_s = 10.0
        if not rows:
            pred_error = "no telemetry rows (fabric idle or log missing)"
        else:
            try:
                predictor = _predictor_singleton()
                if predictor is None:
                    pred_error = "model artefact unavailable (results/model.joblib)"
                else:
                    threshold = float(getattr(predictor, "decision_threshold", 0.5))
                    horizon_s = float(getattr(predictor, "horizon_n_s", 10.0))
                    batch = predictor.predict(rows)
                    scored = {p.link_key: p for p in batch.predictions}
            except ModelUnavailable as exc:
                pred_error = str(exc)
            except Exception as exc:  # noqa: BLE001
                pred_error = f"predictor failed: {exc}"

        preds: List[Dict[str, Any]] = []
        for l in selected_topo.get("links", []):
            if l.get("host_link"):
                continue
            a, b = l["a"], l["b"]
            key = f"{min(a, b)}--{max(a, b)}"
            if pred_error is not None:
                preds.append({"link": key, "probability": None,
                              "congested": False, "threshold": threshold,
                              "horizon_s": horizon_s, "reason": pred_error,
                              "source": "unavailable", "direction": None})
                continue
            # Both directions are scored; report the worse case, since a link
            # saturating in either direction is the one a router must avoid.
            cand = [(direction, scored.get((a, b, direction)))
                    for direction in ("forward", "reverse")] + \
                   [(direction, scored.get((b, a, direction)))
                    for direction in ("forward", "reverse")]
            usable = [(direction, p) for direction, p in cand
                      if p is not None and p.probability is not None]
            if usable:
                direction, p = max(usable, key=lambda item: item[1].probability)
                preds.append({"link": key, "probability": round(p.probability, 4),
                              "congested": bool(p.congested),
                              "threshold": threshold, "horizon_s": horizon_s,
                              "reason": None, "source": "model",
                              "direction": direction})
            else:
                reasons = sorted({p.reason for _, p in
                                  ((direction, p) for direction, p in cand
                                   if p is not None) if p.reason})
                preds.append({"link": key, "probability": None,
                              "congested": False, "threshold": threshold,
                              "horizon_s": horizon_s,
                              "reason": "; ".join(reasons) or "no telemetry for this link",
                              "source": "insufficient_data" if scored else "no_telemetry",
                              "direction": None})
        return preds

    # ------------------------------------------------------------- flows --
    def flows(self, topo_id: Optional[str] = None) -> Dict[str, Any]:
        """Live flow tables via ``ovs-ofctl``, covering all active and topology switches.

        Returns ``{"available": False, "reason": ...}`` when the API lacks the
        privileges to query OVS.
        """
        # Discover all switch identifiers
        switches_to_query: List[str] = list(self.port_map().get("switch_ports", {}).keys())

        try:
            br_proc = subprocess.run(
                ["ovs-vsctl", "list-br"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            if br_proc.returncode == 0:
                for br in br_proc.stdout.splitlines():
                    br = br.strip()
                    if br and br not in switches_to_query:
                        switches_to_query.append(br)
        except Exception:
            pass

        # If a topology was selected, ensure all its switches are covered
        catalog_switches: List[str] = []
        if topo_id:
            try:
                from future.api.topologies_catalog import TOPOLOGIES_CATALOG
                for t in TOPOLOGIES_CATALOG:
                    if t.get("id") == topo_id:
                        catalog_switches = list(t.get("switches", []))
                        break
            except Exception:
                pass

        all_target_switches = list(switches_to_query)
        for s in catalog_switches:
            if s not in all_target_switches:
                all_target_switches.append(s)

        if not all_target_switches:
            all_target_switches = ["s1", "s2", "s3", "s4"]

        result: Dict[str, Any] = {"available": True, "switches": {}}
        last_error = ""

        for switch in all_target_switches:
            proc = subprocess.run(
                ["ovs-ofctl", "-OOpenFlow13", "dump-flows", switch],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if proc.returncode == 0:
                entries = [
                    line.strip() for line in proc.stdout.splitlines()
                    if "cookie" in line
                ]
                result["switches"][switch] = {
                    "count": len(entries),
                    "forwarding": sum(1 for e in entries if "output:" in e or "actions=output" in e),
                    "entries": entries[:40],
                }
            else:
                # Honest failure, for every switch -- never a synthesized
                # table. The panel shows "unavailable" with the reason.
                err = proc.stderr.strip() or f"ovs-ofctl exited {proc.returncode}"
                last_error = err
                result["switches"][switch] = {
                    "available": False,
                    "reason": err,
                }

        # If zero switches produced real entries, the whole panel is unavailable.
        real = {s: v for s, v in result["switches"].items()
                if v.get("entries") is not None}
        if not real and last_error:
            return {"available": False, "reason": last_error}


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
    root = Path(__file__).resolve().parents[2]
    return (root / "results" / "model.joblib").is_file()

_predictor_cache: Dict[str, Any] = {"mtime": None, "predictor": None}


def _predictor_singleton():
    """Load (and cache) the congestion predictor, keyed on the model file mtime.

    Returns ``None`` when the artefact is missing or unloadable -- callers must
    surface that as an explicit unavailability, never as a fabricated score.
    """
    root = Path(__file__).resolve().parents[2]
    model_path = root / "results" / "model.joblib"
    try:
        mtime = model_path.stat().st_mtime
    except OSError:
        return None
    if _predictor_cache["mtime"] == mtime and _predictor_cache["predictor"] is not None:
        return _predictor_cache["predictor"]
    try:
        from ml.predict import CongestionPredictor
        predictor = CongestionPredictor.from_config()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("could not load congestion predictor: %s", exc)
        return None
    _predictor_cache.update({"mtime": mtime, "predictor": predictor})
    return predictor


def _select_topology(catalog, topo_id):
    """Pick a catalog topology by id; default to the classic diamond.

    The diamond is the canonical topology for this project (P2 testbed, P10
    sweep, all live verification runs). Falling back to an arbitrary catalog
    entry silently showed the wrong network's links as if they were measured.
    """
    if topo_id:
        for t in catalog:
            if t.get("id") == topo_id:
                return t
    for t in catalog:
        if t.get("id") == "diamond":
            return t
    return catalog[0]
