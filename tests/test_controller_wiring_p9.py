"""Tests that the P9 loop is reachable from the controller.

Before this wiring, ``routing.mode`` was a configuration value that changed
nothing: :mod:`controller.reroute_loop` existed and was fully tested, but no code
path ever constructed it, so P10's four modes would have measured four identical
experiments. These tests pin the reachability, because a wiring regression is
invisible to the pure unit tests of the loop and the routing package.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller.reroute_integration import RerouteIntegration  # noqa: E402

PORTS = {
    ("s1", "s2"): 2, ("s2", "s1"): 2,
    ("s2", "s4"): 3, ("s4", "s2"): 3,
    ("s1", "s3"): 3, ("s3", "s1"): 3,
    ("s3", "s4"): 4, ("s4", "s3"): 4,
}


class FakeLink:
    """A discovered inter-switch link."""

    def __init__(self, src: str, dst: str, src_port: int, dst_port: int) -> None:
        self.src_switch, self.dst_switch = src, dst
        self.src_port, self.dst_port = src_port, dst_port
        self.bw_mbps, self.delay_ms = 100.0, 2.0


class FakeTopology:
    """Enough of the topology tracker for the port resolvers."""

    def __init__(self, *, with_links: bool = True,
                 with_hosts: bool = True) -> None:
        self.links: Dict[str, FakeLink] = {}
        if with_links:
            for index, (src, dst) in enumerate(
                    [("s1", "s2"), ("s2", "s4"), ("s1", "s3"), ("s3", "s4")]):
                self.links[f"{src}-{dst}"] = FakeLink(
                    src, dst, PORTS[(src, dst)], PORTS[(dst, src)])
        self.host_ports: Set[Tuple[str, int]] = set()
        if with_hosts:
            self.host_ports = {("s1", 1), ("s4", 1)}


class FakeTelemetry:
    """Telemetry collector stand-in."""

    def __init__(self) -> None:
        self.latest_link_records: Dict[Any, Any] = {}

    def is_telemetry_stalled(self) -> bool:
        return False


class FakeProgrammer:
    def install(self, spec: Any) -> bool:
        return True

    def delete(self, spec: Any) -> bool:
        return True


def make_controller(mode: str, *, with_links: bool = True,
                    with_hosts: bool = True, with_flows: bool = True) -> Any:
    """A controller stand-in exposing exactly what the integration touches."""
    from controller.sdn_controller import SdnController

    controller = SdnController.__new__(SdnController)
    controller.config = _Config({"routing.mode": mode})
    controller.topology = FakeTopology(with_links=with_links,
                                       with_hosts=with_hosts)
    controller.telemetry = FakeTelemetry()
    controller.flows = FakeProgrammer() if with_flows else None
    controller.reroute = None
    # Bind the real port resolvers -- they are the code under test here.
    controller.port_of = lambda a, b: SdnController.port_of(controller, a, b)
    controller.host_port_of = lambda sw: SdnController.host_port_of(controller, sw)
    controller._maybe_start_reroute_loop = (
        lambda: SdnController._maybe_start_reroute_loop(controller))
    return controller


class _Config:
    """A config object supporting dotted ``get``."""

    def __init__(self, values: Dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


# ------------------------------------------------------------ port_of --
def test_port_of_resolves_both_directions() -> None:
    controller = make_controller("ecmp")
    assert controller.port_of("s1", "s2") == PORTS[("s1", "s2")]
    assert controller.port_of("s2", "s1") == PORTS[("s2", "s1")]


def test_port_of_is_none_for_non_adjacent_switches() -> None:
    """None makes the caller refuse the operation, which is the safe outcome."""
    controller = make_controller("ecmp")
    assert controller.port_of("s1", "s4") is None
    assert controller.port_of("s1", "s9") is None


def test_host_port_of_returns_the_host_facing_port() -> None:
    controller = make_controller("ecmp")
    assert controller.host_port_of("s1") == 1
    assert controller.host_port_of("s4") == 1


def test_host_port_of_is_none_for_a_switch_without_a_host() -> None:
    controller = make_controller("ecmp")
    assert controller.host_port_of("s2") is None


def test_host_port_of_picks_the_lowest_when_several_hosts_attach() -> None:
    controller = make_controller("ecmp")
    controller.topology.host_ports = {("s1", 5), ("s1", 2)}
    assert controller.host_port_of("s1") == 2


# ---------------------------------------------------------- reachability --
@pytest.mark.parametrize("mode", ["ecmp", "reactive_threshold", "ml_aware"])
def test_the_loop_starts_for_every_decision_mode(mode: str) -> None:
    """This is the regression: without wiring, no mode started anything."""
    controller = make_controller(mode)
    controller._maybe_start_reroute_loop()
    assert controller.reroute is not None, f"{mode} never started a loop"
    assert controller.reroute.loop is not None
    assert controller.reroute.report()["mode"] == mode


def test_the_loop_starts_exactly_once() -> None:
    controller = make_controller("ecmp")
    for _ in range(5):
        controller._maybe_start_reroute_loop()
    assert controller.reroute is not None
    assert controller.reroute.report()["started"] is True


def test_the_loop_observes_the_discovered_links() -> None:
    """Without links the loop cannot enumerate any candidate path."""
    controller = make_controller("ecmp")
    controller._maybe_start_reroute_loop()
    assert controller.reroute.loop.candidates("s1", "s4")


def test_no_flow_programmer_means_no_start() -> None:
    """Switches connect asynchronously; starting too early must be a no-op."""
    controller = make_controller("ecmp", with_flows=False)
    controller._maybe_start_reroute_loop()
    assert controller.reroute is None


def test_no_links_means_no_start() -> None:
    controller = make_controller("ecmp", with_links=False)
    controller._maybe_start_reroute_loop()
    assert controller.reroute is None


def test_no_hosts_means_no_start() -> None:
    """A path's end rules need host-facing ports."""
    controller = make_controller("ecmp", with_hosts=False)
    controller._maybe_start_reroute_loop()
    assert controller.reroute is None


