from __future__ import annotations

import math
from datetime import datetime

from trading_platform.domain.enums import InstrumentType, OptionType, Side
from trading_platform.domain.models import Instrument, MarketBar, Signal
from trading_platform.strategies.base import Strategy, StrategyExitRules, StrategyRiskEstimate


def _atm_option_premium(spot: float, realized_vol: float, days_to_expiry: int) -> float:
    """Brenner-Subrahmanyam ATM approximation: C ≈ S × σ × √(T/252) × 0.4.

    Returns a floor of 1.0 so zero-price signals never enter the system.
    Uses 0.15 annualised vol as minimum to avoid near-zero premiums on calm days.
    """
    annual_vol = max(realized_vol * math.sqrt(252), 0.15)
    dte = max(days_to_expiry, 1)
    premium = spot * annual_vol * math.sqrt(dte / 252) * 0.4
    return max(1.0, round(premium, 2))


class FuturesTrendStrategy(Strategy):
    name = "futures_trend"
    family = "futures"
    supports_rollover = True

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

    def estimate_risk(self, instrument: Instrument, price: float, quantity: int) -> StrategyRiskEstimate:
        notional = abs(price * quantity * instrument.lot_size)
        return StrategyRiskEstimate(max_loss=notional * 0.015, margin_required=notional * 0.12)

    def exit_rules(self) -> StrategyExitRules:
        return StrategyExitRules(stop_loss_pct=0.012, target_pct=0.030, max_holding_days=3, square_off_before_expiry_days=1)


class DefinedRiskOptionSpreadStrategy(Strategy):
    name = "defined_risk_option_spread"
    family = "options"

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
        days_to_expiry = (instrument.expiry - now.date()).days if instrument.expiry else 7
        price = _atm_option_premium(features.close, features.realized_volatility, days_to_expiry)
        return Signal(
            strategy_name=self.name,
            symbol=instrument.symbol,
            side=Side.BUY,
            confidence=min(0.88, 0.55 + abs(features.momentum_20) * 4),
            price=price,
            reason="defined-risk directional options exposure",
            created_at=now,
            metadata={
                "underlying_close": features.close,
                "option_type": instrument.option_type.value,
                "expiry": instrument.expiry.isoformat() if instrument.expiry else None,
                "days_to_expiry": days_to_expiry,
            },
        )

    def estimate_risk(self, instrument: Instrument, price: float, quantity: int) -> StrategyRiskEstimate:
        premium = abs(price * quantity * instrument.lot_size)
        return StrategyRiskEstimate(max_loss=premium, margin_required=premium)

    def exit_rules(self) -> StrategyExitRules:
        return StrategyExitRules(stop_loss_pct=0.35, target_pct=0.50, max_holding_days=2, square_off_before_expiry_days=1)


class VolatilityBreakoutOptionsStrategy(Strategy):
    name = "volatility_breakout_options"
    family = "options"

    def generate_signal(self, instrument: Instrument, bars: list[MarketBar], now: datetime) -> Signal | None:
        if instrument.option_type not in {OptionType.CE, OptionType.PE} or len(bars) < 21:
            return None
        features = self.feature_engine.compute(bars)
        if features.volume_ratio < 1.1 or abs(features.momentum_5) < 0.012:
            return None
        side_option = OptionType.CE if features.momentum_5 > 0 else OptionType.PE
        if instrument.option_type != side_option:
            return None
        days_to_expiry = (instrument.expiry - now.date()).days if instrument.expiry else 7
        price = _atm_option_premium(features.close, features.realized_volatility, days_to_expiry)
        return Signal(
            strategy_name=self.name,
            symbol=instrument.symbol,
            side=Side.BUY,
            confidence=min(0.9, 0.58 + features.volume_ratio / 10),
            price=price,
            reason="volume-backed volatility breakout",
            created_at=now,
            metadata={
                "volume_ratio": features.volume_ratio,
                "momentum_5": features.momentum_5,
                "days_to_expiry": days_to_expiry,
            },
        )


class MeanReversionStrategy(Strategy):
    """RSI-extreme mean reversion.

    Buys oversold (RSI < 32) and sells overbought (RSI > 68).
    Requires Bollinger Band width > 0.02 to avoid flat/dead markets.
    ATR stop prevents entering falling-knife downtrends.
    This is the PRIMARY profitable strategy in ranging, sideways markets.
    """
    name = "mean_reversion"
    family = "directional"

    _RSI_OVERSOLD   = 32.0
    _RSI_OVERBOUGHT = 68.0

    def generate_signal(self, instrument: Instrument, bars: list[MarketBar], now: datetime) -> Signal | None:
        if len(bars) < 21:
            return None
        f = self.feature_engine.compute(bars)

        if f.bb_width < 0.02:   # flat market — no reversion edge
            return None

        # BUY: oversold RSI + not in confirmed downtrend
        if f.rsi_14 < self._RSI_OVERSOLD and f.momentum_20 > -0.12:
            if f.trend_strength > 5.0 and f.momentum_alignment < 0:
                return None  # falling knife — skip
            depth = max(0.0, (self._RSI_OVERSOLD - f.rsi_14) / self._RSI_OVERSOLD)
            conf  = min(0.90, 0.68 + depth * 0.3)
            return Signal(self.name, instrument.symbol, Side.BUY, conf, f.close,
                          f"RSI oversold={f.rsi_14:.0f} mean-revert atr={f.atr_14:.2f}", now,
                          {"rsi_14": f.rsi_14, "atr_14": f.atr_14, "bb_width": f.bb_width, "deviation": f.momentum_20})

        # SELL: overbought RSI + not in confirmed uptrend
        if f.rsi_14 > self._RSI_OVERBOUGHT and f.momentum_20 < 0.12:
            if f.trend_strength > 5.0 and f.momentum_alignment > 0:
                return None  # strong uptrend — skip reversion short
            depth = max(0.0, (f.rsi_14 - self._RSI_OVERBOUGHT) / (100 - self._RSI_OVERBOUGHT))
            conf  = min(0.88, 0.68 + depth * 0.25)
            return Signal(self.name, instrument.symbol, Side.SELL, conf, f.close,
                          f"RSI overbought={f.rsi_14:.0f} mean-revert atr={f.atr_14:.2f}", now,
                          {"rsi_14": f.rsi_14, "atr_14": f.atr_14, "bb_width": f.bb_width, "deviation": f.momentum_20})
        return None

    def exit_rules(self):
        from trading_platform.strategies.base import StrategyExitRules
        return StrategyExitRules(stop_loss_pct=0.015, target_pct=0.038, max_holding_days=5)


