"""Realized-volatility forecaster for the short-vol condor.

This is the honest, validated home for the platform's "neural/forecasting" layer.
Forecasting *returns* has no edge (proven: AUC ~0.50), but *volatility* is
genuinely predictable — it is autocorrelated and mean-reverting. The short-vol
condor's entire edge is the volatility risk premium (implied minus realized), so
a better forecast of *future* realized vol directly sharpens every entry.

It wraps the existing MLE-fitted GARCH(1,1) model. Deployment is gated: the
forecast is only trusted if `validate_beats_naive` shows it predicts future
realized vol better (lower RMSE) than the trailing-realized baseline. If it does
not beat the baseline, callers fall back to trailing realized — the same
"earn deployment" discipline the rest of the platform uses.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from trading_platform.neural.volatility import GARCHVolatilityModel


def _log_returns(closes: list[float] | np.ndarray) -> list[float]:
    c = np.asarray(closes, dtype=float)
    c = c[c > 0]
    if len(c) < 2:
        return []
    return list(np.diff(np.log(c)))


@dataclass(frozen=True)
class VolValidation:
    naive_rmse: float
    garch_rmse: float
    n: int

    @property
    def beats_naive(self) -> bool:
        return self.n >= 30 and self.garch_rmse < self.naive_rmse


class VolatilityForecaster:
    """GARCH(1,1) conditional-volatility forecast, annualised in vol-points (%)."""

    def __init__(self) -> None:
        self._garch = GARCHVolatilityModel()

    def forecast_pct(self, closes: list[float] | np.ndarray, symbol: str = "NIFTY") -> float:
        """Forecast annualised realized vol (%) over the near horizon. Returns 0.0
        if there isn't enough data (caller then falls back to trailing realized)."""
        rets = _log_returns(closes)
        if len(rets) < 20:
            return 0.0
        pred = self._garch.predict(symbol, rets)
        # GARCHVolatilityModel returns annualised vol as a fraction (e.g. 0.13).
        v = float(pred.predicted_volatility) * 100.0
        # Guard against degenerate fits.
        return v if 1.0 < v < 200.0 else 0.0

    @staticmethod
    def _trailing_realized_pct(rets_window: list[float]) -> float:
        if len(rets_window) < 2:
            return 0.0
        return float(np.std(rets_window) * math.sqrt(252) * 100.0)

    def validate_beats_naive(
        self,
        closes: list[float] | np.ndarray,
        horizon: int = 5,
        window: int = 20,
    ) -> VolValidation:
        """Walk-forward: at each point predict the next `horizon`-day realized vol
        with (a) GARCH and (b) the trailing-`window` realized (naive), and compare
        RMSE against the actual realized vol that followed. GARCH earns use only if
        its RMSE is lower on a meaningful sample."""
        rets = _log_returns(closes)
        if len(rets) < window + horizon + 30:
            return VolValidation(naive_rmse=float("inf"), garch_rmse=float("inf"), n=0)

        naive_err, garch_err, n = 0.0, 0.0, 0
        ann = math.sqrt(252) * 100.0
        for t in range(window, len(rets) - horizon):
            past = rets[:t]
            future = rets[t:t + horizon]
            actual = float(np.std(future) * ann)
            naive = self._trailing_realized_pct(rets[t - window:t])
            try:
                garch = float(self._garch.predict("VAL", past).predicted_volatility) * 100.0
            except Exception:
                continue
            if not (1.0 < garch < 200.0):
                continue
            naive_err += (naive - actual) ** 2
            garch_err += (garch - actual) ** 2
            n += 1
        if n == 0:
            return VolValidation(naive_rmse=float("inf"), garch_rmse=float("inf"), n=0)
        return VolValidation(
            naive_rmse=math.sqrt(naive_err / n),
            garch_rmse=math.sqrt(garch_err / n),
            n=n,
        )
