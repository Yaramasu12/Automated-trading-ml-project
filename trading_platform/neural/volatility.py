from __future__ import annotations

"""Volatility and tail-risk models — GARCH baseline + optional deep models."""

import math
from typing import Any

from trading_platform.neural.schemas import TailRiskPrediction, VolatilityPrediction

# Grid used for MLE parameter search; kept small for O(1) prediction latency.
_ALPHA_GRID = (0.05, 0.08, 0.10, 0.12, 0.15, 0.20)
_BETA_GRID = (0.70, 0.75, 0.80, 0.85, 0.88, 0.90)


def _fit_garch(returns: list[float]) -> tuple[float, float, float]:
    """Grid-search MLE for GARCH(1,1) omega/alpha/beta on the provided returns.

    Returns (omega, alpha, beta). Falls back to EWMA-derived defaults when
    the returns series is too short for a reliable fit (< 20 observations).
    """
    n = len(returns)
    sigma2_bar = sum(r * r for r in returns) / n

    if n < 20:
        # EWMA fallback: alpha=0.06, beta=0.94 (RiskMetrics-style)
        alpha, beta = 0.06, 0.94
        return sigma2_bar * (1.0 - alpha - beta), alpha, beta

    best_ll = float("-inf")
    best_alpha, best_beta = 0.10, 0.85
    for alpha in _ALPHA_GRID:
        for beta in _BETA_GRID:
            if alpha + beta >= 0.9999:
                continue
            omega = sigma2_bar * (1.0 - alpha - beta)
            if omega <= 0:
                continue
            h = sigma2_bar
            ll = 0.0
            for r in returns:
                h = omega + alpha * r * r + beta * h
                h = max(h, 1e-12)
                ll -= 0.5 * (math.log(h) + r * r / h)
            if ll > best_ll:
                best_ll = ll
                best_alpha, best_beta = alpha, beta

    omega = sigma2_bar * (1.0 - best_alpha - best_beta)
    return max(omega, 1e-12), best_alpha, best_beta


class GARCHVolatilityModel:
    """GARCH(1,1) with MLE-fitted parameters. No optional dependencies."""

    def predict(self, symbol: str, returns: list[float]) -> VolatilityPrediction:
        if not returns:
            return VolatilityPrediction(
                symbol=symbol, predicted_volatility=0.2,
                garch_volatility=0.2, tail_risk_score=0.5,
            )

        omega, alpha, beta = _fit_garch(returns)

        # Run GARCH recursion over the full series to get current conditional variance
        var = sum(r ** 2 for r in returns) / len(returns)
        for r in returns:
            var = omega + alpha * r ** 2 + beta * var
            var = max(var, 1e-12)
        daily_vol = math.sqrt(var)
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
    """Optional deep volatility model — lazy import; graceful fallback to GARCH."""

    def __init__(self) -> None:
        self._garch = GARCHVolatilityModel()

    def is_available(self) -> bool:
        try:
            import torch  # noqa: F401
            return True
        except ImportError:
            return False

    def predict(self, symbol: str, returns: list[float]) -> VolatilityPrediction:
        # Deep model weights not yet loaded; GARCH is always the production path.
        return self._garch.predict(symbol, returns)


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
