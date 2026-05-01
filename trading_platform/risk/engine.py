from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading_platform.domain.enums import ExecutionMode, InstrumentType, OptionType, Side
from trading_platform.domain.models import OrderIntent
from trading_platform.portfolio.ledger import PortfolioSnapshot


@dataclass(frozen=True)
class RiskLimits:
    max_drawdown: float = 0.10
    max_daily_loss: float = 0.02
    max_position_pct: float = 0.05
    max_margin_utilization: float = 0.60
    max_orders_per_day: int = 500
    max_order_to_trade_ratio: float = 50.0
    block_naked_option_selling: bool = True
    expiry_day_open_cutoff_hour: int = 14


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    risk_score: float


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()

    def evaluate(
        self,
        intent: OrderIntent,
        portfolio: PortfolioSnapshot,
        now: datetime,
        execution_mode: ExecutionMode,
        live_armed: bool = False,
        kill_switch_active: bool = False,
        daily_pnl: float = 0.0,
        orders_sent_today: int = 0,
        trades_today: int = 1,
    ) -> RiskDecision:
        if kill_switch_active:
            return RiskDecision(False, "kill_switch_active", 1.0)
        if execution_mode == ExecutionMode.LIVE and not live_armed:
            return RiskDecision(False, "live_mode_not_armed", 1.0)
        if portfolio.drawdown >= self.limits.max_drawdown:
            return RiskDecision(False, "max_drawdown_breached", 1.0)
        if portfolio.equity <= 0:
            return RiskDecision(False, "invalid_portfolio_equity", 1.0)
        if daily_pnl < -(portfolio.equity * self.limits.max_daily_loss):
            return RiskDecision(False, "max_daily_loss_breached", 1.0)
        if orders_sent_today >= self.limits.max_orders_per_day:
            return RiskDecision(False, "max_orders_per_day_reached", 0.9)

        otr = orders_sent_today / max(1, trades_today)
        if otr > self.limits.max_order_to_trade_ratio:
            return RiskDecision(False, "order_to_trade_ratio_too_high", 0.9)

        position_pct = intent.notional_value / portfolio.equity
        if position_pct > self.limits.max_position_pct:
            return RiskDecision(False, "position_size_exceeds_limit", min(1.0, position_pct))

        instrument = intent.instrument
        if (
            self.limits.block_naked_option_selling
            and instrument.instrument_type == InstrumentType.OPTION
            and intent.signal.side == Side.SELL
            and not intent.signal.metadata.get("hedged", False)
        ):
            return RiskDecision(False, "naked_option_selling_blocked", 0.95)

        if instrument.expiry is not None:
            days_to_expiry = (instrument.expiry - now.date()).days
            if days_to_expiry < 0:
                return RiskDecision(False, "contract_expired", 1.0)
            if (
                days_to_expiry == 0
                and now.hour >= self.limits.expiry_day_open_cutoff_hour
                and intent.signal.metadata.get("opens_position", True)
            ):
                return RiskDecision(False, "expiry_day_open_cutoff", 0.85)
            if instrument.option_type in {OptionType.CE, OptionType.PE} and days_to_expiry <= 1:
                if position_pct > self.limits.max_position_pct / 2:
                    return RiskDecision(False, "near_expiry_option_size_too_large", 0.85)

        risk_score = min(0.99, position_pct / max(self.limits.max_position_pct, 0.0001))
        return RiskDecision(True, "approved", risk_score)

