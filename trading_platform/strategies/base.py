from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from trading_platform.ai.features import FeatureEngine
from trading_platform.domain.models import Instrument, MarketBar, Signal


@dataclass(frozen=True)
class StrategyRiskEstimate:
    max_loss: float
    margin_required: float
    max_gamma_exposure: float = 0.0
    short_option_exposure: float = 0.0


@dataclass(frozen=True)
class StrategyExitRules:
    stop_loss_pct: float
    target_pct: float
    max_holding_days: int
    square_off_before_expiry_days: int = 0


class Strategy(ABC):
    name: str
    family: str = "generic"
    supports_rollover: bool = False
    allows_short_options: bool = False

    def __init__(self):
        self.feature_engine = FeatureEngine()

    @abstractmethod
    def generate_signal(
        self,
        instrument: Instrument,
        bars: list[MarketBar],
        now: datetime,
    ) -> Signal | None:
        raise NotImplementedError

    def estimate_risk(self, instrument: Instrument, price: float, quantity: int) -> StrategyRiskEstimate:
        notional = abs(price * quantity * instrument.lot_size)
        return StrategyRiskEstimate(max_loss=notional * 0.02, margin_required=notional)

    def estimate_expected_return(self, instrument: Instrument, bars: list[MarketBar]) -> float:
        if len(bars) < 2 or bars[-2].close <= 0:
            return 0.0
        return (bars[-1].close - bars[-2].close) / bars[-2].close

    def required_margin(self, instrument: Instrument, price: float, quantity: int) -> float:
        return self.estimate_risk(instrument, price, quantity).margin_required

    def exit_rules(self) -> StrategyExitRules:
        return StrategyExitRules(stop_loss_pct=0.01, target_pct=0.02, max_holding_days=1)

    def expiry_rules(self) -> dict:
        return {
            "allow_rollover": self.supports_rollover,
            "force_square_off_before_expiry_days": 0,
        }
