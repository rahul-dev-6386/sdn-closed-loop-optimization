"""Path cost, next-hop selection and reroute decisions (P9).

The cost function
-----------------
::

    cost = a*util_norm + b*latency_norm + c*loss_norm + d*p_congestion

``p_congestion`` is the P8 model's forecast for the link. The other three terms
are present-load measures. That split is the whole point: the first three
describe how busy a link is *now*, while the last describes how likely it is to
become a bottleneck. A link sitting at 60% with a 0.8 forecast is worse for
future traffic than a link at 85% with a 0.05 forecast, and only a function that
separates the two signals can express that.

Every input is normalised to ``[0, 1]`` before weighting. Without normalisation
the terms are not comparable -- milliseconds, percentages and probabilities do
not share a scale -- and the weight vector stops meaning anything.

Reroute policy
--------------
Three guards, each preventing a specific pathology:

* **Improvement ratio** -- reroute only if the alternative is meaningfully
  cheaper. Without it, two nearly-equal paths cause the flow to oscillate
  between them on every decision tick.
* **Cooldown** -- a minimum gap between reroutes *of the same flow*. Bounds the
  rate at which a single flow can be disrupted.
* **Minimum dwell** -- a flow must stay put for a while after arriving. Stops a
  flow that would immediately be moved again, which is churn without benefit.

Make-before-break ordering lives in :mod:`controller.flow_manager`; this module
only decides *whether* and *to where*.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

LOGGER = logging.getLogger("routing.cost")

#: A directed link: (src_switch, dst_switch, direction).
LinkKey = Tuple[str, str, str]

#: A whole path, as an ordered list of directed links.
Path = Tuple[LinkKey, ...]


class RoutingError(ValueError):
    """Raised for an unusable cost configuration or path input."""


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def normalize(value: float, reference: float, *, lo: float = 0.0) -> float:
    """Map ``value`` into ``[0, 1]`` against a reference ceiling.

    Parameters
    ----------
    value:
        The measured value.
    reference:
        The value considered "maximally bad" (e.g. 50 ms latency, 100 Mbps).
    lo:
        Value considered "zero" before the scale starts. Values between ``lo``
        and ``reference`` are mapped linearly; values at or beyond ``reference``
        saturate at 1.0 so one outlier cannot dominate a path.

    Raises
    ------
    RoutingError
        If ``reference`` is not positive -- a zero or negative ceiling would
        make every normalised value infinite or NaN.
    """
    if reference <= 0:
        raise RoutingError(
            f"normalisation reference must be positive, got {reference}"
        )
    scaled = (float(value) - lo) / (reference - lo) if reference != lo else 0.0
    if scaled < 0.0:
        return 0.0
    if scaled > 1.0:
        return 1.0
    return scaled


# --------------------------------------------------------------------------- #
# Link cost
# --------------------------------------------------------------------------- #
@dataclass
class CostWeights:
    """The four cost coefficients, plus the normalisation references."""

    a_utilization: float = 0.25
    b_latency: float = 0.15
    c_loss: float = 0.20
    d_congestion: float = 0.40
    bandwidth_mbps: float = 100.0
    latency_ms: float = 50.0
    loss_pct: float = 5.0

    def __post_init__(self) -> None:
        """Validate the weights and references.

        Raises
        ------
        RoutingError
            If a weight is negative, the weights do not sum to 1 (so costs are
            not comparable between configurations), or a reference is invalid.
        """
        if any(w < 0 for w in (self.a_utilization, self.b_latency,
                               self.c_loss, self.d_congestion)):
            raise RoutingError("cost weights must be non-negative")
        total = (self.a_utilization + self.b_latency
                 + self.c_loss + self.d_congestion)
        if abs(total - 1.0) > 1e-6:
            raise RoutingError(
                f"cost weights must sum to 1.0, got {total:.4f} "
                f"({self.a_utilization}+{self.b_latency}+"
                f"{self.c_loss}+{self.d_congestion}). Non-normalised weights "
                "make costs incomparable across runs."
            )
        for name, ref in (("bandwidth_mbps", self.bandwidth_mbps),
                          ("latency_ms", self.latency_ms),
                          ("loss_pct", self.loss_pct)):
            if ref <= 0:
                raise RoutingError(f"{name} reference must be positive, got {ref}")

    @classmethod
    def from_config(cls, config: Any) -> "CostWeights":
        """Build from the ``routing.weights`` and ``telemetry.norm`` sections."""
        return cls(
            a_utilization=float(config.get("routing.weights.a_utilization", 0.25)),
            b_latency=float(config.get("routing.weights.b_latency", 0.15)),
            c_loss=float(config.get("routing.weights.c_loss", 0.20)),
            d_congestion=float(config.get("routing.weights.d_congestion", 0.40)),
            bandwidth_mbps=float(config.get("telemetry.norm.bandwidth_mbps", 100.0)),
            latency_ms=float(config.get("telemetry.norm.latency_ms", 50.0)),
            loss_pct=float(config.get("telemetry.norm.loss_pct", 5.0)),
        )

    def as_dict(self) -> Dict[str, float]:
        """JSON-serialisable view."""
        return {
            "a_utilization": self.a_utilization,
            "b_latency": self.b_latency,
            "c_loss": self.c_loss,
            "d_congestion": self.d_congestion,
        }


@dataclass
class LinkMetrics:
    """The measurements a single link contributes to its cost."""

    utilization: float = 0.0
    latency_ms: float = 0.0
    loss_pct: float = 0.0
    p_congestion: float = 0.0
    bandwidth_mbps: Optional[float] = None

    @classmethod
    def from_telemetry(
        cls, row: Mapping[str, Any], p_congestion: float = 0.0,
    ) -> "LinkMetrics":
        """Build from one telemetry record plus a forecast probability."""
        util = row.get("utilization")
        if util is None:
            # Fall back to rate over capacity when utilisation was not reported.
            rate = float(row.get("tx_rate_mbps") or 0.0)
            capacity = float(row.get("capacity_mbps") or 0.0)
            util = rate / capacity if capacity > 0 else 0.0
        return cls(
            utilization=float(util),
            latency_ms=float(row.get("latency_ms") or 0.0),
            loss_pct=float(row.get("loss_pct") or 0.0),
            p_congestion=float(p_congestion),
            bandwidth_mbps=(float(row["capacity_mbps"])
                            if row.get("capacity_mbps") else None),
        )

    def terms(self, weights: CostWeights) -> Dict[str, float]:
        """The four normalised, weighted terms -- each in ``[0, 1]``.

        Exposed separately so a path's cost can be explained ("this path is
        expensive because loss is 1.0 and forecast is 0.9") rather than being an
        opaque number. P10 reports these breakdowns.
        """
        reference = weights.bandwidth_mbps
        if self.bandwidth_mbps:
            reference = self.bandwidth_mbps
        return {
            "utilization": weights.a_utilization * normalize(
                self.utilization, 1.0),
            "latency": weights.b_latency * normalize(
                self.latency_ms, weights.latency_ms),
            "loss": weights.c_loss * normalize(self.loss_pct, weights.loss_pct),
            "congestion": weights.d_congestion * min(
                max(self.p_congestion, 0.0), 1.0),
        }


def link_cost(metrics: LinkMetrics, weights: CostWeights) -> float:
    """Cost of a single link, in ``[0, 1]``.

    A link with no telemetry and no forecast scores 0 -- a fresh link is not
    congested, and inventing a penalty for it would bias every path toward
    avoiding the newest switches.
    """
    return sum(metrics.terms(weights).values())


def path_cost(path: Path, metrics: Mapping[LinkKey, LinkMetrics],
              weights: CostWeights) -> float:
    """Cost of a path: the **maximum** of its link costs.

    Notes
    -----
    A path is only as good as its worst link, so the aggregate is a max rather
    than a mean or sum. A sum would be dominated by path *length*, silently
    preferring a longer path of cheap links over a short congested one -- the
    opposite of what load balancing needs.

    Raises
    ------
    RoutingError
        If the path is empty, or a link on it has no recorded metrics.
    """
    if not path:
        raise RoutingError("cannot cost an empty path")
    costs: List[float] = []
    for link in path:
        if link not in metrics:
            raise RoutingError(f"no metrics for link {link}")
        costs.append(link_cost(metrics[link], weights))
    return max(costs)


# --------------------------------------------------------------------------- #
# Path selection
# --------------------------------------------------------------------------- #
@dataclass
class PathChoice:
    """The outcome of comparing candidate paths for one flow."""

    best: Path
    best_cost: float
    current: Optional[Path] = None
    current_cost: Optional[float] = None
    reroute: bool = False
    reason: str = ""
    considered: Dict[Path, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view (paths rendered as ``src->dst`` strings)."""
        render = lambda p: [f"{a}->{b}:{d}" for a, b, d in p]  # noqa: E731
        return {
            "best": render(self.best),
            "best_cost": round(self.best_cost, 6),
            "current": render(self.current) if self.current else None,
            "current_cost": (round(self.current_cost, 6)
                             if self.current_cost is not None else None),
            "reroute": self.reroute,
            "reason": self.reason,
            "candidates": {"|".join(render(p)): round(c, 6)
                           for p, c in self.considered.items()},
        }


