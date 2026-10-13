"""Tests for wiring the P9 loop into the controller.

These are the tests that would catch a bug invisible to the pure unit tests of
:mod:`routing.reroute` and :mod:`controller.reroute_loop`: the translation from
an abstract "install_ingress on s2" operation into a concrete OpenFlow rule with
real port numbers on a real switch.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller.reroute_integration import (  # noqa: E402
    RerouteIntegration,
    make_flow_applier,
    make_snapshot_fn,
)
from routing.reroute import Operation, build_reroute_plan  # noqa: E402

PATH_A: Tuple[str, ...] = ("s1", "s2", "s4")
PATH_B: Tuple[str, ...] = ("s1", "s3", "s4")

#: Diamond port numbering, as the topology builder assigns it.
PORTS = {
    ("s1", "s2"): 2, ("s2", "s1"): 2,
    ("s2", "s4"): 3, ("s4", "s2"): 3,
    ("s1", "s3"): 3, ("s3", "s1"): 3,
    ("s3", "s4"): 4, ("s4", "s3"): 4,
}


HOST_PORTS = {"s1": 1, "s4": 1}


def port_of(a: str, b: str) -> Optional[int]:
    """The port on ``a`` facing ``b``."""
    return PORTS.get((a, b))


def host_port_of(switch: str) -> Optional[int]:
    """The host-facing port of an edge switch."""
    return HOST_PORTS.get(switch)


class FakeProgrammer:
    """Records the flow specs it is asked to install or delete."""

    def __init__(self, succeed: bool = True) -> None:
        self.installed: List[Any] = []
        self.deleted: List[Any] = []
        self.succeed = succeed

    def install(self, spec: Any) -> bool:
        self.installed.append(spec)
        return self.succeed

    def delete(self, spec: Any) -> bool:
        self.deleted.append(spec)
        return self.succeed


# ------------------------------------------------------------- snapshot --
def test_snapshot_reads_latest_link_records() -> None:
    class Record:
        def __init__(self) -> None:
            self.timestamp, self.utilization = 1000.0, 0.5
            self.latency_ms, self.loss_pct = 4.0, 0.0
            self.tx_rate_mbps, self.capacity_mbps = 50.0, 100.0
            self.is_congested = False

    class Telemetry:
        latest_link_records = {("s1", "s2", "forward"): Record()}

    rows = make_snapshot_fn(Telemetry())()
    assert len(rows) == 1
    assert rows[0]["src_switch"] == "s1"
    assert rows[0]["utilization"] == 0.5


def test_snapshot_includes_stale_records_rather_than_filtering() -> None:
    """P8 detects staleness itself; duplicating the rule here would let them
    drift apart."""

    class Record:
        timestamp, utilization = 1000.0, 0.9
        latency_ms, loss_pct = 99.0, 5.0
        tx_rate_mbps, capacity_mbps = 90.0, 100.0
        is_congested = True

    class Telemetry:
        latest_link_records = {
            ("s1", "s2", "forward"): Record(),
            ("s2", "s4", "forward"): Record(),
        }

    rows = make_snapshot_fn(Telemetry())()
    assert len(rows) == 2
    assert any(r["is_congested"] for r in rows)


def test_snapshot_of_an_empty_collector_is_empty() -> None:
    class Telemetry:
        latest_link_records: Dict[Any, Any] = {}

    assert make_snapshot_fn(Telemetry())() == []


# --------------------------------------------------------------- applier --
def test_applier_installs_a_real_rule_on_each_hop() -> None:
    """The translation that a pure-logic test cannot check.

    An operation says "install_ingress on s2"; the fabric needs a rule matching
    the port the packet arrived on and sending it out the port facing s4.
    """
    prog = FakeProgrammer()
    apply = make_flow_applier(prog, port_of, host_port_of)
    apply.set_path("f1", PATH_A)

    ok = apply(Operation("install_ingress", "s2", {"flow": "f1"}, 99, "prepare"))
    assert ok is True
    assert len(prog.installed) == 1
    spec = prog.installed[0]
    assert spec.switch == "s2"
    assert spec.match == {"in_port": PORTS[("s2", "s1")]}
    assert spec.out_port == PORTS[("s2", "s4")]
    assert spec.priority == 99


def test_applier_uses_the_live_path_after_a_reroute() -> None:
    """Ports must follow the path the flow is actually on."""
    prog = FakeProgrammer()
    apply = make_flow_applier(prog, port_of, host_port_of)
    apply.set_path("f1", PATH_A)
    apply.set_path("f1", PATH_B)
    apply(Operation("install_ingress", "s3", {"flow": "f1"}, 99, "prepare"))
    spec = prog.installed[-1]
    assert spec.switch == "s3"
    assert spec.out_port == PORTS[("s3", "s4")]


def test_applier_refuses_without_path_context() -> None:
    prog = FakeProgrammer()
    apply = make_flow_applier(prog, port_of, host_port_of)
    assert apply(Operation("install_ingress", "s2", {"flow": "ghost"}, 99, "prepare")) \
        is False
    assert prog.installed == [], "an unresolvable operation must install nothing"


def test_applier_refuses_an_unknown_switch() -> None:
    prog = FakeProgrammer()
    apply = make_flow_applier(prog, port_of, host_port_of)
    apply.set_path("f1", PATH_A)
    assert apply(Operation("install_ingress", "s9", {"flow": "f1"}, 99, "prepare")) \
        is False


def test_applier_refuses_unresolvable_ports() -> None:
    """A missing port must fail the operation, not install a wrong rule."""
    prog = FakeProgrammer()
    apply = make_flow_applier(prog, lambda a, b: None, host_port_of)
    apply.set_path("f1", PATH_A)
    assert apply(Operation("install_ingress", "s2", {"flow": "f1"}, 99, "prepare")) \
        is False
    assert prog.installed == []


def test_applier_deletes_when_the_operation_is_a_delete() -> None:
    prog = FakeProgrammer()
    apply = make_flow_applier(prog, port_of, host_port_of)
    apply.set_path("f1", PATH_A)
    apply(Operation("delete_ingress", "s2", {"flow": "f1"}, 100, "commit"))
    assert len(prog.deleted) == 1
    assert prog.deleted[0].switch == "s2"
    assert prog.installed == []


def test_applier_reports_a_failed_install() -> None:
    """A False return is what triggers the plan's rollback."""
    prog = FakeProgrammer(succeed=False)
    apply = make_flow_applier(prog, port_of, host_port_of)
    apply.set_path("f1", PATH_A)
    assert apply(Operation("install_ingress", "s2", {"flow": "f1"}, 99, "prepare")) \
        is False


