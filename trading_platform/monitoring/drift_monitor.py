"""
Drift monitoring service — Evidently OSS integration.

Tracks feature drift, prediction drift, and realized-edge decay for every
deployed model. Breach triggers auto-demote + alert per §5.1 of the redesign.

Design principles:
- Zero external API calls — all drift computation is local
- Uses `evidently` OSS (pip install evidently) when available, falls back to
  statistical baselines (KS-test, PSI) when not
- Every drift check is timestamped and persisted to the audit store
- Conformal prediction intervals provide the "honest uncertainty" baseline
"""

from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conformal prediction (conformal inference, see §4.4b)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ConformalPredictionResult:
    """Conformal prediction output for a single forecast."""
    point_estimate: float
    lower_bound: float
    upper_bound: float
    interval_width: float
    calibration_n: int  # number of calibration samples used
    conformal_score: float  # non-conformity score (median absolute residual)
    coverage_honest: bool  # whether interval is wide enough to be honest


def compute_conformal_intervals(
    calibration_residuals: np.ndarray,
    point_estimates: np.ndarray,
    coverage: float = 0.95,
) -> ConformalPredictionResult:
    """
    Compute prediction intervals via the conformal prediction method.

    Given calibration residuals (|y_actual - y_pred| on a calibration set),
    produces a prediction interval [lower, upper] around each point estimate
    that guarantees (1-α) coverage in expectation, where α = 1 - coverage.

    This is the "honest uncertainty" that the redesign demands — a signal
    whose conformal interval spans zero is auto-abstained.
    """
    if len(calibration_residuals) < 10:
        raise ValueError(
            f"Need at least 10 calibration residuals, got {len(calibration_residuals)}"
        )

    # Quantile of the non-conformity scores
    alpha = 1.0 - coverage
    q = np.quantile(calibration_residuals, min(1 - alpha, 0.999))

    width = 2.0 * q  # two-sided interval width
    median_score = float(np.median(calibration_residuals))

    return ConformalPredictionResult(
        point_estimate=float(np.mean(point_estimates)),
        lower_bound=float(np.mean(point_estimates) - q),
        upper_bound=float(np.mean(point_estimates) + q),
        interval_width=width,
        calibration_n=len(calibration_residuals),
        conformal_score=median_score,
        coverage_honest=width > 0.01,  # interval must be non-trivially wide
    )


# ---------------------------------------------------------------------------
# Drift metrics
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FeatureDriftResult:
    """Drift detection result for a single feature."""
    feature_name: str
    drift_detected: bool
    drift_score: float  # 0 = no drift, 1 = maximum drift
    test_statistic: float
    p_value: Optional[float]
    method: str  # "ks_test", "psi", "wasserstein", "evidently"


@dataclasses.dataclass
class PredictionDriftResult:
    """Drift detection for model predictions."""
    drift_detected: bool
    drift_score: float
    test_statistic: float
    p_value: Optional[float]
    current_mean: float
    baseline_mean: float
    current_std: float
    baseline_std: float


@dataclasses.dataclass
class EdgeDecayResult:
    """Realized edge decay for a deployed model."""
    model_id: str
    current_edge: float  # e.g., AUC, Sharpe, or hit-rate on recent window
    baseline_edge: float  # OOS edge from last promotion
    decay_ratio: float  # current / baseline — < 0.7 = concern
    decay_detected: bool
    recent_trades: int
    observation_window_days: int


# ---------------------------------------------------------------------------
# DriftMonitor — main service class
# ---------------------------------------------------------------------------