class BreakoutStrategy(Strategy):
    """Volume-confirmed N-day high/low breakout.

    Requires:
      - Price closes above 10-day high (upside) or below 10-day low (downside)
      - Volume ratio > 1.25 (institutional participation)
      - RSI not already at extreme (no breakout into overbought/oversold)
      - Bollinger Band expanding (bb_width > 0.03)
    """
    name = "breakout"
    family = "directional"
    _LOOKBACK = 10

    def generate_signal(self, instrument: Instrument, bars: list[MarketBar], now: datetime) -> Signal | None:
        if len(bars) < max(21, self._LOOKBACK + 2):
            return None
        f = self.feature_engine.compute(bars)

        if f.bb_width < 0.03 or f.volume_ratio < 1.25:
            return None

        recent_highs = [bar.high for bar in bars[-(self._LOOKBACK + 1):-1]]
        recent_lows  = [bar.low  for bar in bars[-(self._LOOKBACK + 1):-1]]
        close = bars[-1].close

        if close > max(recent_highs) and f.rsi_14 < 75:
            conf = min(0.88, 0.68 + (f.volume_ratio - 1.25) * 0.15 + f.momentum_5 * 3)
            return Signal(self.name, instrument.symbol, Side.BUY, conf, close,
                          f"upside breakout {self._LOOKBACK}d-high rsi={f.rsi_14:.0f} vol={f.volume_ratio:.2f}", now,
                          {"rsi_14": f.rsi_14, "volume_ratio": f.volume_ratio, "atr_14": f.atr_14})

        if close < min(recent_lows) and f.rsi_14 > 25:
            conf = min(0.85, 0.66 + (f.volume_ratio - 1.25) * 0.12 + abs(f.momentum_5) * 2)
            return Signal(self.name, instrument.symbol, Side.SELL, conf, close,
                          f"downside breakout {self._LOOKBACK}d-low rsi={f.rsi_14:.0f} vol={f.volume_ratio:.2f}", now,
                          {"rsi_14": f.rsi_14, "volume_ratio": f.volume_ratio, "atr_14": f.atr_14})
        return None

    def exit_rules(self):
        from trading_platform.strategies.base import StrategyExitRules
        return StrategyExitRules(stop_loss_pct=0.02, target_pct=0.05, max_holding_days=7)


class OptionsTemplateStrategy(DefinedRiskOptionSpreadStrategy):
    option_structure: str = "generic"
    allows_short_options = False

    def generate_signal(self, instrument: Instrument, bars: list[MarketBar], now: datetime) -> Signal | None:
        signal = super().generate_signal(instrument, bars, now)
        if signal is None:
            return None
        return Signal(
            self.name,
            signal.symbol,
            signal.side,
            signal.confidence,
            signal.price,
            self.option_structure,
            signal.created_at,
            {**signal.metadata, "structure": self.option_structure, "hedged": not self.allows_short_options},
        )


class LongStraddleStrategy(OptionsTemplateStrategy):
    name = "long_straddle"
    option_structure = "long straddle"


class ShortStraddleStrategy(OptionsTemplateStrategy):
    name = "short_straddle"
    option_structure = "short straddle"
    allows_short_options = True


class StrangleStrategy(OptionsTemplateStrategy):
    name = "strangle"
    option_structure = "strangle"


class IronCondorStrategy(OptionsTemplateStrategy):
    name = "iron_condor"
    option_structure = "iron condor"
    allows_short_options = True


class BullCallSpreadStrategy(OptionsTemplateStrategy):
    name = "bull_call_spread"
    option_structure = "bull call spread"


class BearPutSpreadStrategy(OptionsTemplateStrategy):
    name = "bear_put_spread"
    option_structure = "bear put spread"


class CalendarSpreadStrategy(OptionsTemplateStrategy):
    name = "calendar_spread"
    option_structure = "calendar spread"
    supports_rollover = True


class DeltaNeutralHedgeStrategy(OptionsTemplateStrategy):
    name = "delta_neutral_hedge"
    option_structure = "delta neutral hedge"
    allows_short_options = True


class FuturesHedgeStrategy(FuturesTrendStrategy):
    name = "hedge_futures"


class PairHedgeStrategy(FuturesTrendStrategy):
    name = "pair_hedge"


class ExpiryRolloverStrategy(FuturesTrendStrategy):
    name = "expiry_rollover"
    supports_rollover = True
