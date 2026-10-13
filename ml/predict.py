"""Online congestion prediction for the live controller (P8).

This module turns a trained P7 model into something a controller can call on
every telemetry tick. The hard part is not the ``predict_proba`` call -- it is
the four ways an online predictor can quietly lie:

**Stale or missing data.** A link that stopped reporting still *has* a
congestion probability inside the model, because the model has no notion of a
missing sample. Predicting from a stale window is worse than declining to
predict, so a link whose newest sample is older than the stall threshold is
reported as :attr:`Prediction.stale` with no probability, and P9 falls back to
shortest-path for it.

**Short history.** The model was trained on full ``window_w``-sample windows. A
link that just came up has fewer samples; scoring a partially-filled window
would feed the model a distribution it never saw. Those rows are reported as
``insufficient_history`` and left unscored.

**Feature drift.** A saved model records the feature names it was trained on. If
the allowlist in config has since changed, the columns arriving here are a
*different* set. Scoring them anyway produces confident nonsense, so the mismatch
is refused outright.

**Latency.** The controller cannot afford to block. Every scoring pass is timed
against ``ml.max_inference_ms``; if the model is too slow, the predictor reports
``degraded`` so the caller can fall back rather than stall the control loop.

Usage
-----
    predictor = CongestionPredictor.from_config()
    for pred in predictor.predict(telemetry_rows):
        if pred.congested:
            reroute(pred.link_key)
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("ml.predict")

#: Link identity used throughout: (src_switch, dst_switch, direction).
LinkKey = Tuple[str, str, str]


class ModelUnavailable(RuntimeError):
    """Raised when no usable model bundle can be loaded."""


@dataclass
class Prediction:
    """The forecast for one link at one instant.

    Attributes
    ----------
    link_key:
        ``(src_switch, dst_switch, direction)``.
    probability:
        Probability the link congests within the horizon, or ``None`` when no
        score was produced (``stale``, ``insufficient_history`` or
        ``degraded``). ``None`` is deliberately distinct from ``0.0``: "I do not
        know" and "I know it is fine" must not drive the same decision.
    congested:
        Whether this link should be treated as predicted-congested, i.e.
        ``probability >= decision_threshold``.
    reason:
        Why no score was produced, when applicable.
    latency_ms:
        Wall time spent scoring this batch, for the whole call (see
        :class:`CongestionPredictor`).
    """

    link_key: LinkKey
    probability: Optional[float] = None
    congested: bool = False
    reason: str = ""
    n_samples: int = 0
    lead_time_s: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view with the link key flattened."""
        src, dst, direction = self.link_key
        return {
            "src_switch": src,
            "dst_switch": dst,
            "direction": direction,
            "probability": self.probability,
            "congested": self.congested,
            "reason": self.reason,
            "n_samples": self.n_samples,
            "lead_time_s": self.lead_time_s,
        }