def test_every_plan_operation_can_be_applied() -> None:
    """End-to-end: a real make-before-break plan against a fake fabric.

    Every operation must resolve to a concrete rule, otherwise the controller
    would roll back every reroute it attempted and the four-mode comparison
    would be meaningless.
    """
    prog = FakeProgrammer()
    apply = make_flow_applier(prog, port_of, host_port_of)
    apply.set_path("f1", PATH_A)
    plan = build_reroute_plan("f1", PATH_A, PATH_B, {"flow": "f1"})
    # Both paths must be registered: installs resolve against the new path and
    # deletes against the old. Registering only the old path makes every install
    # unresolvable, so the plan rolls back and no reroute ever succeeds.
    apply.set_plan("f1", PATH_A, PATH_B)
    for op in plan.operations:
        assert apply(op) is True, f"operation failed: {op}"
    # Ingress and egress at an intermediate switch resolve to the same rule, so
    # the four operations cover three distinct switches, not four rules.
    installed_switches = {s.switch for s in prog.installed}
    assert installed_switches == {"s1", "s3", "s4"}, \
        "every switch on the new path must receive a rule"
    assert {s.switch for s in prog.deleted} == {"s1", "s2", "s4"}


# ------------------------------------------------------------ lifecycle --
def test_static_shortest_starts_no_thread() -> None:
    """That mode has nothing to decide, so no thread should be spawned."""

    class Controller:
        class config:
            @staticmethod
            def get(key, default=None):
                return "static_shortest" if key == "routing.mode" else default

        telemetry = None
        flows = None

    integration = RerouteIntegration(Controller())
    integration.start()
    assert integration.thread is None
    assert integration.loop is None
    report = integration.report()
    assert report["started"] is False
    assert report["mode"] == "static_shortest"