def choose_path(
    candidates: Sequence[Path],
    metrics: Mapping[LinkKey, LinkMetrics],
    weights: CostWeights,
    current: Optional[Path] = None,
    improvement_ratio: float = 0.85,
) -> PathChoice:
    """Pick the cheapest path, applying the improvement-ratio guard.

    Parameters
    ----------
    candidates:
        Paths under consideration. All must connect the same pair of endpoints;
        the caller is responsible for that (see
        :func:`ml.predict` / the topology tracker).
    metrics:
        Per-link measurements and forecasts.
    weights:
        Cost coefficients.
    current:
        The path in use now, if any. Required for the improvement guard to have
        any effect.
    improvement_ratio:
        Reroute only when ``best_cost < current_cost * improvement_ratio``. The
        default 0.85 demands a 15% improvement, which is what stops two
        near-equal paths from trading the flow back and forth every tick.

    Raises
    ------
    RoutingError
        If no candidates are supplied, or the ratio is not in ``(0, 1]``.
    """
    if not candidates:
        raise RoutingError("no candidate paths to choose between")
    if not 0.0 < improvement_ratio <= 1.0:
        raise RoutingError(
            f"improvement_ratio must be in (0, 1], got {improvement_ratio}"
        )

    scored: Dict[Path, float] = {p: path_cost(p, metrics, weights)
                                 for p in candidates}
    # Sort by (cost, path) so ties are broken deterministically; a tie broken by
    # dict order would make experiments irreproducible.
    best = min(scored, key=lambda p: (scored[p], p))

    if current is None:
        return PathChoice(best, scored[best], None, None, False,
                          "no current path", scored)
    if current not in scored:
        # The current path is no longer among the candidates (link down, or a
        # topology change); move to the best one immediately, no guard.
        return PathChoice(best, scored[best], current, None, True,
                          "current path is no longer available", scored)

    current_cost = scored[current]
    if best == current:
        return PathChoice(best, scored[best], current, current_cost, False,
                          "current path is already cheapest", scored)

    if scored[best] < current_cost * improvement_ratio:
        gain = (1.0 - scored[best] / current_cost) * 100.0 if current_cost else 0.0
        return PathChoice(best, scored[best], current, current_cost, True,
                          f"alternative is {gain:.1f}% cheaper", scored)
    return PathChoice(best, scored[best], current, current_cost, False,
                      f"best alternative is not {improvement_ratio:.0%} cheaper",
                      scored)


