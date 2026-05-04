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
    max_loss_per_strategy_pct: float = 0.02
    max_position_pct: float = 0.05           # options / equity: full notional cap
    max_futures_margin_pct: float = 0.20     # futures: SPAN margin cap (1 index lot ≈ 12-15%)
    max_margin_utilization: float = 0.60
    max_options_short_exposure_pct: float = 0.10
    max_gamma_near_expiry: float = 0.05
    max_symbol_exposure_pct: float = 0.10
    max_correlated_exposure_pct: float = 0.25
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
        strategy_daily_pnl: float = 0.0,
        options_short_exposure: float = 0.0,
        gamma_exposure: float = 0.0,
        symbol_exposure_pct: float | None = None,
        correlated_exposure_pct: float = 0.0,
        margin_utilization: float = 0.0,
        orders_sent_today: int = 0,
        trades_today: int = 1,
    ) -> RiskDecision:
        # Position-reducing orders must stay available during protective states
        # in BACKTEST/PAPER. Otherwise a drawdown breach or kill switch would
        # trap the simulator in the risky position it is trying to close.
        is_opening = intent.signal.metadata.get("opens_position", True)
        if kill_switch_active and is_opening:
            return RiskDecision(False, "kill_switch_active", 1.0)
        if execution_mode.value.startswith("LIVE") and not live_armed:
            return RiskDecision(False, "live_mode_not_armed", 1.0)
        if portfolio.drawdown >= self.limits.max_drawdown and is_opening:
            return RiskDecision(False, "max_drawdown_breached", 1.0)
        if portfolio.equity <= 0:
            return RiskDecision(False, "invalid_portfolio_equity", 1.0)
        if daily_pnl < -(portfolio.equity * self.limits.max_daily_loss):
            return RiskDecision(False, "max_daily_loss_breached", 1.0)
        if strategy_daily_pnl < -(portfolio.equity * self.limits.max_loss_per_strategy_pct):
            return RiskDecision(False, "max_strategy_loss_breached", 0.95)
        if margin_utilization > self.limits.max_margin_utilization:
            return RiskDecision(False, "max_margin_utilization_breached", min(1.0, margin_utilization))
        if orders_sent_today >= self.limits.max_orders_per_day:
            return RiskDecision(False, "max_orders_per_day_reached", 0.9)

        otr = orders_sent_today / max(1, trades_today)
        if otr > self.limits.max_order_to_trade_ratio:
            return RiskDecision(False, "order_to_trade_ratio_too_high", 0.9)

        # Position-growing checks only apply to orders that open / increase a
        # position. Closing orders (`opens_position=False`) reduce risk by
        # construction, so the position-size, symbol, correlation, and
        # options-short caps must not block them — otherwise a forced exit at
        # end-of-backtest (or any stop-out) would be silently rejected, leaving
        # the position open and the demo backtest's win/loss metrics empty.
        # For futures the capital at risk is the SPAN margin (~12% of notional),
        # not the full contract notional.  Using full notional always exceeds
        # position limits for any index lot, so we compare margin against
        # max_futures_margin_pct (default 20%) rather than max_position_pct (5%).
        is_future = intent.instrument.instrument_type == InstrumentType.FUTURE
        if is_future:
            effective_exposure = intent.notional_value * 0.12
            applicable_limit = self.limits.max_futures_margin_pct
        else:
            effective_exposure = intent.notional_value
            applicable_limit = self.limits.max_position_pct
        position_pct = effective_exposure / portfolio.equity
        if is_opening:
            if position_pct > applicable_limit:
                return RiskDecision(False, "position_size_exceeds_limit", min(1.0, position_pct))
            sym_exp = symbol_exposure_pct if symbol_exposure_pct is not None else position_pct
            sym_exp_limit = applicable_limit * 2 if is_future else self.limits.max_symbol_exposure_pct
            if sym_exp > sym_exp_limit:
                return RiskDecision(False, "symbol_exposure_exceeds_limit", 0.9)
            if correlated_exposure_pct > self.limits.max_correlated_exposure_pct:
                return RiskDecision(False, "correlated_exposure_exceeds_limit", 0.9)

        instrument = intent.instrument
        if is_opening and options_short_exposure > portfolio.equity * self.limits.max_options_short_exposure_pct:
            return RiskDecision(False, "options_short_exposure_exceeds_limit", 0.9)
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
                if is_opening and abs(gamma_exposure) > self.limits.max_gamma_near_expiry:
                    return RiskDecision(False, "near_expiry_gamma_exceeds_limit", 0.9)
                if is_opening and position_pct > self.limits.max_position_pct / 2:
                    return RiskDecision(False, "near_expiry_option_size_too_large", 0.85)

        risk_score = min(0.99, position_pct / max(applicable_limit, 0.0001))
        return RiskDecision(True, "approved", risk_score)
