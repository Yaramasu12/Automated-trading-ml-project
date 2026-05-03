from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trading_platform.domain.models import OrderIntent
from trading_platform.portfolio.ledger import PortfolioSnapshot


@dataclass
class CapitalCheckResult:
    approved: bool
    reason: str
    available_capital: float
    required_capital: float
    utilization_pct: float
    checked_at: str


class CapitalProtection:
    """Hard capital-floor checks before order submission.

    Ensures:
    - Net notional value fits within available margin
    - Daily loss limit has not been breached
    - Max drawdown circuit breaker has not tripped
    """

    def __init__(
        self,
        max_position_pct: float = 0.20,
        daily_loss_limit_pct: float = 0.02,
        drawdown_halt_pct: float = 0.10,
        margin_multiplier: float = 5.0,
    ) -> None:
        self.max_position_pct = max_position_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.drawdown_halt_pct = drawdown_halt_pct
        self.margin_multiplier = margin_multiplier

    def check(
        self,
        intent: OrderIntent,
        snapshot: PortfolioSnapshot,
        daily_pnl: float = 0.0,
    ) -> CapitalCheckResult:
        now = datetime.now(timezone.utc).isoformat()
        equity = snapshot.equity

        if equity <= 0:
            return CapitalCheckResult(
                approved=False,
                reason="Portfolio equity is zero or negative",
                available_capital=0.0,
                required_capital=intent.notional_value,
                utilization_pct=1.0,
                checked_at=now,
            )

        if snapshot.drawdown >= self.drawdown_halt_pct:
            return CapitalCheckResult(
                approved=False,
                reason=f"Max drawdown circuit breaker: {snapshot.drawdown:.1%} >= {self.drawdown_halt_pct:.1%}",
                available_capital=snapshot.cash,
                required_capital=intent.notional_value,
                utilization_pct=snapshot.drawdown,
                checked_at=now,
            )

        daily_loss_pct = abs(daily_pnl) / equity if daily_pnl < 0 else 0.0
        if daily_loss_pct >= self.daily_loss_limit_pct:
            return CapitalCheckResult(
                approved=False,
                reason=f"Daily loss limit: {daily_loss_pct:.1%} >= {self.daily_loss_limit_pct:.1%}",
                available_capital=snapshot.cash,
                required_capital=intent.notional_value,
                utilization_pct=daily_loss_pct,
                checked_at=now,
            )

        max_notional = equity * self.max_position_pct
        if intent.notional_value > max_notional:
            return CapitalCheckResult(
                approved=False,
                reason=f"Notional {intent.notional_value:,.0f} exceeds {self.max_position_pct:.0%} of equity {equity:,.0f}",
                available_capital=snapshot.cash,
                required_capital=intent.notional_value,
                utilization_pct=intent.notional_value / equity,
                checked_at=now,
            )

        margin_required = intent.notional_value / self.margin_multiplier
        if margin_required > snapshot.cash:
            return CapitalCheckResult(
                approved=False,
                reason=f"Insufficient margin: need {margin_required:,.0f}, available {snapshot.cash:,.0f}",
                available_capital=snapshot.cash,
                required_capital=margin_required,
                utilization_pct=margin_required / max(snapshot.cash, 1),
                checked_at=now,
            )

        return CapitalCheckResult(
            approved=True,
            reason="OK",
            available_capital=snapshot.cash,
            required_capital=intent.notional_value,
            utilization_pct=intent.notional_value / equity,
            checked_at=now,
        )
