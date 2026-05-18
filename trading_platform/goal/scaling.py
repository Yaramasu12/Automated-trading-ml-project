from __future__ import annotations

import math

from trading_platform.domain.models import OrderIntent
from trading_platform.goal.governance import GoalState


class PositionScaler:
    """Scales OrderIntent quantities based on GoalGovernance phase.

    Also applies Kelly-fraction-inspired volatility scaling when
    a volatility estimate is available.
    """

    def __init__(
        self,
        min_quantity: int = 1,
        kelly_fraction: float = 0.25,
    ) -> None:
        self.min_quantity = min_quantity
        self.kelly_fraction = kelly_fraction

    def scale(
        self,
        intent: OrderIntent,
        goal_state: GoalState | None = None,
        volatility: float | None = None,
        base_volatility: float = 0.20,
    ) -> OrderIntent:
        scale = goal_state.scaling_factor if goal_state else 1.0

        if volatility and volatility > 0 and base_volatility > 0:
            vol_ratio = base_volatility / volatility
            vol_scale = min(vol_ratio * self.kelly_fraction / 0.25, 1.5)
            scale *= vol_scale

        new_qty = max(self.min_quantity, math.floor(intent.quantity * scale))
        if new_qty == intent.quantity:
            return intent

        from dataclasses import replace
        return replace(intent, quantity=new_qty)

    def kelly_quantity(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        capital: float,
        price: float,
        lot_size: int = 1,
    ) -> int:
        if avg_loss <= 0 or avg_win <= 0 or price <= 0:
            return 1
        # Standard fractional Kelly: f* = (win_rate * avg_win - (1-win_rate) * avg_loss) / avg_win
        edge = win_rate * avg_win - (1 - win_rate) * avg_loss
        kelly_pct = max(0.0, edge / avg_win) * self.kelly_fraction
        notional = capital * kelly_pct
        qty = max(1, math.floor(notional / (price * lot_size)))
        return qty
