from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, pstdev

from trading_platform.domain.models import MarketBar


@dataclass(frozen=True)
class FeatureSnapshot:
    symbol: str
    close: float
    momentum_5: float
    momentum_20: float
    realized_volatility: float
    volume_ratio: float
    trend_strength: float


class FeatureEngine:
    def compute(self, bars: list[MarketBar]) -> FeatureSnapshot:
        if len(bars) < 21:
            raise ValueError("At least 21 bars are required for feature computation")
        closes = [bar.close for bar in bars]
        volumes = [bar.volume for bar in bars]
        returns = [
            (closes[index] - closes[index - 1]) / closes[index - 1]
            for index in range(1, len(closes))
            if closes[index - 1] > 0
        ]
        close = closes[-1]
        momentum_5 = (close - closes[-6]) / closes[-6]
        momentum_20 = (close - closes[-21]) / closes[-21]
        recent_vol = pstdev(returns[-20:]) if len(returns) >= 20 else 0.0
        volume_ratio = volumes[-1] / max(1.0, mean(volumes[-20:]))
        trend_strength = abs(momentum_20) / max(recent_vol, 0.0001)
        return FeatureSnapshot(
            symbol=bars[-1].symbol,
            close=close,
            momentum_5=momentum_5,
            momentum_20=momentum_20,
            realized_volatility=recent_vol,
            volume_ratio=volume_ratio,
            trend_strength=trend_strength,
        )