def test_start_is_skipped_without_a_flow_programmer() -> None:
    """The controller builds its programmer asynchronously; starting too early
    must be a no-op rather than a crash."""

    class Controller:
        class config:
            @staticmethod
            def get(key, default=None):
                return "ml_aware" if key == "routing.mode" else default

        telemetry = None
        flows = None

    integration = RerouteIntegration(Controller())
    integration.start()
    assert integration.thread is None


def test_report_before_start_is_honest() -> None:
    class Controller:
        class config:
            @staticmethod
            def get(key, default=None):
                return "ecmp" if key == "routing.mode" else default

    integration = RerouteIntegration(Controller())
    report = integration.report()
    assert report["started"] is False
    assert report["mode"] == "ecmp"
    assert report["n_ticks"] == 0


def test_stop_is_idempotent() -> None:
    class Controller:
        class config:
            @staticmethod
            def get(key, default=None):
                return "static_shortest" if key == "routing.mode" else default

    integration = RerouteIntegration(Controller())
    integration.stop()
    integration.stop()


def test_first_switch_matches_the_host_facing_port() -> None:
    """s1 receives the packet from h1, not from another switch."""
    prog = FakeProgrammer()
    apply = make_flow_applier(prog, port_of, host_port_of)
    apply.set_path("f1", PATH_A)
    assert apply(Operation("install_ingress", "s1", {"flow": "f1"}, 99, "prepare"))
    spec = prog.installed[-1]
    assert spec.match == {"in_port": HOST_PORTS["s1"]}
    assert spec.out_port == PORTS[("s1", "s2")]


def test_last_switch_sends_to_its_host() -> None:
    prog = FakeProgrammer()
    apply = make_flow_applier(prog, port_of, host_port_of)
    apply.set_path("f1", PATH_A)
    assert apply(Operation("install_egress", "s4", {"flow": "f1"}, 99, "prepare"))
    spec = prog.installed[-1]
    assert spec.match == {"in_port": PORTS[("s4", "s2")]}
    assert spec.out_port == HOST_PORTS["s4"]


def test_without_a_host_resolver_edge_operations_are_refused() -> None:
    """Better to refuse than to guess a port and install a wrong rule."""
    prog = FakeProgrammer()
    apply = make_flow_applier(prog, port_of)   # no host_port_of
    apply.set_path("f1", PATH_A)
    assert apply(Operation("install_ingress", "s1", {"flow": "f1"}, 99, "prepare")) \
        is False
    assert apply(Operation("install_egress", "s4", {"flow": "f1"}, 99, "prepare")) \
        is False
    assert prog.installed == []
    # The intermediate switch needs no host port, so it still works.
    assert apply(Operation("install_ingress", "s2", {"flow": "f1"}, 99, "prepare")) \
        is True


def test_installs_resolve_against_the_new_path_not_the_old() -> None:
    """Regression: the failure mode was a reroute that always rolled back."""
    prog = FakeProgrammer()
    apply = make_flow_applier(prog, port_of, host_port_of)
    apply.set_plan("f1", PATH_A, PATH_B)
    # s3 is on the NEW path only.
    assert apply(Operation("install_ingress", "s3", {"flow": "f1"}, 99, "prepare")) \
        is True
    assert prog.installed[-1].out_port == PORTS[("s3", "s4")]


def test_deletes_resolve_against_the_old_path_not_the_new() -> None:
    """s2 is on the OLD path only; a delete for it must still resolve."""
    prog = FakeProgrammer()
    apply = make_flow_applier(prog, port_of, host_port_of)
    apply.set_plan("f1", PATH_A, PATH_B)
    assert apply(Operation("delete_ingress", "s2", {"flow": "f1"}, 100, "commit")) \
        is True
    assert prog.deleted[-1].out_port == PORTS[("s2", "s4")]


def test_an_operation_off_both_paths_is_refused() -> None:
    prog = FakeProgrammer()
    apply = make_flow_applier(prog, port_of, host_port_of)
    apply.set_plan("f1", PATH_A, PATH_B)
    assert apply(Operation("install_ingress", "s9", {"flow": "f1"}, 99, "prepare")) \
        is False
