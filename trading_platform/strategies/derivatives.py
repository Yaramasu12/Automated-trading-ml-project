from __future__ import annotations

from datetime import datetime

from trading_platform.domain.enums import OptionType, Side
from trading_platform.domain.models import Instrument, MarketBar, Signal
from trading_platform.strategies.base import Strategy


class FuturesTrendStrategy(Strategy):
    name = "futures_trend"

    def generate_signal(self, instrument: Instrument, bars: list[MarketBar], now: datetime) -> Signal | None:
        if len(bars) < 21:
            return None
        features = self.feature_engine.compute(bars)
        if features.trend_strength < 2.5:
            return None
        side = Side.BUY if features.momentum_20 > 0 else Side.SELL
        return Signal(
            strategy_name=self.name,
            symbol=instrument.symbol,
            side=side,
            confidence=min(0.92, 0.50 + features.trend_strength / 15),
            price=features.close,
            reason=f"{instrument.underlying or instrument.symbol} future trend confirmation",
            created_at=now,
            metadata={"trend_strength": features.trend_strength, "regime": "TRENDING"},
        )


class DefinedRiskOptionSpreadStrategy(Strategy):
    name = "defined_risk_option_spread"

    def generate_signal(self, instrument: Instrument, bars: list[MarketBar], now: datetime) -> Signal | None:
        if instrument.option_type not in {OptionType.CE, OptionType.PE} or len(bars) < 21:
            return None
        features = self.feature_engine.compute(bars)
        if features.realized_volatility < 0.006:
            return None
        bullish = features.momentum_20 > 0
        preferred_option = OptionType.CE if bullish else OptionType.PE
        if instrument.option_type != preferred_option:
            return None
        return Signal(
            strategy_name=self.name,
            symbol=instrument.symbol,
            side=Side.BUY,
            confidence=min(0.88, 0.55 + abs(features.momentum_20) * 4),
            price=max(1.0, features.close * 0.015),
            reason="defined-risk directional options exposure",
            created_at=now,
            metadata={
                "underlying_close": features.close,
                "option_type": instrument.option_type.value,
                "expiry": instrument.expiry.isoformat() if instrument.expiry else None,
            },
        )


class VolatilityBreakoutOptionsStrategy(Strategy):
    name = "volatility_breakout_options"

    def generate_signal(self, instrument: Instrument, bars: list[MarketBar], now: datetime) -> Signal | None:
        if instrument.option_type not in {OptionType.CE, OptionType.PE} or len(bars) < 21:
            return None
        features = self.feature_engine.compute(bars)
        if features.volume_ratio < 1.1 or abs(features.momentum_5) < 0.012:
            return None
        side_option = OptionType.CE if features.momentum_5 > 0 else OptionType.PE
        if instrument.option_type != side_option:
            return None
        return Signal(
            strategy_name=self.name,
            symbol=instrument.symbol,
            side=Side.BUY,
            confidence=min(0.9, 0.58 + features.volume_ratio / 10),
            price=max(1.0, features.close * 0.018),
            reason="volume-backed volatility breakout",
            created_at=now,
            metadata={"volume_ratio": features.volume_ratio, "momentum_5": features.momentum_5},
        )

