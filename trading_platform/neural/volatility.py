from __future__ import annotations

"""Volatility and tail-risk models — GARCH baseline + optional deep models."""

import math
from typing import Any

from trading_platform.neural.schemas import TailRiskPrediction, VolatilityPrediction


class GARCHVolatilityModel:
    """Simplified GARCH(1,1) baseline. Deterministic; no optional dependencies."""

    def __init__(self, omega: float = 1e-6, alpha: float = 0.05, beta: float = 0.90) -> None:
        self.omega = omega
        self.alpha = alpha
        self.beta = beta

    def predict(self, symbol: str, returns: list[float]) -> VolatilityPrediction:
        if not returns:
            return VolatilityPrediction(
                symbol=symbol, predicted_volatility=0.2,
                garch_volatility=0.2, tail_risk_score=0.5,
            )

        # Estimate unconditional variance
        var = sum(r ** 2 for r in returns) / len(returns)
        # Run GARCH recursion
        for r in returns[-30:]:
            var = self.omega + self.alpha * r ** 2 + self.beta * var
        daily_vol = math.sqrt(max(var, 1e-10))
        ann_vol = daily_vol * math.sqrt(252)

        # Simple tail-risk proxy: fraction of returns beyond 2σ
        threshold = 2 * daily_vol
        extremes = sum(1 for r in returns[-60:] if abs(r) > threshold)
        tail_risk = min(1.0, extremes / max(1, min(60, len(returns[-60:]))) * 5)

        return VolatilityPrediction(
            symbol=symbol,
            predicted_volatility=ann_vol,
            garch_volatility=ann_vol,
            tail_risk_score=tail_risk,
            model_id="garch_1_1",
            confidence=0.6,
        )


class DeepVolatilityForecaster:
    """Optional deep volatility model — lazy import; graceful fallback."""

    def is_available(self) -> bool:
        try:
            import torch  # noqa: F401
            return True
        except ImportError:
            return False

    def predict(self, symbol: str, returns: list[float]) -> VolatilityPrediction | None:
        if not self.is_available():
            return None
        return None  # Placeholder until model weights loaded


class TailRiskModel:
    """Quantile-regression-style baseline tail-risk predictor."""

    def predict(self, symbol: str, returns: list[float]) -> TailRiskPrediction:
        if not returns:
            return TailRiskPrediction(
                symbol=symbol, extreme_move_probability=0.05,
                expected_max_drawdown=0.10, model_id="quantile_baseline",
            )
        std = math.sqrt(sum(r ** 2 for r in returns) / len(returns))
        threshold = 3 * std
        prob = sum(1 for r in returns if abs(r) > threshold) / len(returns)
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in returns:
            cum += r
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
        return TailRiskPrediction(
            symbol=symbol,
            extreme_move_probability=min(1.0, prob * 10),
            expected_max_drawdown=max_dd,
            model_id="quantile_baseline",
            confidence=0.5,
        )
