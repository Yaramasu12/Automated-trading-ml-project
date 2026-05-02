from __future__ import annotations

from datetime import datetime

from trading_platform.domain.enums import Side
from trading_platform.domain.models import Instrument, MarketBar, Signal
from trading_platform.strategies.base import Strategy


class EquityMomentumStrategy(Strategy):
    name = "equity_momentum"
    family = "equity"

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


class SwingTrendStrategy(EquityMomentumStrategy):
    name = "swing_trend"

    def exit_rules(self):
        return super().exit_rules().__class__(stop_loss_pct=0.03, target_pct=0.06, max_holding_days=10)


class GapStrategy(EquityMomentumStrategy):
    name = "gap_strategy"

    def generate_signal(self, instrument: Instrument, bars: list[MarketBar], now: datetime) -> Signal | None:
        if len(bars) < 2:
            return None
        previous_close = bars[-2].close
        gap = (bars[-1].open - previous_close) / previous_close if previous_close else 0.0
        if abs(gap) < 0.01:
            return None
        side = Side.BUY if gap > 0 else Side.SELL
        return Signal(
            strategy_name=self.name,
            symbol=instrument.symbol,
            side=side,
            confidence=min(0.82, 0.55 + abs(gap) * 10),
            price=bars[-1].open,
            reason="opening gap continuation",
            created_at=now,
            metadata={"gap": gap},
        )
