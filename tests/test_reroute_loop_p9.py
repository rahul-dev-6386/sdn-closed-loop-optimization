"""Tests for the P9 reroute decision loop.

The modes are compared in P10, so the tests here pin the two things that make
that comparison meaningful: each mode must actually use the signals it claims
to, and a mode that could not run must say so rather than quietly reporting
numbers under the wrong label.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from common.config import load_config
from controller.reroute_loop import (
    MODE_SIGNALS,
    ROUTING_MODES,
    LinkHistory,
    LoopConfig,
    RerouteLoop,
    RerouteLoopError,
)
from routing.cost import CostWeights
from routing.reroute import Operation

DIAMOND_LINKS = [("s1", "s2"), ("s2", "s4"), ("s1", "s3"), ("s3", "s4")]
PATH_A: Tuple[str, ...] = ("s1", "s2", "s4")
PATH_B: Tuple[str, ...] = ("s1", "s3", "s4")

#: A window long enough that no link is ever short on history.
MANY_ROWS = 20


def rows_for(
    busy: Tuple[str, ...], *, n: int = MANY_ROWS, start: float = 1000.0,
    util_a: float = 0.9, util_b: float = 0.2,
) -> List[Dict[str, Any]]:
    """Telemetry where the links of one path are congested and the other is not."""
    out: List[Dict[str, Any]] = []
    for i in range(n):
        ts = start + i * 2.0
        for src, dst in DIAMOND_LINKS:
            congested = (src, dst) in busy
            out.append({
                "timestamp": ts, "src_switch": src, "dst_switch": dst,
                "direction": "forward", "run_id": "run_A", "pattern": "ramp",
                "utilization": (0.95 if congested
                                else (util_a if (src, dst) in (("s1", "s2"),
                                                               ("s2", "s4"))
                                      else util_b)),
                "latency_ms": 55.0 if congested else 4.0,
                "loss_pct": 3.0 if congested else 0.0,
                "tx_rate_mbps": 90.0 if congested else 20.0,
                "rx_rate_mbps": 85.0 if congested else 19.0,
                "tx_packet_rate_pps": 900.0 if congested else 200.0,
                "rx_packet_rate_pps": 880.0 if congested else 190.0,
                "capacity_mbps": 100.0,
                "is_congested": congested,
            })
    return out


def make_loop(
    mode: str = "ml_aware", *, rows: Optional[List[Dict[str, Any]]] = None,
    predictor: Any = None, stalled: bool = False,
    apply_results: Optional[List[bool]] = None, start_t: float = 0.0,
) -> RerouteLoop:
    """A loop over a scripted telemetry source, with no fabric involved."""
    source = rows if rows is not None else rows_for({("s1", "s2"), ("s2", "s4")})
    state = {"t": start_t}
    applied: List[Operation] = []
    results = list(apply_results) if apply_results is not None else []

    def apply(op: Operation) -> bool:
        applied.append(op)
        if results:
            return results.pop(0)
        return True

    loop = RerouteLoop(
        snapshot=lambda: source,
        apply=apply,
        predictor=predictor,
        stalled=lambda: stalled,
        config=LoopConfig(
            mode=mode, weights=CostWeights(), improvement_ratio=0.85,
            cooldown_s=10.0, min_dwell_s=0.0, congestion_threshold=0.80,
            history_w=12,
        ),
        now_fn=lambda: state["t"],
    )
    loop.observe_links(DIAMOND_LINKS)
    loop.register_flow("h1->h4", PATH_A)
    loop.applied = applied
    return loop


class _Pred:
    """One link's forecast, shaped like ml.predict.Prediction."""

    def __init__(self, link_key, probability):
        self.link_key = link_key
        self.probability = probability
        self.congested = probability is not None and probability >= 0.5


class StubPredictor:
    """Returns a per-link probability, defaulting to one value for all links.

    Parameters
    ----------
    value:
        Probability for any link not named in ``overrides``.
    overrides:
        ``(src, dst) -> probability``, so a test can say "the model sees danger
        on this path only" -- which is the whole point of comparing the modes.
    """

    def __init__(self, value: Optional[float] = 0.95, degraded: bool = False,
                 overrides: Optional[Dict[Tuple[str, str], Optional[float]]] = None) -> None:
        self.value = value
        self.degraded = degraded
        self.overrides = overrides or {}

    def predict(self, rows):
        class Batch:
            pass

        batch = Batch()
        seen: Dict[Tuple[str, str, str], _Pred] = {}
        for r in rows:
            key = (r["src_switch"], r["dst_switch"], r["direction"])
            prob = self.overrides.get((r["src_switch"], r["dst_switch"]),
                                      self.value)
            seen[key] = _Pred(key, prob)
        batch.predictions = list(seen.values())
        batch.degraded = self.degraded
        return batch


