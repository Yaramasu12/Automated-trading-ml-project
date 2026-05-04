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
        return StrategyExitRules(stop_loss_pct=0.012, target_pct=0.025, max_holding_days=3, square_off_before_expiry_days=1)


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
    name = "mean_reversion"
    family = "directional"

    def generate_signal(self, instrument: Instrument, bars: list[MarketBar], now: datetime) -> Signal | None:
        if len(bars) < 21:
            return None
        closes = [bar.close for bar in bars[-20:]]
        average = sum(closes) / len(closes)
        deviation = (bars[-1].close - average) / average if average else 0.0
        if abs(deviation) < 0.012:
            return None
        side = Side.SELL if deviation > 0 else Side.BUY
        return Signal(self.name, instrument.symbol, side, min(0.84, 0.55 + abs(deviation) * 8), bars[-1].close, "mean reversion", now, {"deviation": deviation})


class BreakoutStrategy(Strategy):
    name = "breakout"
    family = "directional"

    def generate_signal(self, instrument: Instrument, bars: list[MarketBar], now: datetime) -> Signal | None:
        if len(bars) < 21:
            return None
        recent_high = max(bar.high for bar in bars[-20:-1])
        recent_low = min(bar.low for bar in bars[-20:-1])
        close = bars[-1].close
        if close > recent_high:
            return Signal(self.name, instrument.symbol, Side.BUY, 0.72, close, "20-bar upside breakout", now, {"breakout": "high"})
        if close < recent_low:
            return Signal(self.name, instrument.symbol, Side.SELL, 0.72, close, "20-bar downside breakout", now, {"breakout": "low"})
        return None


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
