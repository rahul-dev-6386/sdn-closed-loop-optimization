"""Structured, timestamped logging for the SDN project (design rule 9).

Two sinks are provided:

* a human-readable **console/file** stream using the format configured in
  ``config.yaml`` (``logging.fmt`` / ``logging.datefmt``), and
* a **JSON Lines** event stream (``logging.events_jsonl``) that the P12 API and
  dashboard read back for congestion events and path changes.

The JSONL stream is append-only and flushed after every record, so a consumer
tailing the file sees events as they happen and a crash loses nothing.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

DEFAULT_FMT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-22s %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

#: Event kinds emitted to the JSONL stream. Kept as constants so the dashboard
#: and analysis scripts can rely on a closed set.
EVENT_CONGESTION = "congestion"
EVENT_PREDICTION = "prediction"
EVENT_REROUTE = "reroute"
EVENT_PATH_CHANGE = "path_change"
EVENT_TELEMETRY_STALL = "telemetry_stall"
EVENT_FALLBACK = "fallback"

_CONFIGURED = False


def setup_logging(
    config: Any = None, *, name: str = "sdn", force: bool = False
) -> logging.Logger:
    """Configure the root logger from ``config.yaml`` and return a child logger.

    Parameters
    ----------
    config:
        A :class:`common.config.Config`. When omitted, ``config.yaml`` is
        loaded automatically.
    name:
        Logger name to return, e.g. ``"telemetry"``.
    force:
        Reconfigure even if logging was already set up. Useful in tests.

    Returns
    -------
    logging.Logger
        The requested named child logger.
    """
    global _CONFIGURED
    if config is None:
        from common.config import load_config

        config = load_config()

    if _CONFIGURED and not force:
        return logging.getLogger(name)

    level_name = str(config.get("logging.level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = config.get("logging.fmt", DEFAULT_FMT)
    datefmt = config.get("logging.datefmt", DEFAULT_DATEFMT)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(fmt=fmt, datefmt=datefmt)

    if bool(config.get("logging.console", True)):
        console = logging.StreamHandler(stream=sys.stdout)
        console.setLevel(level)
        console.setFormatter(formatter)
        root.addHandler(console)

    log_file = config.get("logging.file")
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)) or ".", exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(formatter)
        root.addHandler(handler)

    # NOTE: the JSONL event stream is NOT produced by a logging handler. It is
    # written directly by EventLog.emit, so it stays independent of the console
    # level and never drowns in DEBUG chatter. No handler is added here.

    # Per-module overrides for debugging.
    for module in config.get("logging.debug_modules", []) or []:
        logging.getLogger(str(module)).setLevel(logging.DEBUG)

    _CONFIGURED = True
    return logging.getLogger(name)


class EventLog:
    """Append-only JSON Lines writer for controller and telemetry events.

    Parameters
    ----------
    path:
        Destination file. Parent directories are created.
    clock:
        Injectable time function returning epoch seconds, so tests can make
        timestamps deterministic.

    Notes
    -----
    Every record carries a monotonic ``ts`` and a wall-clock ``wall_ts``. The
    monotonic value is what lead-time and recovery-time calculations use, since
    it is immune to NTP steps; ``wall_ts`` is only for human display.
    """

    def __init__(self, path: str, clock: Any = time.time) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Touch the file so consumers can tail it before the first event.
        with open(path, "a", encoding="utf-8"):
            pass

    def emit(self, kind: str, **fields: Any) -> Dict[str, Any]:
        """Write one event and return the record that was written.

        Parameters
        ----------
        kind:
            One of the ``EVENT_*`` constants.
        **fields:
            Arbitrary JSON-serialisable payload, e.g. ``link="s1--s2"``.
        """
        now = self._clock()
        record: Dict[str, Any] = {
            "ts": time.monotonic(),
            "wall_ts": now,
            "wall_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "kind": kind,
        }
        record.update(fields)
        line = json.dumps(record, sort_keys=True, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        return record

    def read_all(self) -> list:
        """Return every event written so far (used by tests and P11/P12)."""
        if not os.path.isfile(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


def get_event_log(config: Any = None, *, force: bool = False) -> EventLog:
    """Build (or reuse) the :class:`EventLog` described by ``config.yaml``."""
    if config is None:
        from common.config import load_config

        config = load_config()
    path = config.get("logging.events_jsonl", "data/logs/events.jsonl")
    return EventLog(str(path))


def monotonic_ms() -> float:
    """Monotonic milliseconds, the time base for all rate/lead-time maths."""
    return time.monotonic() * 1000.0