# ------------------------------------------------------------- config --
def test_config_from_the_project_yaml_uses_a_valid_mode() -> None:
    cfg = LoopConfig.from_config(load_config())
    assert cfg.mode in ROUTING_MODES
    assert cfg.history_w == int(load_config().get("ml.window_w"))


def test_an_unknown_mode_is_rejected() -> None:
    class Fake:
        def get(self, key, default=None):
            return "teleportation" if key == "routing.mode" else default

    with pytest.raises(RerouteLoopError, match="not one of"):
        LoopConfig.from_config(Fake())


def test_every_mode_declares_its_signals() -> None:
    for mode in ROUTING_MODES:
        signals = MODE_SIGNALS[mode]
        assert signals["load"] or mode == "static_shortest"
        if mode == "ml_aware":
            assert signals["forecast"] is True
        else:
            assert signals["forecast"] is False


# ------------------------------------------------------------ history --
def test_history_keeps_only_the_trailing_window() -> None:
    history = LinkHistory(window_w=3)
    for tick in range(5):
        history.add([{"src_switch": "s1", "dst_switch": "s2",
                      "direction": "forward", "timestamp": float(tick)}])
    assert history.window_sizes()[("s1", "s2", "forward")] == 3


def test_history_rejects_a_bad_window() -> None:
    with pytest.raises(ValueError, match="window_w"):
        LinkHistory(0)


def test_history_ignores_rows_without_a_link() -> None:
    history = LinkHistory(4)
    history.add([{"timestamp": 1.0}, {"src_switch": "", "dst_switch": "s2"}])
    assert history.window_sizes() == {}


def test_history_clear() -> None:
    history = LinkHistory(4)
    history.add(rows_for(set(), n=1))
    history.clear()
    assert history.rows() == []


# ----------------------------------------------------------- fallback --
def test_a_stalled_fabric_falls_back_to_shortest_path() -> None:
    """Rule 7: with untrusted telemetry the only safe choice is hop count."""
    loop = make_loop("ml_aware", stalled=True, predictor=StubPredictor())
    report = loop.tick()
    assert report.effective_mode == "static_shortest"
    assert report.degraded is True
    assert "stalled" in report.fallback_reason


def test_ml_aware_without_a_model_degrades_to_reactive() -> None:
    loop = make_loop("ml_aware", predictor=None)
    report = loop.tick()
    assert report.effective_mode == "reactive_threshold"
    assert "no model" in report.fallback_reason or "unavailable" in report.fallback_reason


def test_a_degraded_prediction_degrades_the_mode() -> None:
    loop = make_loop("ml_aware", predictor=StubPredictor(degraded=True))
    report = loop.tick()
    assert report.effective_mode == "reactive_threshold"
    assert report.degraded is True


def test_a_raising_predictor_does_not_kill_the_tick() -> None:
    class Exploding:
        def predict(self, rows):
            raise RuntimeError("model corrupted")

    loop = make_loop("ml_aware", predictor=Exploding())
    report = loop.tick()
    assert report.effective_mode == "reactive_threshold"
    assert "unavailable" in report.fallback_reason


def test_static_shortest_never_degrades() -> None:
    """It has no telemetry dependency to lose."""
    loop = make_loop("static_shortest", stalled=True)
    report = loop.tick()
    assert report.effective_mode == "static_shortest"
    assert report.degraded is False


def test_fallbacks_are_counted_for_the_results_table() -> None:
    """A results table must be able to say how often the mode really ran."""
    loop = make_loop("ml_aware", predictor=None)
    loop.tick()
    loop.tick()
    summary = loop.mode_report()
    assert summary["n_ticks"] == 2
    assert summary["degraded_ticks"] == 2
    assert summary["fallbacks"]


# ------------------------------------------------------ mode behaviour --
def test_static_shortest_ignores_congestion_entirely() -> None:
    """Hop count alone: no mode of the ML signal may change its decision."""
    loop = make_loop("static_shortest")
    loop.tick()
    assert loop.applied == [], "a static controller must not reroute"


def test_reactive_threshold_moves_once_the_link_is_over_threshold() -> None:
    loop = make_loop("reactive_threshold")
    report = loop.tick()
    assert report.reroutes_applied > 0
    assert loop.controller.flows["h1->h4"].path == PATH_B