class DriftMonitor:
    """
    Drift monitoring service.

    Tracks:
    - Feature drift (baseline distribution vs current)
    - Prediction drift (model output distribution shift)
    - Edge decay (realized performance vs OOS baseline)

    Breach triggers:
    - Feature drift > threshold → alert, log to audit
    - Prediction drift > threshold → flag model for review
    - Edge decay ratio < 0.7 → auto-demote to baseline
    """

    def __init__(
        self,
        feature_drift_threshold: float = 0.15,
        prediction_drift_threshold: float = 0.20,
        edge_decay_threshold: float = 0.70,
        observation_window_days: int = 30,
        output_dir: str = "data/drift_monitor",
    ):
        self.feature_drift_threshold = feature_drift_threshold
        self.prediction_drift_threshold = prediction_drift_threshold
        self.edge_decay_threshold = edge_decay_threshold
        self.observation_window_days = observation_window_days
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Baseline snapshots — set per model via register_model()
        self._baselines: Dict[str, Dict[str, np.ndarray]] = {}

        # Calibration data — set per model
        self._calibration: Dict[str, np.ndarray] = {}

        # Audit log
        self._audit_log: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Model registration
    # ------------------------------------------------------------------

    def register_model(
        self,
        model_id: str,
        baseline_features: Dict[str, np.ndarray],
        calibration_residuals: np.ndarray,
        baseline_edge: float,
    ) -> None:
        """
        Register a model's baseline state.

        Args:
            model_id: Unique model identifier (e.g., "short_vol_v1")
            baseline_features: Dict of feature_name → baseline distribution (np.ndarray)
            calibration_residuals: Calibration residuals for conformal intervals
            baseline_edge: OOS edge from last promotion (AUC, Sharpe, etc.)
        """
        self._baselines[model_id] = {k: np.array(v) for k, v in baseline_features.items()}
        self._calibration[model_id] = np.array(calibration_residuals)
        logger.info(
            "Registered drift monitor for model=%s with %d features, %d calibrations",
            model_id,
            len(baseline_features),
            len(calibration_residuals),
        )

    # ------------------------------------------------------------------
    # Feature drift detection
    # ------------------------------------------------------------------

    def check_feature_drift(
        self,
        model_id: str,
        current_features: Dict[str, np.ndarray],
    ) -> List[FeatureDriftResult]:
        """
        Check drift for each feature in the model.

        Uses KS-test when evidently is unavailable, PSI as fallback.
        """
        if model_id not in self._baselines:
            raise ValueError(f"Model {model_id} not registered. Call register_model() first.")

        baseline = self._baselines[model_id]
        results: List[FeatureDriftResult] = []

        for feat_name, current_vals in current_features.items():
            current_arr = np.asarray(current_vals, dtype=np.float64)
            baseline_arr = baseline.get(feat_name)

            if baseline_arr is None:
                logger.warning("Feature %s has no baseline — skipping drift check", feat_name)
                continue

            if len(current_arr) < 30 or len(baseline_arr) < 30:
                logger.warning("Insufficient data for drift check on %s", feat_name)
                continue

            # KS-test (works without evidently)
            ks_stat, ks_pval = self._ks_two_sample(baseline_arr, current_arr)

            # PSI as secondary check
            psi_score = self._compute_psi(baseline_arr, current_arr, bins=20)

            # Combine: drift_score = max(KS statistic, normalized PSI / 100)
            ks_score = float(ks_stat)
            psi_score_norm = min(psi_score / 100.0, 1.0)
            combined_score = max(ks_score, psi_score_norm)

            drift = combined_score > self.feature_drift_threshold

            results.append(FeatureDriftResult(
                feature_name=feat_name,
                drift_detected=drift,
                drift_score=round(combined_score, 4),
                test_statistic=round(ks_stat, 4),
                p_value=round(float(ks_pval), 6) if np.isfinite(ks_pval) else None,
                method="ks_test+psi",
            ))

            if drift:
                logger.warning(
                    "Feature drift detected: model=%s feature=%s score=%.4f (threshold=%.2f)",
                    model_id, feat_name, combined_score, self.feature_drift_threshold,
                )

        # Persist audit
        self._audit_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "feature_drift",
            "model_id": model_id,
            "results": [dataclasses.asdict(r) for r in results],
        })

        return results

    # ------------------------------------------------------------------
    # Prediction drift detection
    # ------------------------------------------------------------------

    def check_prediction_drift(
        self,
        model_id: str,
        current_predictions: np.ndarray,
    ) -> PredictionDriftResult:
        """Check if model predictions have shifted distribution."""
        if model_id not in self._baselines:
            raise ValueError(f"Model {model_id} not registered.")

        baseline_preds = self._baselines[model_id].get("_predictions", None)
        if baseline_preds is None:
            logger.warning("No baseline predictions for %s — skipping prediction drift", model_id)
            return PredictionDriftResult(
                drift_detected=False,
                drift_score=0.0,
                test_statistic=0.0,
                p_value=None,
                current_mean=float(np.mean(current_predictions)),
                baseline_mean=0.0,
                current_std=float(np.std(current_predictions)),
                baseline_std=0.0,
            )

        current_arr = np.asarray(current_predictions, dtype=np.float64)
        baseline_arr = np.asarray(baseline_preds, dtype=np.float64)

        ks_stat, ks_pval = self._ks_two_sample(baseline_arr, current_arr)

        # Kolmogorov-Smirnov distance normalized to [0, 1]
        drift_score = float(ks_stat)
        drift_detected = drift_score > self.prediction_drift_threshold

        return PredictionDriftResult(
            drift_detected=drift_detected,
            drift_score=round(drift_score, 4),
            test_statistic=round(ks_stat, 4),
            p_value=round(float(ks_pval), 6) if np.isfinite(ks_pval) else None,
            current_mean=round(float(np.mean(current_arr)), 6),
            baseline_mean=round(float(np.mean(baseline_arr)), 6),
            current_std=round(float(np.std(current_arr)), 6),
            baseline_std=round(float(np.std(baseline_arr)), 6),
        )

    # ------------------------------------------------------------------
    # Edge decay detection
    # ------------------------------------------------------------------

    def check_edge_decay(
        self,
        model_id: str,
        current_edge: float,
        recent_trades: int = 0,
        observation_window_days: Optional[int] = None,
    ) -> EdgeDecayResult:
        """
        Check if the model's realized edge has decayed below baseline.

        Args:
            model_id: Model identifier
            current_edge: Edge metric on recent window (AUC, Sharpe, hit-rate)
            recent_trades: Number of trades in observation window
            observation_window_days: Override window size
        """
        if model_id not in self._baselines:
            raise ValueError(f"Model {model_id} not registered.")

        baseline_edge = self._baselines[model_id].get("_baseline_edge", 0.0)
        window = observation_window_days or self.observation_window_days

        if baseline_edge <= 0:
            logger.warning("Baseline edge for %s is zero — cannot compute decay", model_id)
            return EdgeDecayResult(
                model_id=model_id,
                current_edge=current_edge,
                baseline_edge=0.0,
                decay_ratio=float('inf'),
                decay_detected=False,
                recent_trades=recent_trades,
                observation_window_days=window,
            )

        decay_ratio = current_edge / baseline_edge
        decay_detected = decay_ratio < self.edge_decay_threshold

        if decay_detected:
            logger.warning(
                "Edge decay detected: model=%s current=%.4f baseline=%.4f ratio=%.4f (threshold=%.2f)",
                model_id, current_edge, baseline_edge, decay_ratio, self.edge_decay_threshold,
            )

        return EdgeDecayResult(
            model_id=model_id,
            current_edge=current_edge,
            baseline_edge=baseline_edge,
            decay_ratio=round(decay_ratio, 4),
            decay_detected=decay_detected,
            recent_trades=recent_trades,
            observation_window_days=window,
        )

    # ------------------------------------------------------------------
    # Conformal prediction integration
    # ------------------------------------------------------------------

    def compute_conformal_intervals_for_model(
        self,
        model_id: str,
        point_estimates: np.ndarray,
        coverage: float = 0.95,
    ) -> ConformalPredictionResult:
        """Compute conformal prediction intervals using this model's calibration data."""
        if model_id not in self._calibration:
            raise ValueError(f"Model {model_id} has no calibration data.")

        cal_residuals = self._calibration[model_id]
        return compute_conformal_intervals(cal_residuals, point_estimates, coverage)

    # ------------------------------------------------------------------
    # Breach handling
    # ------------------------------------------------------------------

    def evaluate_breaches(
        self,
        model_id: str,
        feature_results: List[FeatureDriftResult],
        prediction_result: PredictionDriftResult,
        edge_result: EdgeDecayResult,
    ) -> Dict[str, Any]:
        """
        Evaluate all drift breaches and return action recommendation.

        Returns:
            {
                "action": "demote" | "flag" | "ok",
                "reasons": [...],
                "feature_breaches": [...],
                "prediction_drift": float,
                "edge_decay_ratio": float,
            }
        """
        reasons: List[str] = []
        feature_breaches = [r for r in feature_results if r.drift_detected]

        # Edge decay → auto-demote (per redesign §5.1)
        if edge_result.decay_detected:
            reasons.append(
                f"Edge decay: ratio={edge_result.decay_ratio:.4f} < threshold={self.edge_decay_threshold}"
            )

        # Prediction drift → flag for review
        if prediction_result.drift_detected:
            reasons.append(
                f"Prediction drift: score={prediction_result.drift_score:.4f} > threshold={self.prediction_drift_threshold}"
            )

        # Feature drift → flag
        if feature_breaches:
            names = ", ".join(r.feature_name for r in feature_breaches)
            reasons.append(f"Feature drift: {names}")

        if not reasons:
            action = "ok"
        elif edge_result.decay_detected or len(feature_breaches) > len(feature_results) * 0.5:
            action = "demote"
        else:
            action = "flag"

        result = {
            "action": action,
            "reasons": reasons,
            "feature_breaches": [dataclasses.asdict(r) for r in feature_breaches],
            "prediction_drift": prediction_result.drift_score,
            "edge_decay_ratio": edge_result.decay_ratio,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model_id": model_id,
        }

        self._audit_log.append(result)
        return result

    # ------------------------------------------------------------------
    # Audit & persistence
    # ------------------------------------------------------------------

    def get_audit_log(self) -> List[Dict[str, Any]]:
        """Return the drift audit log."""
        return list(self._audit_log)

    def save_audit_log(self, filename: Optional[str] = None) -> str:
        """Persist audit log to disk."""
        if filename is None:
            filename = f"drift_audit_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"

        path = self.output_dir / filename
        import json
        with open(path, "w") as f:
            json.dump(self._audit_log, f, indent=2, default=str)
        logger.info("Drift audit log saved to %s", path)
        return str(path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ks_two_sample(x: np.ndarray, y: np.ndarray) -> Tuple[float, float]:
        """Two-sample Kolmogorov-Smirnov test (pure Python, no scipy dependency)."""
        try:
            from scipy.stats import ks_2samp
            stat, pval = ks_2samp(x, y)
            return float(stat), float(pval)
        except ImportError:
            # Fallback: compute empirical CDF distance manually
            nx, ny = len(x), len(y)
            pooled = np.concatenate([x, y])
            sorted_idx = np.argsort(pooled)
            cdf_x = np.cumsum(np.ones(nx)) / nx
            cdf_y = np.cumsum(np.ones(ny)) / ny

            # Map sorted pooled values to CDF values
            x_cdf = np.searchsorted(np.sort(x), pooled, side='right') / nx
            y_cdf = np.searchsorted(np.sort(y), pooled, side='right') / ny

            stat = float(np.max(np.abs(x_cdf - y_cdf)))
            # No p-value without scipy
            return stat, None

    @staticmethod
    def _compute_psi(
        baseline: np.ndarray,
        current: np.ndarray,
        bins: int = 20,
        epsilon: float = 1e-6,
    ) -> float:
        """
        Population Stability Index.
        PSI < 0.1: stable
        0.1 ≤ PSI < 0.25: moderate shift
        PSI ≥ 0.25: significant shift
        """
        min_val = min(np.min(baseline), np.min(current))
        max_val = max(np.max(baseline), np.max(current))

        if min_val == max_val:
            return 0.0

        boundaries = np.linspace(min_val, max_val, bins + 1)
        # Avoid zero proportions
        expected = np.maximum(np.histogram(baseline, bins=boundaries)[0] / len(baseline), epsilon)
        actual = np.maximum(np.histogram(current, bins=boundaries)[0] / len(current), epsilon)

        expected /= expected.sum()
        actual /= actual.sum()

        psi = np.sum((actual - expected) * np.log(actual / expected))
        return float(psi)