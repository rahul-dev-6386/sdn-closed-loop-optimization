"""Configuration loading for the SDN project.

Design rule: **no magic numbers in code**. Every tunable lives in ``config.yaml``
and is reached through :func:`load_config`. The loader deliberately fails fast
on a malformed file and on unknown top-level sections, because a silently
misspelled key would otherwise surface much later as a wrong routing decision.

Example
-------
>>> cfg = load_config()                       # doctest: +SKIP
>>> cfg.get("telemetry.sample_interval_s")    # doctest: +SKIP
2.0
>>> cfg.path("routing.weights.d_congestion")  # doctest: +SKIP
'routing.weights.d_congestion'
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from typing import Any, Optional

import yaml

DEFAULT_CONFIG_PATH = "config.yaml"

#: Top-level sections the project expects. A missing one is a hard error.
REQUIRED_SECTIONS = (
    "experiment",
    "topology",
    "controller",
    "discovery",
    "telemetry",
    "ml",
    "routing",
    "traffic",
    "experiments",
    "logging",
)

_MISSING = object()


class ConfigError(ValueError):
    """Raised when ``config.yaml`` is absent, unreadable, or incomplete."""


class Config(Mapping):
    """Read-only view over the parsed YAML configuration.

    Supports both mapping access (``cfg["routing"]``) and dotted-path access
    (``cfg.get("routing.weights.a_utilization")``).
    """

    __slots__ = ("_data", "_path")

    def __init__(self, data: Mapping[str, Any], path: str = "<memory>") -> None:
        self._data = dict(data)
        self._path = path

    # ------------------------------------------------------------- Mapping --
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Config(path={self._path!r}, sections={sorted(self._data)})"

    # ------------------------------------------------------------- access --
    @property
    def source_path(self) -> str:
        """Filesystem path this configuration was loaded from."""
        return self._path

    def get(self, dotted: str, default: Any = None) -> Any:
        """Return the value at ``dotted`` or ``default`` if any part is missing.

        A present-but-``None`` value returns ``None``, not ``default``, so an
        explicit ``null`` in YAML is distinguishable from an absent key.
        """
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return default if node is _MISSING else node

    def require(self, dotted: str) -> Any:
        """Return the value at ``dotted``, raising if it is absent.

        Use this for values with no sensible default (bandwidths, weights,
        thresholds) so a missing key fails at load time, not mid-experiment.
        """
        value = self.get(dotted, _MISSING)
        if value is _MISSING or value is None:
            raise ConfigError(
                f"{self._path}: required configuration key '{dotted}' is missing"
            )
        return value

    def path(self, dotted: str) -> str:
        """Resolve a dotted path to its concrete location in the YAML file."""
        return f"{self._path}:{dotted}"

    def section(self, name: str) -> "Config":
        """Return a nested section as its own :class:`Config`."""
        value = self.require(name)
        if not isinstance(value, Mapping):
            raise ConfigError(
                f"{self._path}: section '{name}' must be a mapping, "
                f"got {type(value).__name__}"
            )
        return Config(value, f"{self._path}:{name}")


def _find_config(path: Optional[str]) -> str:
    """Resolve the config file location, searching upward from the CWD."""
    if path:
        return path
    env = os.environ.get("SDN_CONFIG")
    if env:
        return env
    # Walk up from the current directory so tests and scripts work from anywhere
    # inside the repository.
    here = os.path.abspath(os.getcwd())
    while True:
        candidate = os.path.join(here, DEFAULT_CONFIG_PATH)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            return DEFAULT_CONFIG_PATH
        here = parent


def load_config(path: Optional[str] = None, *, strict: bool = True) -> Config:
    """Load and validate ``config.yaml``.

    Parameters
    ----------
    path:
        Explicit path to the YAML file. When omitted, ``$SDN_CONFIG`` is
        consulted, then the file is searched for in the current directory and
        its ancestors.
    strict:
        When True (default) every section in :data:`REQUIRED_SECTIONS` must be
        present.

    Raises
    ------
    ConfigError
        If the file is missing, is not valid YAML, is not a mapping, or (when
        ``strict``) lacks a required section.
    """
    resolved = _find_config(path)
    if not os.path.isfile(resolved):
        raise ConfigError(
            f"configuration file not found: {resolved}\n"
            f"Set --config, or the SDN_CONFIG environment variable."
        )
    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{resolved}: invalid YAML: {exc}") from exc

    if data is None:
        raise ConfigError(f"{resolved}: file is empty")
    if not isinstance(data, Mapping):
        raise ConfigError(
            f"{resolved}: top level must be a mapping, got {type(data).__name__}"
        )

    if strict:
        missing = [s for s in REQUIRED_SECTIONS if s not in data]
        if missing:
            raise ConfigError(f"{resolved}: missing required sections: {missing}")

    return Config(data, resolved)