from __future__ import annotations

"""Forecasting models — deterministic baselines first; deep models via lazy imports."""

import math
from typing import Any

from trading_platform.neural.schemas import ForecastPrediction


class MovingAverageForecaster:
    """Simple moving-average / linear baseline forecast. Always available."""

    def predict(self, symbol: str, bars: list[dict]) -> ForecastPrediction:
        if len(bars) < 2:
            return ForecastPrediction(
                symbol=symbol, direction_probability=0.5,
                expected_return=0.0, model_id="baseline_ma",
                confidence=0.3, model_uncertainty=0.7,
            )

        closes = [b.get("close", 0.0) for b in bars]
        returns = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            if closes[i - 1] > 0 else 0.0
            for i in range(1, len(closes))
        ]

        # Medium-term baseline (20 bars)
        medium = returns[-20:] if len(returns) >= 20 else returns
        mean_ret = sum(medium) / len(medium) if medium else 0.0
        std_ret = math.sqrt(sum((r - mean_ret) ** 2 for r in medium) / max(1, len(medium)))

        # Short-term momentum (5 bars) — more responsive to recent price action
        short = returns[-5:] if len(returns) >= 5 else returns
        short_mean = sum(short) / len(short) if short else mean_ret

        # Trend consistency: fraction of short bars agreeing with short_mean direction
        if short and short_mean != 0:
            agree = sum(1 for r in short if (r > 0) == (short_mean > 0))
            consistency_bonus = (agree / len(short) - 0.5) * 0.15
        else:
            consistency_bonus = 0.0

        # Momentum alignment: short-term and medium-term point same direction
        alignment_bonus = 0.05 if (short_mean * mean_ret > 0) else -0.03

        # Sharpe-like signal using short-term mean vs medium-term volatility
        # Scale by 0.3 (vs old 0.1) for a more responsive but bounded signal
        sharpe_signal = short_mean / max(std_ret, 1e-6)
        direction_prob = min(0.9, max(0.1,
            0.5 + sharpe_signal * 0.3 + consistency_bonus + alignment_bonus
        ))

        # Expected return: project 5-bar forward return from short-term momentum
        # This gives ProfitGuard a more realistic expected gain than raw bar mean_ret
        expected_return = short_mean * 5

        q10 = mean_ret - 1.28 * std_ret
        q90 = mean_ret + 1.28 * std_ret
        uncertainty = min(1.0, std_ret * 10)

        return ForecastPrediction(
            symbol=symbol,
            direction_probability=direction_prob,
            expected_return=expected_return,
            return_quantile_10=q10,
            return_quantile_90=q90,
            model_id="baseline_ma",
            confidence=max(0.1, 1.0 - uncertainty),
            model_uncertainty=uncertainty,
        )


class TemporalFusionTransformerForecaster:
    """Optional TFT forecaster — lazy import; graceful fallback if unavailable."""

    def __init__(self) -> None:
        self._available = False
        self._model: Any = None

    def is_available(self) -> bool:
        if self._available:
            return True
        try:
            import pytorch_forecasting  # noqa: F401
            import torch  # noqa: F401
            self._available = True
        except ImportError:
            pass
        return self._available

    def predict(self, symbol: str, bars: list[dict]) -> ForecastPrediction | None:
        if not self.is_available():
            return None
        # Placeholder: returns None if model weights are not loaded
        return None
