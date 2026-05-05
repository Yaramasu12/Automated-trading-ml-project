from __future__ import annotations

from datetime import datetime

from trading_platform.domain.enums import Side
from trading_platform.domain.models import Instrument, MarketBar, Signal
from trading_platform.strategies.base import Strategy


class EquityMomentumStrategy(Strategy):
    name = "equity_momentum"
    family = "equity"

    # Thresholds — lower than before but require momentum alignment
    _MOM5_BUY = 0.006
    _MOM20_BUY = 0.010
    _MOM5_SELL = -0.007
    _MOM20_SELL = -0.012
    _RSI_OVERBOUGHT = 72.0   # don't buy into overbought
    _RSI_OVERSOLD = 28.0     # don't sell into oversold

    def generate_signal(self, instrument: Instrument, bars: list[MarketBar], now: datetime) -> Signal | None:
        if len(bars) < 21:
            return None
        f = self.feature_engine.compute(bars)

        # Require both timeframes aligned — kills 40% of noise trades
        if f.momentum_alignment < 0:
            return None

        # Volatility-normalized confidence: strong in calm, weaker in high-vol
        vol_penalty = min(1.0, 0.015 / max(f.realized_volatility, 0.001))
        base_conf = 0.55 + (f.trend_strength / 20) * vol_penalty

        if f.momentum_5 > self._MOM5_BUY and f.momentum_20 > self._MOM20_BUY:
            # RSI filter: skip if already overbought
            if f.rsi_14 > self._RSI_OVERBOUGHT:
                return None
            # Volume confirmation: prefer volume surge
            vol_boost = 0.05 if f.volume_ratio > 1.3 else 0.0
            return Signal(
                strategy_name=self.name,
                symbol=instrument.symbol,
                side=Side.BUY,
                confidence=min(0.93, base_conf + vol_boost),
                price=f.close,
                reason=f"aligned momentum BUY rsi={f.rsi_14:.0f} vol_ratio={f.volume_ratio:.2f}",
                created_at=now,
                metadata={
                    "momentum_5": f.momentum_5, "momentum_20": f.momentum_20,
                    "rsi_14": f.rsi_14, "atr_14": f.atr_14, "volume_ratio": f.volume_ratio,
                },
            )
        if f.momentum_5 < self._MOM5_SELL and f.momentum_20 < self._MOM20_SELL:
            # RSI filter: skip if already oversold
            if f.rsi_14 < self._RSI_OVERSOLD:
                return None
            vol_boost = 0.05 if f.volume_ratio > 1.3 else 0.0
            return Signal(
                strategy_name=self.name,
                symbol=instrument.symbol,
                side=Side.SELL,
                confidence=min(0.90, base_conf + vol_boost),
                price=f.close,
                reason=f"aligned momentum SELL rsi={f.rsi_14:.0f} vol_ratio={f.volume_ratio:.2f}",
                created_at=now,
                metadata={
                    "momentum_5": f.momentum_5, "momentum_20": f.momentum_20,
                    "rsi_14": f.rsi_14, "atr_14": f.atr_14, "volume_ratio": f.volume_ratio,
                },
            )
        return None


class SwingTrendStrategy(EquityMomentumStrategy):
    name = "swing_trend"

    def exit_rules(self):
        return super().exit_rules().__class__(stop_loss_pct=0.03, target_pct=0.06, max_holding_days=10)


class GapStrategy(EquityMomentumStrategy):
    name = "gap_strategy"

    def generate_signal(self, instrument: Instrument, bars: list[MarketBar], now: datetime) -> Signal | None:
        if len(bars) < 21:
            return None
        previous_close = bars[-2].close
        gap = (bars[-1].open - previous_close) / previous_close if previous_close else 0.0
        if abs(gap) < 0.01:
            return None

        f = self.feature_engine.compute(bars)

        # Exhaustion filter: large gap + extreme RSI = likely reversal, skip continuation
        gap_up = gap > 0
        if gap_up and f.rsi_14 > 75:
            return None   # gap-up into overbought = exhaustion, not continuation
        if not gap_up and f.rsi_14 < 25:
            return None   # gap-down into oversold = exhaustion

        # Volume confirms conviction (genuine gap vs. illiquid overnight move)
        if f.volume_ratio < 0.7:
            return None

        side = Side.BUY if gap_up else Side.SELL
        confidence = min(0.85, 0.55 + abs(gap) * 8 + (0.05 if f.volume_ratio > 1.5 else 0.0))
        return Signal(
            strategy_name=self.name,
            symbol=instrument.symbol,
            side=side,
            confidence=confidence,
            price=bars[-1].open,
            reason=f"gap {'up' if gap_up else 'down'} continuation gap={gap:.3f} rsi={f.rsi_14:.0f}",
            created_at=now,
            metadata={"gap": gap, "rsi_14": f.rsi_14, "volume_ratio": f.volume_ratio},
        )
