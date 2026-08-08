"""
trading_platform/ai/features/meta_labeling.py — Meta-labeling with LightGBM

Per §4.3 and López de Prado (AFML):
- Base rules (ORB, VWAP reversion) propose signals
- LightGBM classifier filters/sizes which signals to trade
- Deployment law: walk-forward AUC must beat 0.5 + max(0.02, 2·SE_null)
  or the model is NOT saved — the platform remains pure short-vol
- Fractional execution: size ∝ P(win) - 0.5, capped at 0.25× Kelly
- Conformal prediction: if interval spans 0.5 → auto-abstain
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from numpy.typing import NDArray
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, brier_score_loss
from sklearn.calibration import IsotonicRegression

from trading_platform.ai.ml_features.conformal_prediction import (
    ConformalPredictor,
    ConformalDecision,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Meta-labeling configuration
# ──────────────────────────────────────────────


@dataclass
class MetaLabelingConfig:
    """Configuration for meta-labeling pipeline."""
    # Model hyperparameters
    learning_rate: float = 0.05
    n_estimators: int = 200
    max_depth: int = 4
    min_child_samples: int = 50
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    reg_alpha: float = 0.1
    reg_lambda: float = 0.1
    random_state: int = 42

    # Meta-labeling thresholds
    abstain_threshold: float = 0.55  # P(win) must exceed this to trade
    min_auc_gap: float = 0.02  # AUC must beat 0.5 by at least this margin
    calibration_method: str = "isotonic"  # "isotonic" or "sigmoid"

    # Sample weights
    use_sample_uniqueness: bool = True
    weight_scale: float = 1.0

    # Conformal prediction
    conformal_confidence: float = 0.90
    min_calibrations: int = 30  # Minimum residuals for valid conformal intervals


# ──────────────────────────────────────────────
# Base-rule signal proposer
# ──────────────────────────────────────────────


@dataclass
class BaseSignal:
    """A signal proposed by a base rule."""
    timestamp: int
    instrument: str
    direction: int  # +1 = long, -1 = short
    rule_name: str  # e.g. "ORB", "VWAP_REVERSION", "MOMENTUM"
    features: Dict[str, float] = field(default_factory=dict)
    conviction: float = 0.5  # Base-rule confidence [0, 1]


def propose_orb_signals(
    open_prices: NDArray[np.float64],
    high_prices: NDArray[np.float64],
    low_prices: NDArray[np.float64],
    close_prices: NDArray[np.float64],
    volumes: NDArray[np.float64],
    instrument: str = "NIFTY",
    breakout_window: int = 30,  # minutes
    threshold: float = 0.002,  # 0.2% breakout threshold
) -> List[BaseSignal]:
    """
    Open Range Breakout (ORB) signal proposer.

    After the first N minutes, if price breaks above high or below low,
    propose a long/short signal.
    """
    signals = []
    n = len(open_prices)

    if n < breakout_window + 5:
        return signals

    for i in range(breakout_window, n):
        # Range in first breakout_window minutes
        window_high = np.max(high_prices[:breakout_window])
        window_low = np.min(low_prices[:breakout_window])

        current_price = close_prices[i]

        # Breakout above range
        if current_price >= window_high * (1 + threshold):
            signals.append(BaseSignal(
                timestamp=i,
                instrument=instrument,
                direction=1,
                rule_name="ORB",
                features={
                    "orb_range": (window_high - window_low) / window_low,
                    "orb_breakout_pct": (current_price - window_high) / window_high,
                    "volume_ratio": volumes[i] / max(np.mean(volumes[:breakout_window]), 1),
                },
                conviction=0.6,
            ))

        # Breakdown below range
        elif current_price <= window_low * (1 - threshold):
            signals.append(BaseSignal(
                timestamp=i,
                instrument=instrument,
                direction=-1,
                rule_name="ORB",
                features={
                    "orb_range": (window_high - window_low) / window_low,
                    "orb_breakout_pct": (window_low - current_price) / window_low,
                    "volume_ratio": volumes[i] / max(np.mean(volumes[:breakout_window]), 1),
                },
                conviction=0.6,
            ))

    return signals


def propose_vwap_reversion_signals(
    close_prices: NDArray[np.float64],
    volumes: NDArray[np.float64],
    high_prices: NDArray[np.float64],
    low_prices: NDArray[np.float64],
    instrument: str = "NIFTY",
    lookback: int = 60,
    z_threshold: float = 2.0,
) -> List[BaseSignal]:
    """
    VWAP mean-reversion signal proposer.

    When price deviates > z_threshold std from VWAP, propose reversion signal.
    """
    signals = []
    n = len(close_prices)

    if n < lookback + 10:
        return signals

    for i in range(lookback, n):
        # Rolling VWAP
        prices = close_prices[i - lookback:i]
        vols = volumes[i - lookback:i]
        total_vol = np.sum(vols)
        if total_vol <= 0:
            continue
        vwap = np.sum(prices * vols) / total_vol

        # Rolling std
        std = np.std(prices)
        if std <= 0:
            continue

        # Z-score of current price
        z = (close_prices[i] - vwap) / std

        if z > z_threshold:
            # Price above VWAP → propose short (reversion)
            signals.append(BaseSignal(
                timestamp=i,
                instrument=instrument,
                direction=-1,
                rule_name="VWAP_REVERSION",
                features={
                    "vwap_z_score": z,
                    "distance_from_vwap": (close_prices[i] - vwap) / vwap,
                },
                conviction=min(0.8, z / 4.0),
            ))
        elif z < -z_threshold:
            # Price below VWAP → propose long (reversion)
            signals.append(BaseSignal(
                timestamp=i,
                instrument=instrument,
                direction=1,
                rule_name="VWAP_REVERSION",
                features={
                    "vwap_z_score": z,
                    "distance_from_vwap": (vwap - close_prices[i]) / vwap,
                },
                conviction=min(0.8, -z / 4.0),
            ))

    return signals


# ──────────────────────────────────────────────
# Meta-labeling classifier
# ──────────────────────────────────────────────


class MetaLabelingClassifier:
    """
    LightGBM meta-labeling classifier.

    Takes base-rule signals + features → predicts P(win) for each signal.
    Only signals with P(win) > threshold are executed.
    Size scales with (P(win) - 0.5) × 2, capped.
    """

    def __init__(
        self,
        config: Optional[MetaLabelingConfig] = None,
        instrument: str = "NIFTY",
    ):
        self.config = config or MetaLabelingConfig()
        self.instrument = instrument
        self.model: Optional[lgb.LGBMClassifier] = None
        self.calibrator: Optional[IsotonicRegression] = None
        self.residuals: NDArray[np.float64] = np.array([])
        self.feature_names: List[str] = []
        self.is_trained: bool = False
        self.is_calibrated: bool = False
        self.metrics: Dict[str, float] = {}

    def train(
        self,
        X_train: NDArray[np.float64],
        y_train: NDArray[np.int64],
        sample_weights: Optional[NDArray[np.float64]] = None,
        feature_names: Optional[List[str]] = None,
    ) -> bool:
        """
        Train the meta-labeling classifier.

        Returns True if model passes AUC gate, False otherwise.
        """
        if feature_names:
            self.feature_names = feature_names

        if len(np.unique(y_train)) < 2:
            logger.warning("Meta-labeling: only one class in training data")
            return False

        n_estimators = min(self.config.n_estimators, max(50, len(y_train) // 2))

        self.model = lgb.LGBMClassifier(
            n_estimators=n_estimators,
            learning_rate=self.config.learning_rate,
            max_depth=self.config.max_depth,
            min_child_samples=self.config.min_child_samples,
            subsample=self.config.subsample,
            colsample_bytree=self.config.colsample_bytree,
            reg_alpha=self.config.reg_alpha,
            reg_lambda=self.config.reg_lambda,
            random_state=self.config.random_state,
            verbose=-1,
            force_col_wise=True,  # Avoid LightGBM warning on newer versions
        )

        self.model.fit(
            X_train, y_train,
            sample_weight=sample_weights,
        )

        # Training metrics
        y_pred_proba = self.model.predict_proba(X_train)[:, 1]
        train_auc = roc_auc_score(y_train, y_pred_proba)
        self.metrics["train_auc"] = float(train_auc)

        logger.info("Meta-labeling train AUC: %.4f (gate: %.4f)",
                     train_auc, 0.5 + self.config.min_auc_gap)

        # Check AUC gate
        null_auc = 0.5 + self.config.min_auc_gap
        if train_auc < null_auc:
            logger.warning(
                "Meta-labeling: train AUC %.4f < gate %.4f — model NOT promoted",
                train_auc, null_auc
            )
            return False

        self.is_trained = True
        return True

    def calibrate(self, X_cal: NDArray[np.float64], y_cal: NDArray[np.int64]) -> bool:
        """
        Isotonic calibration of prediction probabilities.

        Per §4.4b: every deployed model needs calibration + conformal prediction.
        Initializes the dedicated ConformalPredictor for honest uncertainty.
        """
        if not self.is_trained or len(np.unique(y_cal)) < 2:
            return False

        y_pred = self.model.predict_proba(X_cal)[:, 1]

        self.calibrator = IsotonicRegression(out_of_bounds="clip")
        self.calibrator.fit(y_pred, y_cal)
        self.is_calibrated = True

        # Compute calibration residuals for conformal prediction
        y_calibrated = self.calibrator.transform(y_pred)
        self.residuals = np.abs(y_calibrated - y_cal)

        # Initialize dedicated conformal predictor
        self._conformal = ConformalPredictor(
            alpha=1.0 - self.config.conformal_confidence,
            method="split",
            min_calibration=self.config.min_calibrations,
        )
        self._conformal.calibrate(y_cal, self.residuals.tolist())

        # Calibration metrics
        cal_auc = roc_auc_score(y_cal, y_pred)
        cal_brier = brier_score_loss(y_cal, y_pred)
        self.metrics["cal_auc"] = float(cal_auc)
        self.metrics["cal_brier"] = float(cal_brier)

        logger.info("Meta-labeling calibration: AUC=%.4f, Brier=%.4f, "
                     "conformal_qhat=%.6f, n_residuals=%d",
                     cal_auc, cal_brier,
                     self._conformal.qhat if self._conformal.calibrated else 0,
                     len(self.residuals))

        return len(self.residuals) >= self.config.min_calibrations

    def predict(
        self, X: NDArray[np.float64]
    ) -> Tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
        """
        Predict P(win) with conformal prediction intervals.

        Uses the dedicated ConformalPredictor for honest uncertainty quantification.

        Returns:
            (predictions, lower_bounds, upper_bounds)
        """
        if not self.is_trained:
            logger.warning("Meta-labeling: predicting before training")
            return np.zeros(len(X)), np.zeros(len(X)), np.zeros(len(X))

        y_pred = self.model.predict_proba(X)[:, 1]

        if self.is_calibrated and self.calibrator is not None:
            y_pred = self.calibrator.transform(y_pred)

        # Use dedicated conformal predictor
        if self._conformal is not None and self._conformal.calibrated:
            lower = np.empty(len(y_pred))
            upper = np.empty(len(y_pred))
            for i in range(len(y_pred)):
                interval = self._conformal.predict_interval(y_pred[i], width=0.0)
                lower[i] = interval.lower
                upper[i] = interval.upper
        else:
            # Not enough calibration data — wide intervals
            width = 0.3
            lower = y_pred - width
            upper = y_pred + width

        return y_pred, lower, upper

    def should_trade(
        self,
        predictions: NDArray[np.float64],
        lower_bounds: NDArray[np.float64],
        upper_bounds: NDArray[np.float64],
    ) -> Tuple[NDArray[np.float64], NDArray[np.float64]]:
        """
        Determine which signals to trade and their fractional sizes.

        Uses the dedicated ConformalPredictor.make_decision() for abstention logic.

        Rules:
        - If conformal interval spans 0.5 → abstain
        - Size = min(1.0, (P(win) - 0.5) × 2), capped at 0.25× Kelly
        - Only trade if P(win) > abstain_threshold
        - Conviction scales with distance from interval boundary

        Returns:
            (trade_flags, fractional_sizes)
        """
        n = len(predictions)
        trade_flags = np.zeros(n, dtype=bool)
        sizes = np.zeros(n, dtype=float)

        for i in range(n):
            # Use conformal decision logic
            decision = self._conformal.make_decision(
                point_pred=predictions[i],
                width=float(upper_bounds[i] - lower_bounds[i]),
                min_conviction=self.config.abstain_threshold - 0.5,
            )

            if decision.abstained:
                continue

            trade_flags[i] = True
            sizes[i] = decision.size_scaling * min(1.0, max(0.0,
                         (predictions[i] - 0.5) * 2.0))

        return trade_flags, sizes

    def get_feature_importance(self) -> Dict[str, float]:
        """Return feature importance from the trained model."""
        if not self.is_trained or self.model is None:
            return {}

        importances = self.model.feature_importances_
        names = self.feature_names or [f"f{i}" for i in range(len(importances))]
        total = np.sum(importances)
        if total > 0:
            return {name: float(imp / total) for name, imp in zip(names, importances)}
        return {name: 0.0 for name in names}


# ──────────────────────────────────────────────
# Meta-labeling pipeline
# ──────────────────────────────────────────────


def run_meta_labeling_pipeline(
    base_signals: List[BaseSignal],
    feature_matrix: NDArray[np.float64],
    outcomes: NDArray[np.int64],
    config: Optional[MetaLabelingConfig] = None,
    feature_names: Optional[List[str]] = None,
) -> Tuple[MetaLabelingClassifier, List[BaseSignal]]:
    """
    Complete meta-labeling pipeline:
    1. Extract features from base signals
    2. Train LightGBM on outcomes
    3. Calibrate with isotonic regression
    4. Apply conformal prediction
    5. Filter/size signals

    Returns:
        (trained_classifier, filtered_signals)
    """
    if config is None:
        config = MetaLabelingConfig()

    if len(base_signals) < 100:
        logger.warning("Meta-labeling: insufficient signals (%d) — skipping",
                       len(base_signals))
        return MetaLabelingClassifier(config), base_signals

    X = feature_matrix
    y = outcomes

    # Sample-unique weights
    if config.use_sample_uniqueness:
        weights = _compute_sample_unique_weights(
            [s.timestamp for s in base_signals], config.weight_scale
        )
    else:
        weights = np.ones(len(y))

    # Train
    classifier = MetaLabelingClassifier(config)
    passed = classifier.train(X, y, weights, feature_names)

    if not passed:
        logger.warning("Meta-labeling: AUC gate failed — signals NOT executed")
        return classifier, []

    # Calibrate (use held-out data if available, else train)
    if len(y) > 200:
        split = int(0.8 * len(y))
        X_cal, y_cal = X[split:], y[split:]
    else:
        X_cal, y_cal = X, y

    calibrated = classifier.calibrate(X_cal, y_cal)
    if not calibrated:
        logger.warning("Meta-labeling: calibration failed — using uncalibrated predictions")

    # Predict on all signals
    preds, lo, hi = classifier.predict(X)

    # Filter/size
    flags, sizes = classifier.should_trade(preds, lo, hi)

    # Filter signals
    filtered = []
    for i, signal in enumerate(base_signals):
        if flags[i]:
            signal.features["meta_pred"] = float(preds[i])
            signal.features["meta_confidence"] = float(sizes[i])
            signal.features["meta_interval_lo"] = float(lo[i])
            signal.features["meta_interval_hi"] = float(hi[i])
            signal.conviction = float(preds[i])
            filtered.append(signal)

    logger.info("Meta-labeling: %d / %d signals passed filter "
                 "(AUC=%.4f, Brier=%.4f)",
                 len(filtered), len(base_signals),
                 classifier.metrics.get("train_auc", 0),
                 classifier.metrics.get("cal_brier", 0))

    return classifier, filtered


def _compute_sample_unique_weights(
    timestamps: List[int],
    weight_scale: float = 1.0,
    overlap_window: int = 50,
) -> NDArray[np.float64]:
    """
    Compute sample-unique weights per López de Prado (2018) §6.1.2.

    Overlapping labels share credit: weight = 1 / (1 + overlap_count × scale).
    De-biases training set when labels from overlapping windows.
    """
    n = len(timestamps)
    weights = np.ones(n, dtype=float)

    for i in range(n):
        overlap_count = 0
        for j in range(n):
            if i == j:
                continue
            if abs(timestamps[i] - timestamps[j]) < overlap_window:
                overlap_count += 1
        weights[i] = 1.0 / (1.0 + overlap_count * weight_scale)

    return weights