def test_start_deferred_until_the_fabric_is_ready() -> None:
    """The three prerequisites may arrive in any order; only the last triggers."""
    controller = make_controller("ecmp", with_links=False, with_hosts=False)
    controller._maybe_start_reroute_loop()
    assert controller.reroute is None
    controller.topology = FakeTopology(with_links=True, with_hosts=False)
    controller._maybe_start_reroute_loop()
    assert controller.reroute is None
    controller.topology = FakeTopology(with_links=True, with_hosts=True)
    controller._maybe_start_reroute_loop()
    assert controller.reroute is not None


def test_static_shortest_starts_nothing_but_is_reported() -> None:
    """That mode has no decision to make, so no thread -- and it must still be
    reportable so the API can say why."""
    controller = make_controller("static_shortest")
    controller._maybe_start_reroute_loop()
    assert controller.reroute is not None
    report = controller.reroute.report()
    assert report["started"] is False
    assert report["mode"] == "static_shortest"


def test_ml_aware_without_a_model_still_starts_and_degrades(
    monkeypatch,
) -> None:
    """No model is a degradation, not a reason to run nothing.

    The loader is stubbed to fail rather than relying on ``results/model.joblib``
    being absent -- this test once passed only because no model had been trained
    yet, and failed the moment one was. A test must not depend on the state of
    the working tree.
    """
    import controller.reroute_integration as integration_module

    def _no_model(self) -> None:
        return None

    monkeypatch.setattr(integration_module.RerouteIntegration,
                        "_load_predictor", _no_model)

    controller = make_controller("ml_aware")
    controller._maybe_start_reroute_loop()
    integration = controller.reroute
    assert integration is not None and integration.loop is not None
    assert integration.loop.predictor is None
    report = integration.loop.tick()
    assert report.effective_mode == "reactive_threshold"
    assert report.degraded is True
    assert integration.loop.fallbacks, "the fallback must be counted"