@dataclass
class PredictionBatch:
    """One scoring pass over every link the controller knows about."""

    predictions: List[Prediction] = field(default_factory=list)
    latency_ms: float = 0.0
    degraded: bool = False
    within_budget: bool = True

    def by_link(self) -> Dict[LinkKey, Prediction]:
        """Index the batch by link key."""
        return {p.link_key: p for p in self.predictions}

    def congested_links(self) -> List[LinkKey]:
        """Links predicted to congest, in a stable order for reproducible tests."""
        return sorted(
            (p.link_key for p in self.predictions if p.congested),
        )

    def unscored(self) -> List[Prediction]:
        """Links that got no probability, with the reason attached."""
        return [p for p in self.predictions if p.probability is None]

    def as_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view of the whole pass."""
        return {
            "predictions": [p.as_dict() for p in self.predictions],
            "latency_ms": round(self.latency_ms, 3),
            "degraded": self.degraded,
            "within_budget": self.within_budget,
            "n_congested": len(self.congested_links()),
            "n_unscored": len(self.unscored()),
        }


class CongestionPredictor:
    """Scores live telemetry rows with a trained model.

    Parameters
    ----------
    model:
        A fitted estimator exposing ``predict_proba``.
    feature_names:
        Exact feature column order the model was trained on. Used to refuse a
        mismatched matrix.
    decision_threshold:
        Probability at or above which a link counts as congested.
    window_w:
        Samples required per link for a full feature window.
    sample_interval_s:
        Telemetry cadence, used for the staleness deadline and slope units.
    horizon_n_s:
        The prediction horizon, reported alongside each score so P9 can reason
        about how much warning it has.
    max_inference_ms:
        Per-pass latency budget. Exceeding it sets ``degraded`` rather than
        blocking the controller.
    """

    def __init__(
        self,
        model: Any,
        feature_names: Sequence[str],
        decision_threshold: float = 0.5,
        window_w: int = 12,
        sample_interval_s: float = 2.0,
        horizon_n_s: float = 10.0,
        max_inference_ms: float = 50.0,
    ) -> None:
        if not hasattr(model, "predict_proba"):
            raise ModelUnavailable(
                f"{type(model).__name__} has no predict_proba; it is not a "
                "fitted classifier"
            )
        self.model = model
        self.feature_names = list(feature_names)
        self.decision_threshold = float(decision_threshold)
        self.window_w = int(window_w)
        self.sample_interval_s = float(sample_interval_s)
        self.horizon_n_s = float(horizon_n_s)
        self.max_inference_ms = float(max_inference_ms)

    # ------------------------------------------------------------- loading --
    @classmethod
    def from_bundle(cls, bundle: Dict[str, Any], **overrides: Any) -> "CongestionPredictor":
        """Build a predictor from a :func:`ml.train.save_model` bundle.

        Parameters
        ----------
        bundle:
            The deserialised mapping, containing at least ``model`` and
            ``feature_names``.
        overrides:
            Any constructor argument to take from the live caller instead
            (typically ``max_inference_ms`` from the current config).

        Raises
        ------
        ModelUnavailable
            If the bundle is missing the model or its feature names.
        """
        if "model" not in bundle:
            raise ModelUnavailable("model bundle has no 'model' key")
        if not bundle.get("feature_names"):
            raise ModelUnavailable(
                "model bundle has no 'feature_names'; refusing to guess the "
                "column order"
            )
        kwargs: Dict[str, Any] = {
            "decision_threshold": bundle.get("decision_threshold", 0.5),
            "window_w": bundle.get("window_w", 12),
            "sample_interval_s": bundle.get("sample_interval_s", 2.0),
            "horizon_n_s": bundle.get("horizon_n_s", 10.0),
        }
        kwargs.update(overrides)
        return cls(model=bundle["model"],
                   feature_names=bundle["feature_names"], **kwargs)

    @classmethod
    def from_path(cls, path: str | Path, **overrides: Any) -> "CongestionPredictor":
        """Load a bundle from disk with :mod:`joblib`.

        Raises
        ------
        FileNotFoundError
            If the bundle does not exist.
        ModelUnavailable
            If the bundle is malformed.
        """
        import joblib

        model_path = Path(path)
        if not model_path.is_file():
            raise FileNotFoundError(
                f"no trained model at {model_path}. Run `make train` first."
            )
        return cls.from_bundle(joblib.load(model_path), **overrides)

    @classmethod
    def from_config(cls, config: Any = None) -> "CongestionPredictor":
        """Build from ``config.yaml`` and the configured model path."""
        import sys

        if config is None:
            root = Path(__file__).resolve().parents[1]
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            from common.config import load_config

            config = load_config()

        from ml.train import _config_path

        model_path = _config_path(config, "ml.model_path", "results/model.joblib")
        return cls.from_path(
            model_path,
            max_inference_ms=float(config.get("ml.max_inference_ms", 50.0)),
        )

    # ------------------------------------------------------------- features --
    def build_matrix(
        self, rows: Sequence[Dict[str, Any]],
    ) -> Tuple[List[LinkKey], List[List[float]]]:
        """Build one feature row per link from the supplied telemetry.

        Each link is evaluated independently on its own trailing window. Links
        without a full window are omitted; :meth:`predict` reports them
        explicitly rather than scoring a partial window.
        """
        from ml.features import FeatureBuilder

        builder = FeatureBuilder(
            window_w=self.window_w,
            sample_interval_s=self.sample_interval_s,
            # At inference time there is no label to compute, so the horizon is
            # only carried for reporting. It is set to the smallest legal value.
            horizon_n_s=max(self.horizon_n_s, self.sample_interval_s + 1.0),
            label_gap_s=self.sample_interval_s,
            fields=self._field_names(),
        )
        # ``include_invalid`` keeps rows whose (irrelevant) horizon is
        # unobserved -- at inference time every row is "unobserved" by
        # construction, since we are predicting past the end of the data.
        built = builder.build(rows)
        keys: List[LinkKey] = []
        matrix: List[List[float]] = []
        for item in built:
            if not item.horizon_valid:
                keys.append(item.link_key)
                matrix.append(item.values)
        return keys, matrix

    def _field_names(self) -> List[str]:
        """Recover the base field names from the stored feature column names."""
        fields: List[str] = []
        for name in self.feature_names:
            base = name.rsplit("__", 1)[0]
            if base not in fields:
                fields.append(base)
        return fields

    def validate_feature_names(self, names: Sequence[str]) -> None:
        """Raise unless ``names`` matches the model's training columns exactly.

        Raises
        ------
        ModelUnavailable
            On any difference, including order. A reordered matrix produces
            confident, wrong numbers rather than an error.
        """
        if list(names) != self.feature_names:
            missing = set(self.feature_names) - set(names)
            extra = set(names) - set(self.feature_names)
            raise ModelUnavailable(
                "feature set does not match the trained model "
                f"(missing={sorted(missing)}, unexpected={sorted(extra)}, "
                "order_mismatch="
                f"{not missing and not extra}). Refusing to score."
            )

    # ------------------------------------------------------------- scoring --
    def predict(
        self,
        rows: Sequence[Dict[str, Any]],
        now: Optional[float] = None,
    ) -> PredictionBatch:
        """Score every link represented in ``rows``.

        Parameters
        ----------
        rows:
            Recent telemetry records, as written by
            :class:`controller.telemetry.TelemetryCollector`. Several samples
            per link are expected -- at least ``window_w``.
        now:
            Current time, used for the staleness deadline. Defaults to the
            newest sample's timestamp, which keeps the function deterministic
            and testable.
        """
        batch = PredictionBatch()
        if not rows:
            batch.predictions = []
            return batch

        newest = max(float(r["timestamp"]) for r in rows)
        reference = newest if now is None else float(now)
        # A link is stale if its newest sample is older than three cadences --
        # the same multiple the controller uses for its own stall detection.
        deadline = reference - 3.0 * self.sample_interval_s

        # Newest sample per link, so every link is reported exactly once even
        # when it has only a single sample.
        latest: Dict[LinkKey, Dict[str, Any]] = {}
        counts: Dict[LinkKey, int] = {}
        for row in rows:
            key = link_key_of(row)
            latest[key] = row
            counts[key] = counts.get(key, 0) + 1

        keys, matrix = self.build_matrix(rows)
        if matrix:
            row_width = len(matrix[0])
            if row_width != len(self.feature_names):
                raise ModelUnavailable(
                    f"built {row_width} features but the model expects "
                    f"{len(self.feature_names)}; the config allowlist and the "
                    "trained model have diverged"
                )

        started = time.perf_counter()
        probabilities: List[Optional[float]] = [None] * len(keys)
        if matrix:
            batch.within_budget = True
            probabilities = [
                float(p) for p in self.model.predict_proba(matrix)[:, 1]
            ]
        batch.latency_ms = (time.perf_counter() - started) * 1000.0
        batch.within_budget = batch.latency_ms <= self.max_inference_ms
        batch.degraded = bool(matrix) and not batch.within_budget
        if batch.degraded:
            LOGGER.warning(
                "inference took %.2fms, over the %.2fms budget; reporting as "
                "degraded so the caller can fall back to shortest path",
                batch.latency_ms, self.max_inference_ms,
            )
            probabilities = [None] * len(keys)

        by_link: Dict[LinkKey, float] = dict(zip(keys, probabilities))
        for key, newest_row in sorted(latest.items()):
            n = counts[key]
            if batch.degraded:
                batch.predictions.append(Prediction(
                    key, None, False, "degraded_inference", n))
            elif float(newest_row["timestamp"]) < deadline:
                batch.predictions.append(Prediction(
                    key, None, False, "stale", n))
            elif key not in by_link:
                reason = ("insufficient_history" if n < self.window_w
                          else "missing_features")
                batch.predictions.append(Prediction(key, None, False, reason, n))
            else:
                prob = by_link[key]
                batch.predictions.append(Prediction(
                    key, prob, prob >= self.decision_threshold, "", n))

        return batch


def link_key_of(row: Dict[str, Any]) -> LinkKey:
    """Extract the link key from a raw telemetry row.

    A thin alias for :meth:`ml.features.FeatureBuilder.link_key` so callers do
    not have to import the builder just to identify a row.
    """
    from ml.features import FeatureBuilder

    return FeatureBuilder.link_key(row)


def predict_from_config(rows: Sequence[Dict[str, Any]]) -> PredictionBatch:
    """Convenience entry point used by the controller and by P9's tests."""
    return CongestionPredictor.from_config().predict(rows)
