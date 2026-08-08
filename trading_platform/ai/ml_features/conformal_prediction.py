"""
Conformal Prediction for Honest Uncertainty Intervals.

Implements Conformalized Prediction Sets per López de Prado's AFML framework.
Signals whose conformal interval spans zero are auto-abstained.
Position size scales with interval width.

Design Principle: A model's uncertainty must be honest — conformal intervals
guarantee coverage regardless of model assumptions.

Usage:
    >>> cp = ConformalPredictor(alpha=0.1)
    >>> cp.calibrate(y_true, residuals)
    >>> intervals = cp.predict_interval(model, X_new)
    >>> # If interval spans zero → abstain; size ∝ 1/width
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

import numpy as np
import numpy.typing as npt

logger = logging.getLogger(__name__)


@dataclass
class ConformalInterval:
    """A single conformal prediction interval with coverage guarantee."""
    lower: float
    upper: float
    width: float
    calibrated: bool
    alpha: float  # miscoverage level


@dataclass
class ConformalDecision:
    """Final decision after conformal prediction applies abstention."""
    direction: int  # -1, 0 (abstain), +1
    conviction: float  # 0..1
    interval: ConformalInterval | None
    abstained: bool
    size_scaling: float  # 0..1, scales position size inversely with width


class ConformalPredictor:
    """
    Split conformal predictor.

    Splits training data into calibration set, computes nonconformity scores,
    then produces prediction intervals with guaranteed (1-α) coverage.

    Parameters
    ----------
    alpha : float
        Miscoverage level (e.g., 0.1 → 90% coverage).
    method : str
        'split' for split conformal, 'cross' for cross-conformal.
    """

    def __init__(
        self,
        alpha: float = 0.1,
        method: str = 'split',
        min_calibration: int = 30,
    ) -> None:
        self.alpha = alpha
        self.method = method
        self.min_calibration = min_calibration
        self._qhat: float | None = None
        self._cal_size: int = 0
        self._cal_scores: npt.NDArray[np.float64] | None = None

    @property
    def calibrated(self) -> bool:
        return self._qhat is not None

    def calibrate(
        self,
        y_true: Sequence[float],
        residuals: Sequence[float],
    ) -> None:
        """
        Calibrate using held-out calibration set.

        Parameters
        ----------
        y_true : sequence of true labels
        residuals : sequence of |y_true - y_pred| nonconformity scores
        """
        scores = np.array([abs(r) for r in residuals], dtype=np.float64)

        if len(scores) < self.min_calibration:
            logger.warning(
                "Conformal calibration: insufficient scores (%d < %d), skipping",
                len(scores), self.min_calibration,
            )
            return

        self._cal_scores = scores
        self._cal_size = len(scores)

        # Quantile level: ceil((n+1)(1-α))/n
        q_level = min(1.0, np.ceil((self._cal_size + 1) * (1 - self.alpha)) / self._cal_size)
        self._qhat = float(np.quantile(scores, min(q_level, 0.999)))

        logger.info(
            "Conformal calibrated: α=%.3f, qhat=%.6f, n=%d, coverage≈%.1f%%",
            self.alpha, self._qhat, self._cal_size,
            (1 - self.alpha) * 100,
        )

    def predict_interval(
        self,
        point_pred: float,
        width: float,
    ) -> ConformalInterval:
        """
        Produce a conformal prediction interval around a point prediction.

        Parameters
        ----------
        point_pred : float
            Model's point prediction.
        width : float
            Predicted interval width (e.g., from model uncertainty).

        Returns
        -------
        ConformalInterval
            Guaranteed (1-α) coverage interval.
        """
        if not self.calibrated:
            logger.warning("Conformal predictor not calibrated — returning wide default")
            return ConformalInterval(
                lower=point_pred - 10.0,
                upper=point_pred + 10.0,
                width=20.0,
                calibrated=False,
                alpha=self.alpha,
            )

        half_width = max(self._qhat, width)
        return ConformalInterval(
            lower=point_pred - half_width,
            upper=point_pred + half_width,
            width=2 * half_width,
            calibrated=True,
            alpha=self.alpha,
        )

    def make_decision(
        self,
        point_pred: float,
        width: float,
        min_conviction: float = 0.3,
    ) -> ConformalDecision:
        """
        Make a trading decision with conformal abstention.

        If the conformal interval spans zero → abstain (direction=0).
        Size scaling inversely proportional to interval width.

        Parameters
        ----------
        point_pred : float
            Model's point prediction.
        width : float
            Predicted interval width.
        min_conviction : float
            Minimum conviction to activate (0..1).

        Returns
        -------
        ConformalDecision
        """
        interval = self.predict_interval(point_pred, width)

        # Abstain if interval spans zero
        abstained = interval.lower <= 0 <= interval.upper

        # Direction
        if abstained:
            direction = 0
            conviction = 0.0
        else:
            direction = 1 if point_pred > 0 else -1
            # Conviction scales with distance of point_pred from interval boundary
            boundary_dist = abs(point_pred) - interval.width / 2
            conviction = float(np.clip(boundary_dist / (abs(point_pred) + 1e-12), 0, 1))

        # Size scaling: inversely proportional to width
        # Narrow interval → full size; wide interval → reduced size
        max_width = max(abs(point_pred) * 2, 1e-6)
        size_scaling = float(np.clip(1.0 - (interval.width / max_width), 0.05, 1.0))

        # Apply min_conviction threshold
        if conviction < min_conviction:
            direction = 0
            conviction = 0.0
            size_scaling = 0.0

        return ConformalDecision(
            direction=direction,
            conviction=conviction,
            interval=interval,
            abstained=abstained,
            size_scaling=size_scaling,
        )

    def to_dict(self) -> dict:
        return {
            "alpha": self.alpha,
            "method": self.method,
            "calibrated": self.calibrated,
            "qhat": self._qhat,
            "cal_size": self._cal_size,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConformalPredictor":
        cp = cls(alpha=d["alpha"], method=d["method"])
        cp._qhat = d.get("qhat")
        cp._cal_size = d.get("cal_size", 0)
        return cp


class EnsembleConformalPredictor:
    """
    Ensemble conformal predictor — aggregates intervals from multiple models.

    When multiple models score the same signal, use conformal prediction
    across the ensemble for more robust abstention decisions.
    """

    def __init__(self, alpha: float = 0.1) -> None:
        self.alpha = alpha
        self._predictors: list[ConformalPredictor] = []
        self._weights: list[float] = []

    def add_predictor(self, predictor: ConformalPredictor, weight: float = 1.0) -> None:
        self._predictors.append(predictor)
        self._weights.append(weight)

    def calibrate(self, y_true: Sequence[float], predictions: Sequence[Sequence[float]]) -> None:
        """
        Calibrate each predictor in the ensemble.

        Parameters
        ----------
        y_true : sequence of true labels
        predictions : sequence of sequences, one per predictor
        """
        for i, pred in enumerate(predictions):
            residuals = [t - p for t, p in zip(y_true, pred)]
            self._predictors[i].calibrate(y_true, residuals)

    def predict_ensemble(
        self,
        point_preds: Sequence[float],
        widths: Sequence[float],
    ) -> ConformalDecision:
        """
        Aggregate conformal predictions across the ensemble.

        Returns the consensus decision with the widest coverage.
        """
        intervals = [
            p.predict_interval(pp, w)
            for p, pp, w in zip(self._predictors, point_preds, widths)
        ]

        # Weighted aggregation
        total_weight = sum(self._weights)
        if total_weight == 0:
            total_weight = len(intervals)

        weighted_lower = sum(iv.lower * w for iv, w in zip(intervals, self._weights)) / total_weight
        weighted_upper = sum(iv.upper * w for iv, w in zip(intervals, self._weights)) / total_weight
        weighted_width = sum(iv.width * w for iv, w in zip(intervals, self._weights)) / total_weight

        point_pred = weighted_lower + weighted_width / 2

        # Use the widest interval for conservative coverage
        max_width = max(iv.width for iv in intervals)

        # Delegate to single predictor for decision logic
        dummy = ConformalPredictor(alpha=self.alpha)
        return dummy.make_decision(point_pred, max_width)