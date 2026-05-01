from __future__ import annotations

from datetime import datetime

from trading_platform.domain.enums import Side
from trading_platform.domain.models import Instrument, MarketBar, Signal
from trading_platform.strategies.base import Strategy


class EquityMomentumStrategy(Strategy):
    name = "equity_momentum"

    def generate_signal(self, instrument: Instrument, bars: list[MarketBar], now: datetime) -> Signal | None:
        if len(bars) < 21:
            return None
        features = self.feature_engine.compute(bars)
        if features.momentum_5 > 0.008 and features.momentum_20 > 0.015:
            return Signal(
                strategy_name=self.name,
                symbol=instrument.symbol,
                side=Side.BUY,
                confidence=min(0.95, 0.55 + features.trend_strength / 20),
                price=features.close,
                reason="positive short and medium-term momentum",
                created_at=now,
                metadata={"momentum_5": features.momentum_5, "momentum_20": features.momentum_20},
            )
        if features.momentum_5 < -0.01 and features.momentum_20 < -0.02:
            return Signal(
                strategy_name=self.name,
                symbol=instrument.symbol,
                side=Side.SELL,
                confidence=min(0.9, 0.55 + features.trend_strength / 20),
                price=features.close,
                reason="negative short and medium-term momentum",
                created_at=now,
                metadata={"momentum_5": features.momentum_5, "momentum_20": features.momentum_20},
            )
        return None