def test_ml_aware_reroutes_using_the_forecast() -> None:
    loop = make_loop("ml_aware", predictor=StubPredictor(0.95))
    report = loop.tick()
    assert report.reroutes_applied > 0
    assert loop.controller.flows["h1->h4"].path == PATH_B


def test_ml_aware_with_a_low_forecast_keeps_the_flow_where_it_is() -> None:
    """A model that sees no danger must not trigger a reroute.

    Both paths are lightly loaded here, so the only thing that could move the
    flow is the forecast -- and the forecast says 0.01.
    """
    loop = make_loop("ml_aware", rows=rows_for(set(), util_a=0.2, util_b=0.2),
                     predictor=StubPredictor(0.01))
    loop.tick()
    assert loop.applied == []


def test_ml_aware_reroutes_on_the_forecast_alone() -> None:
    """The central P9 claim, tested as a difference between two modes.

    Nothing is congested yet: both paths carry 20%. A reactive controller has no
    evidence and must stay put. The ML controller has been told this path is
    about to congest and moves *before* it does. If these two behaved the same,
    the model would be adding nothing.
    """
    quiet = rows_for(set(), util_a=0.2, util_b=0.2)
    forecast = {"s1->s2:forward": 0.95, "s2->s4:forward": 0.95,
                "s1->s3:forward": 0.02, "s3->s4:forward": 0.02}

    reactive = make_loop("reactive_threshold", rows=quiet)
    reactive.tick()
    assert reactive.applied == [], "no congestion yet, so nothing to react to"
    assert reactive.controller.flows["h1->h4"].path == PATH_A

    aware = make_loop("ml_aware", rows=quiet, predictor=StubPredictor(
        0.02, overrides={("s1", "s2"): 0.95, ("s2", "s4"): 0.95}))
    aware.tick()
    assert aware.applied, "the forecast should have moved the flow"
    assert aware.controller.flows["h1->h4"].path == PATH_B


def test_ecmp_cannot_see_the_forecast_either() -> None:
    """ECMP spreads load without a feedback loop, so it must also stay put."""
    quiet = rows_for(set(), util_a=0.2, util_b=0.2)
    loop = make_loop("ecmp", rows=quiet, predictor=StubPredictor(
        0.02, overrides={("s1", "s2"): 0.99, ("s2", "s4"): 0.99}))
    loop.tick()
    assert loop.applied == []


def test_candidates_cover_both_diamond_paths() -> None:
    loop = make_loop("ml_aware")
    assert set(loop.candidates("s1", "s4")) == {PATH_A, PATH_B}


def test_candidates_are_empty_for_an_unknown_pair() -> None:
    loop = make_loop("ml_aware")
    assert loop.candidates("s1", "s9") == []


# ------------------------------------------------------------ reporting --
def test_tick_report_serialises() -> None:
    loop = make_loop("ml_aware", predictor=StubPredictor(0.95))
    report = loop.tick().as_dict()
    assert report["mode"] == "ml_aware"
    assert report["effective_mode"] == "ml_aware"
    assert "predicted_congested" in report
    assert isinstance(report["plans"], list)


def test_predicted_congested_lists_the_links() -> None:
    loop = make_loop("ml_aware", predictor=StubPredictor(0.95))
    report = loop.tick()
    assert "s1->s2:forward" in report.predicted_congested


def test_mode_report_counts_planned_reroutes() -> None:
    loop = make_loop("reactive_threshold")
    loop.tick()
    summary = loop.mode_report()
    assert summary["reroutes_planned"] >= 1
    assert summary["n_flows"] == 1


def test_a_failed_application_leaves_the_flow_alone() -> None:
    """The flow must not be recorded as moved if the rules did not go in."""
    loop = make_loop("reactive_threshold", apply_results=[False])
    report = loop.tick()
    assert report.reroutes_applied == 0
    assert loop.controller.flows["h1->h4"].path == PATH_A


def test_reset_clears_history_flows_and_reports() -> None:
    loop = make_loop("reactive_threshold")
    loop.tick()
    loop.reset()
    summary = loop.mode_report()
    assert summary["n_ticks"] == 0
    assert summary["n_flows"] == 0


def test_cooldown_prevents_a_reroute_every_tick() -> None:
    """Without hysteresis a flow would move on every 2 s decision tick."""
    loop = make_loop("reactive_threshold")
    for tick in range(10):
        report = loop.tick()
        loop.controller.guard.record("h1->h4", float(tick * 2))
    assert len(loop.applied) < 10 * 4
