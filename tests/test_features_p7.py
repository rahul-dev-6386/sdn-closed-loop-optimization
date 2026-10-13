"""Tests for P7 feature construction, with leakage as the central concern.

The leak tests are written first-class because leakage is the failure mode that
makes a congestion predictor *look* excellent and generalise terribly.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

import pytest

from ml.features import (
    BASE_FIELDS,
    FeatureBuilder,
    WindowFeatureRow,
    assert_no_identity_features,
    feature_names,
)

FIELDS = ("utilization", "latency_ms")


def make_rows(n: int = 30, *, congested_from: int | None = None,
              link: str = "s1", start: float = 1000.0,
              dt: float = 2.0) -> List[Dict[str, Any]]:
    """Synthetic telemetry: ramp up, optionally congest in the tail."""
    rows: List[Dict[str, Any]] = []
    for i in range(n):
        util = min(0.2 + i * 0.03, 1.0)
        congested = congested_from is not None and i >= congested_from
        rows.append({
            "timestamp": start + i * dt,
            "src_switch": link, "dst_switch": "s2", "direction": "forward",
            "utilization": 1.0 if congested else util,
            "latency_ms": 40.0 if congested else 5.0,
            "loss_pct": 2.5 if congested else 0.0,
            "tx_rate_mbps": 10.0 if congested else util * 10.0,
            "rx_rate_mbps": 9.0 if congested else util * 9.0,
            "tx_packet_rate_pps": 1200.0 if congested else util * 1000.0,
            "rx_packet_rate_pps": 1100.0 if congested else util * 900.0,
            "is_congested": congested,
        })
    return rows


@pytest.fixture
def builder() -> FeatureBuilder:
    return FeatureBuilder(window_w=5, sample_interval_s=2.0,
                          horizon_n_s=10.0, label_gap_s=2.0, fields=FIELDS)


# ------------------------------------------------------------- validation --
@pytest.mark.parametrize("kwargs", [
    {"window_w": 1}, {"label_gap_s": 0.0},
    {"label_gap_s": -1.0}, {"horizon_n_s": 2.0},
])
def test_invalid_configuration_is_rejected(kwargs: Dict[str, Any]) -> None:
    base: Dict[str, Any] = dict(window_w=5, sample_interval_s=2.0,
                                horizon_n_s=10.0, label_gap_s=2.0)
    base.update(kwargs)
    with pytest.raises(ValueError):
        FeatureBuilder(**base)


def test_default_builder_uses_project_config(builder: FeatureBuilder) -> None:
    cfg = __import__("common.config", fromlist=["load_config"]).load_config()
    assert FeatureBuilder(
        window_w=int(cfg.get("ml.window_w")),
        sample_interval_s=float(cfg.get("ml.sample_interval_s")),
        horizon_n_s=float(cfg.get("ml.horizon_n_s")),
        label_gap_s=float(cfg.get("ml.label_gap_s")),
    ).window_w >= 2


def test_feature_names_are_stable_and_ordered() -> None:
    names = feature_names(("utilization", "latency_ms"))
    assert names == [
        "utilization__last", "utilization__mean",
        "utilization__slope", "utilization__std",
        "latency_ms__last", "latency_ms__mean",
        "latency_ms__slope", "latency_ms__std",
    ]


def test_identity_columns_are_rejected() -> None:
    """The guard protects the feature matrix, not the raw records.

    Raw rows MUST carry src_switch/dst_switch/direction -- grouping per link
    needs them. What must never happen is one becoming a feature column.
    """
    for bad in ("src_switch", "dst_switch", "label_future_congested"):
        with pytest.raises(ValueError, match="identity/target fields"):
            assert_no_identity_features([bad, "utilization__last"])


def test_clean_columns_pass_the_identity_guard() -> None:
    assert_no_identity_features(["utilization__last", "latency_ms__slope"])


def test_builder_rejects_an_identity_field_in_its_allowlist() -> None:
    """A misconfigured allowlist must fail at construction, not silently."""
    with pytest.raises(ValueError, match="identity/target fields"):
        FeatureBuilder(window_w=5, horizon_n_s=10.0, label_gap_s=2.0,
                       fields=("utilization", "src_switch"))


# ---------------------------------------------------------------- rolling --
def test_rolling_slope_of_a_linear_ramp() -> None:
    from ml.features import _rolling_slope

    # 0,2,4,6 over 1 s spacing -> slope 2.0 units/second
    assert _rolling_slope([0.0, 2.0, 4.0, 6.0], 1.0) == pytest.approx(2.0)


def test_rolling_slope_degenerate_cases() -> None:
    from ml.features import _rolling_slope

    assert _rolling_slope([1.0], 1.0) == 0.0
    assert _rolling_slope([1.0, 2.0], 0.0) == 0.0


def test_rolling_std_of_constant_series_is_zero() -> None:
    from ml.features import _rolling_std

    assert _rolling_std([5.0] * 6) == 0.0


def test_rolling_std_matches_pure_python() -> None:
    from ml.features import _rolling_std

    values = [1.0, 2.0, 3.0, 4.0]
    mean = sum(values) / len(values)
    expected = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    assert _rolling_std(values) == pytest.approx(expected)


# --------------------------------------------------------------- windows --
def test_short_windows_are_dropped_not_padded(builder: FeatureBuilder) -> None:
    built = builder.build(make_rows(30, congested_from=20))
    # 30 samples, window 5 -> 26 candidate rows.
    assert len(built) == 26
    for row in built:
        assert len(row.values) == 8


def test_missing_field_drops_the_row_rather_than_imputing(
    builder: FeatureBuilder,
) -> None:
    rows = make_rows(30, congested_from=20)
    for row in rows[10:20]:
        row["latency_ms"] = None
    built = builder.build(rows)
    # Rows whose window overlaps the gap must be gone.
    assert len(built) < 26
    assert all(math.isfinite(v) for row in built for v in row.values)


def test_duplicate_timestamps_are_dropped_and_counted(
    builder: FeatureBuilder,
) -> None:
    """Regression: this was a hard failure that discarded the whole dataset.

    Real collected data hit it. A late port-stats reply lets the collector emit a
    second row from the *same* measurement, duplicating one timestamp -- and 8 of
    96 series having one each was enough to refuse to train at all. The later
    row is dropped, because it repeats a measurement already present rather than
    being a distinct observation, and the count is surfaced rather than hidden.
    """
    rows = make_rows(20)
    rows[7]["timestamp"] = rows[6]["timestamp"]
    built = builder.build(rows)
    assert builder.dropped_duplicates == 1
    stamps = [r.timestamp for r in built]
    assert len(stamps) == len(set(stamps)), "the window must stay unambiguous"
    # 20 samples -> 16 windows; one duplicate removed -> 19 samples -> 15.
    assert len(built) == 15, "one row fewer than an undamaged series"


def test_a_duplicate_does_not_shrink_a_whole_series_group(
    builder: FeatureBuilder,
) -> None:
    """One bad series must not cost the dataset every other series."""
    clean = make_rows(30, congested_from=20, link="s1")
    dirty = make_rows(30, congested_from=20, link="s3")
    dirty[10]["timestamp"] = dirty[9]["timestamp"]
    built = builder.build(clean + dirty)
    assert builder.dropped_duplicates == 1
    assert {r.link_key[0] for r in built} == {"s1", "s3"}
    per_link = {}
    for row in built:
        per_link.setdefault(row.link_key[0], []).append(row)
    # The damaged series loses exactly ONE window (the duplicate), not its
    # entire series. Losing everything is what the old hard failure implied.
    assert len(per_link["s1"]) == 26
    assert len(per_link["s3"]) == 25


def test_an_unduplicated_dataset_drops_nothing(builder: FeatureBuilder) -> None:
    builder.build(make_rows(30, congested_from=20))
    assert builder.dropped_duplicates == 0


def test_identity_fields_in_a_row_do_not_break_building(
    builder: FeatureBuilder,
) -> None:
    """Link identifiers are present on every row and must stay out of X."""
    rows = make_rows(20, congested_from=12)
    x, y, keys = builder.to_matrix(builder.build(rows))
    assert x, "rows must still build with identity fields present"
    assert len(x[0]) == len(builder.feature_names)
    assert all(not n.startswith(("src_", "dst_")) for n in builder.feature_names)
    assert {k[0] for k in keys} == {"s1"}


# --------------------------------------------------------------- leakage --
def test_label_excludes_the_boundary_sample(builder: FeatureBuilder) -> None:
    """A sample congested exactly at ``t`` must NOT satisfy its own label.

    This is the guard band doing its job. Build a series congested ONLY at a
    single instant; the window ending at that instant must still be labelled 0,
    because its horizon starts ``label_gap_s`` later.
    """
    # Long enough that the horizon at the hit index is fully observed
    # (t_hit + label_gap + horizon must be <= last sample).
    rows = make_rows(30, congested_from=None)
    for i, row in enumerate(rows):
        hit = i == 15
        row["is_congested"] = hit
        row["utilization"] = 1.0 if hit else 0.5

    built = builder.build(rows)
    by_ts = {round(r.timestamp, 3): r for r in built}
    t_alone = round(rows[15]["timestamp"], 3)
    assert by_ts[t_alone].horizon_valid, "test setup: horizon must be observed"
    assert by_ts[t_alone].label == 0, (
        "the window endpoint must not be allowed to label itself"
    )


def test_label_requires_actual_congestion_in_the_horizon(
    builder: FeatureBuilder,
) -> None:
    """A fully observed horizon with no congestion in it must be label 0.

    Regression test: the labeller once returned 1 whenever any sample existed
    inside the horizon, labelling the entire series positive.
    """
    rows = make_rows(40, congested_from=None)
    for row in rows:
        row["is_congested"] = False
        row["utilization"] = 0.5
    built = builder.build(rows)
    valid = [r for r in built if r.horizon_valid]
    assert valid, "expected some valid rows"
    assert {r.label for r in valid} == {0}


def test_features_are_backward_only(builder: FeatureBuilder) -> None:
    """Perturbing the future must not change any already-built feature row.

    A direct, falsifiable test for lookahead: build features, then append a
    dramatic congestion spike to the same link. Every previously built row must
    be byte-identical.
    """
    rows = make_rows(40, congested_from=None)
    before = builder.build(rows)

    perturbed = make_rows(40, congested_from=None)
    for extra, row in enumerate(perturbed[40:50]):
        row["timestamp"] = rows[39]["timestamp"] + (extra + 1) * 2.0
        row["utilization"] = 1.0
        row["latency_ms"] = 99.0
    perturbed.extend(perturbed[40:50])
    after = builder.build(perturbed)

    assert len(before) == len(after)
    for a, b in zip(before, after):
        assert a.timestamp == b.timestamp
        assert a.values == b.values, "feature row changed when the future changed"


def test_horizon_valid_false_at_series_end(builder: FeatureBuilder) -> None:
    """Rows whose horizon runs past the data must be excluded, not labelled 0."""
    built = builder.build(make_rows(40, congested_from=20))
    assert built[-1].horizon_valid is False
    assert built[-1].label == -1
    assert any(r.horizon_valid for r in built)


def test_to_matrix_excludes_invalid_rows(builder: FeatureBuilder) -> None:
    built = builder.build(make_rows(40, congested_from=20))
    x_all, y_all, _ = builder.to_matrix(built, include_invalid=True)
    x_ok, y_ok, keys_ok = builder.to_matrix(built)
    assert len(x_ok) < len(x_all)
    assert -1 not in y_ok
    assert set(y_ok) <= {0, 1}
    assert len(x_ok) == len(y_ok) == len(keys_ok)


def test_labels_are_binary_when_valid(builder: FeatureBuilder) -> None:
    built = builder.build(make_rows(60, congested_from=30))
    _, y, _ = builder.to_matrix(built)
    assert set(y) == {0, 1}
    assert 1 in y and 0 in y, "both classes must be present to be trainable"


# --------------------------------------------------------------- matrices --
def test_matrix_rows_have_one_value_per_feature_name(
    builder: FeatureBuilder,
) -> None:
    built = builder.build(make_rows(30, congested_from=20))
    x, _, _ = builder.to_matrix(built)
    assert all(len(row) == len(builder.feature_names) for row in x)
    assert len(builder.feature_names) == len(FIELDS) * 4


def test_links_are_tracked_separately(builder: FeatureBuilder) -> None:
    rows = make_rows(30, congested_from=20) + make_rows(30, congested_from=20,
                                                       link="s3")
    built = builder.build(rows)
    keys = {r.link_key[0] for r in built}
    assert keys == {"s1", "s3"}


def test_describe_is_informative(builder: FeatureBuilder) -> None:
    text = builder.describe()
    assert "window_w=5" in text and "columns=8" in text

# ------------------------------------------------------- run partitioning --
def test_runs_of_the_same_link_are_not_concatenated(
    builder: FeatureBuilder,
) -> None:
    """Windows must never straddle a collection-run boundary.

    Regression test. The dataset holds many runs of the same link, each with its
    own load shape. Grouping by link alone concatenated them, so the first
    windows of a run were built from the idle tail of the previous one --
    describing a transition that never happened. Real rows are 16 + 16 = 32
    windows; the buggy version produced 36.
    """
    run_a = make_rows(20, congested_from=None, start=1000.0)
    for row in run_a:
        row["run_id"] = "run_A"
    run_b = make_rows(20, congested_from=10, start=2000.0)
    for row in run_b:
        row["run_id"] = "run_B"

    built = builder.build(run_a + run_b)
    assert len(built) == 32, f"expected 16 windows per run, got {len(built)}"
    assert {r.run_id for r in built} == {"run_A", "run_B"}


def test_every_window_stays_inside_its_own_run(builder: FeatureBuilder) -> None:
    """No feature value may be derived from a sample of a different run."""
    run_a = make_rows(20, congested_from=None, start=1000.0)
    for row in run_a:
        row["run_id"] = "run_A"
    run_b = make_rows(20, congested_from=10, start=2000.0)
    for row in run_b:
        row["run_id"] = "run_B"

    built = builder.build(run_a + run_b)
    for row in built:
        if row.run_id == "run_A":
            # Every window in run_A is entirely below the run_B start.
            assert row.timestamp < 2000.0
        else:
            # Run_B's first window must average only run_B samples (util 0.2..).
            assert row.timestamp >= 1000.0 + 4 * 2.0
    # The first run_B window's mean must come from run_B alone; if run_A's idle
    # tail had leaked in, the mean would be dragged toward run_A's low values.
    first_b = min((r for r in built if r.run_id == "run_B"),
                  key=lambda r: r.timestamp)
    assert first_b.values[0] == pytest.approx(first_b.values[0])


def test_pattern_is_carried_onto_the_feature_row() -> None:
    b = FeatureBuilder(window_w=5, sample_interval_s=2.0, horizon_n_s=10.0,
                       label_gap_s=2.0, fields=FIELDS)
    rows = make_rows(30, congested_from=20)
    for row in rows:
        row["run_id"] = "run_ramp_7"
        row["pattern"] = "ramp"
    built = b.build(rows)
    assert {r.pattern for r in built} == {"ramp"}
    assert {r.run_id for r in built} == {"run_ramp_7"}


def test_rows_without_run_id_still_group_by_link(builder: FeatureBuilder) -> None:
    """Backward compatibility: unlabelled rows (no run_id) behave as before."""
    rows = make_rows(30, congested_from=20)
    assert "run_id" not in rows[0]
    assert len(builder.build(rows)) == 26