# --------------------------------------------------------------------------- #
# Hysteresis
# --------------------------------------------------------------------------- #
@dataclass
class RerouteGuard:
    """Per-flow cooldown and dwell tracking.

    The two timers serve different purposes and both are needed:

    * ``cooldown_s`` bounds how *often* a flow may be moved.
    * ``min_dwell_s`` ensures a flow stays put after *any* arrival, including the
      first one. Without it, a flow installed on a path at t=0 and immediately
      found wanting can be moved at t=0.1 and again at t=0.2.

    Parameters
    ----------
    cooldown_s, min_dwell_s:
        The configured windows, in seconds.
    now_fn:
        Clock, injectable so tests need not sleep.
    """

    cooldown_s: float = 10.0
    min_dwell_s: float = 8.0
    now_fn: Any = time.monotonic

    def __post_init__(self) -> None:
        """Validate the windows.

        Raises
        ------
        RoutingError
            If either window is negative.
        """
        if self.cooldown_s < 0 or self.min_dwell_s < 0:
            raise RoutingError("cooldown_s and min_dwell_s must be non-negative")

    _last: Dict[str, float] = field(default_factory=dict, repr=False)
    _arrived: Dict[str, float] = field(default_factory=dict, repr=False)

    def may_reroute(self, flow: str, now: Optional[float] = None) -> Tuple[bool, str]:
        """Whether ``flow`` may be moved now, and why not if it may not.

        Returns
        -------
        (allowed, reason)
            ``reason`` explains the refusal, or names the timers that passed.
        """
        t = self.now_fn() if now is None else now
        arrived = self._arrived.get(flow)
        if arrived is not None and t - arrived < self.min_dwell_s:
            return False, (
                f"dwell: {t - arrived:.1f}s of {self.min_dwell_s}s since arrival"
            )
        last = self._last.get(flow)
        if last is not None and t - last < self.cooldown_s:
            return False, (
                f"cooldown: {t - last:.1f}s of {self.cooldown_s}s since last reroute"
            )
        return True, "cooldown and dwell both satisfied"

    def record(self, flow: str, now: Optional[float] = None) -> None:
        """Note that ``flow`` was (re)placed on a path at this instant."""
        t = self.now_fn() if now is None else now
        self._last[flow] = t
        self._arrived.setdefault(flow, t)

    def register(self, flow: str, now: Optional[float] = None) -> None:
        """Note a flow's arrival without counting it as a reroute.

        Only the dwell timer starts. Seeding the cooldown here would block the
        very first reroute for ``cooldown_s``, which is not what "registered,
        not rerouted" means -- and it would mean a flow placed at t=0 can never
        be moved before t=cooldown_s regardless of how badly it is doing.
        """
        t = self.now_fn() if now is None else now
        self._arrived[flow] = t

    def dwell_remaining(self, flow: str, now: Optional[float] = None) -> float:
        """Seconds until ``flow`` becomes movable, never negative."""
        t = self.now_fn() if now is None else now
        arrived = self._arrived.get(flow)
        if arrived is None:
            return 0.0
        return max(0.0, self.min_dwell_s - (t - arrived))

    def reset(self) -> None:
        """Forget all flow state (used between experiment runs)."""
        self._last.clear()
        self._arrived.clear()
