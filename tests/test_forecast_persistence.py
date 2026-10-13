"""Tests that P8's forecast actually reaches the telemetry CSV.

Without this the predictor's output never leaves the loop: the CSV has no
forecast column, so the demo can never show a lead time and the experiment
harness can never correlate a prediction with what followed it. These tests pin
the whole path, including the distinction that matters most -- "no forecast" must
never be written as "forecast of zero".
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller.reroute_loop import LoopConfig, RerouteLoop  # noqa: E402
from controller.telemetry import (  # noqa: E402
    CSV_FIELDNAMES,
    LinkTelemetryRecord,
    TelemetryCollector,
)

KEY: Tuple[str, str, str] = ("s1", "s2", "forward")


def make_record(**overrides: Any) -> LinkTelemetryRecord:
    """A minimal link record with sensible defaults."""
    base = dict(
        run_id="run_A", timestamp=1000.0, src_switch="s1", dst_switch="s2",
        direction="forward", src_port=1, dst_port=2, capacity_mbps=100.0,
        tx_rate_mbps=10.0, rx_rate_mbps=9.0, tx_packet_rate_pps=100.0,
        rx_packet_rate_pps=90.0, utilization=0.10, latency_ms=3.0, loss_pct=0.0,
        tx_bytes=0, rx_bytes=0, tx_packets=0, rx_packets=0, tx_dropped=0,
        rx_errors=0, probe_loss_pct=0.0, is_stale=False, is_congested=False,
    )
    base.update(overrides)
    return LinkTelemetryRecord(**base)


# ------------------------------------------------------------------ csv --
def test_csv_has_the_forecast_columns() -> None:
    assert "p_congestion" in CSV_FIELDNAMES
    assert "predicted_congested" in CSV_FIELDNAMES


def test_absent_forecast_is_empty_not_zero() -> None:
    """The distinction the whole file depends on."""
    row = make_record().as_csv_dict()
    assert row["p_congestion"] == ""
    assert row["predicted_congested"] is False


def test_a_present_forecast_is_written() -> None:
    row = make_record(p_congestion=0.87,
                      predicted_congested=True).as_csv_dict()
    assert row["p_congestion"] == "0.8700"
    assert row["predicted_congested"] is True


def test_a_zero_forecast_is_written_as_zero_not_blank() -> None:
    """0.0 is a real prediction and must stay distinguishable from None."""
    row = make_record(p_congestion=0.0).as_csv_dict()
    assert row["p_congestion"] == "0.0000"


def test_record_fields_default_to_absent() -> None:
    record = make_record()
    assert record.p_congestion is None
    assert record.predicted_congested is False


# ------------------------------------------------------------ collector --
def test_apply_forecasts_reports_how_many_were_scored() -> None:
    collector = TelemetryCollector.__new__(TelemetryCollector)
    collector.forecasts = {}
    scored = collector.apply_forecasts({
        KEY: (0.9, True),
        ("s1", "s3", "forward"): (None, False),
    })
    assert scored == 1, "an unscored link is not a scored one"


def test_apply_forecasts_stores_none_as_no_forecast() -> None:
    collector = TelemetryCollector.__new__(TelemetryCollector)
    collector.forecasts = {}
    collector.apply_forecasts({KEY: (None, False)})
    probability, flagged = collector.forecasts[KEY]
    assert probability is None
    assert flagged is False


def test_apply_forecasts_drops_the_previous_tick() -> None:
    """Stale forecasts must not be carried forward.

    A link whose telemetry went quiet would otherwise keep reporting a confident
    probability derived from data that no longer exists -- the exact fiction P8
    exists to prevent.
    """
    collector = TelemetryCollector.__new__(TelemetryCollector)
    collector.forecasts = {}
    collector.apply_forecasts({KEY: (0.9, True)})
    collector.apply_forecasts({})
    assert collector.forecasts == {}


# --------------------------------------------------------- end to end --
class _Config:
    """Minimal config: the integration reads routing.mode at construction."""

    def get(self, key: str, default: Any = None) -> Any:
        return "ml_aware" if key == "routing.mode" else default


class StubPredictor:
    """Returns a fixed probability per link."""

    def __init__(self, value: Optional[float]) -> None:
        self.value = value

    def predict(self, rows):
        class Batch:
            degraded = False
            predictions = []

        batch = Batch()
        seen = {}
        for row in rows:
            key = (row["src_switch"], row["dst_switch"], row["direction"])
            seen[key] = type("P", (), {"link_key": key,
                                       "probability": self.value})()
        batch.predictions = list(seen.values())
        return batch


def make_rows(n: int = 20) -> List[Dict[str, Any]]:
    """Telemetry for one link, enough history for the predictor to score it."""
    return [{
        "timestamp": 1000.0 + i * 2.0, "src_switch": "s1", "dst_switch": "s2",
        "direction": "forward", "run_id": "run_A", "pattern": "ramp",
        "utilization": 0.5, "latency_ms": 4.0, "loss_pct": 0.0,
        "tx_rate_mbps": 50.0, "rx_rate_mbps": 48.0, "tx_packet_rate_pps": 500.0,
        "rx_packet_rate_pps": 480.0, "capacity_mbps": 100.0,
        "is_congested": False,
    } for i in range(n)]


def make_loop(value: Optional[float], **kwargs: Any) -> RerouteLoop:
    """A loop in ml_aware mode with a stub predictor."""
    rows = make_rows()
    loop = RerouteLoop(
        snapshot=lambda: rows, apply=lambda op: True,
        predictor=StubPredictor(value),
        stalled=lambda: False,
        config=LoopConfig(mode="ml_aware", history_w=12, congestion_threshold=0.8),
    )
    loop.observe_links([("s1", "s2")])
    return loop


def test_a_tick_records_the_forecast_for_publication() -> None:
    loop = make_loop(0.91)
    loop.tick()
    assert KEY in loop.last_forecasts
    probability, flagged = loop.last_forecasts[KEY]
    assert probability == pytest.approx(0.91)
    assert flagged is True


def test_a_low_forecast_is_recorded_but_not_flagged() -> None:
    loop = make_loop(0.10)
    loop.tick()
    probability, flagged = loop.last_forecasts[KEY]
    assert probability == pytest.approx(0.10)
    assert flagged is False


def test_a_non_ml_mode_records_no_forecasts() -> None:
    """A mode that does not use the model must not publish a forecast.

    Publishing one would let a static run's telemetry look like it had a
    predictor, and any lead-time comparison would be meaningless.
    """
    rows = make_rows()
    loop = RerouteLoop(
        snapshot=lambda: rows, apply=lambda op: True, predictor=None,
        stalled=lambda: False,
        config=LoopConfig(mode="reactive_threshold", history_w=12),
    )
    loop.observe_links([("s1", "s2")])
    loop.tick()
    assert loop.last_forecasts == {}


def test_publish_forecasts_reaches_the_collector() -> None:
    """The integration's job: get the forecast into telemetry."""
    from controller.reroute_integration import RerouteIntegration

    class Telemetry:
        def __init__(self) -> None:
            self.forecasts: Dict[Any, Any] = {}

        def apply_forecasts(self, forecasts):
            self.forecasts = dict(forecasts)
            return sum(1 for p in forecasts.values() if p[0] is not None)

        def is_telemetry_stalled(self) -> bool:
            return False

    class Controller:
        config = _Config()
        telemetry = Telemetry()

    integration = RerouteIntegration(Controller())
    integration.loop = make_loop(0.77)
    integration.loop.tick()
    assert integration.publish_forecasts() == 1
    assert Controller.telemetry.forecasts[KEY][0] == pytest.approx(0.77)


def test_publishing_without_a_loop_is_zero_not_a_crash() -> None:
    from controller.reroute_integration import RerouteIntegration

    class Telemetry:
        def apply_forecasts(self, forecasts):
            return 0

        def is_telemetry_stalled(self) -> bool:
            return False

    class Controller:
        config = _Config()
        telemetry = Telemetry()

    assert RerouteIntegration(Controller()).publish_forecasts() == 0
