from __future__ import annotations

"""Calibration helpers — conformal prediction placeholder.

Converts raw model confidence into calibrated probabilities.
Falls back to identity calibration if no calibrator is fitted.
"""


class IdentityCalibrator:
    """Pass-through: assumes model outputs are already well-calibrated."""

    def calibrate(self, raw_prob: float) -> float:
        return max(0.0, min(1.0, float(raw_prob)))


class IsotonicCalibrator:
    """Simple isotonic regression proxy via linear interpolation.

    In production: fit on out-of-sample data with sklearn.isotonic.
    """

    def __init__(self) -> None:
        self._fitted = False
        self._x: list[float] = []
        self._y: list[float] = []

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    def fit(self, raw_probs: list[float], labels: list[float]) -> None:
        if len(raw_probs) != len(labels) or len(raw_probs) < 2:
            return
        pairs = sorted(zip(raw_probs, labels))
        self._x = [p[0] for p in pairs]
        self._y = [p[1] for p in pairs]
        self._fitted = True

    def calibrate(self, raw_prob: float) -> float:
        if not self._fitted or not self._x:
            return max(0.0, min(1.0, raw_prob))
        # Linear interpolation
        if raw_prob <= self._x[0]:
            return self._y[0]
        if raw_prob >= self._x[-1]:
            return self._y[-1]
        for i in range(len(self._x) - 1):
            if self._x[i] <= raw_prob <= self._x[i + 1]:
                dx = self._x[i + 1] - self._x[i]
                dy = self._y[i + 1] - self._y[i]
                t = (raw_prob - self._x[i]) / dx if dx > 0 else 0.0
                return max(0.0, min(1.0, self._y[i] + t * dy))
        return max(0.0, min(1.0, raw_prob))


NO_TRADE_UNCERTAINTY_THRESHOLD = 0.70  # Do not trade when model uncertainty > this
