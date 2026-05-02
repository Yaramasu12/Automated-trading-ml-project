from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class TargetProgress:
    annual_target: float
    start_capital: float
    current_equity: float
    realized_pnl: float
    elapsed_days: int
    required_run_rate: float
    current_gap: float
    allocation_bias: str
    max_scaling_multiplier: float


class AnnualTargetTracker:
    def __init__(self, annual_target: float = 50_000_000):
        self.annual_target = annual_target

    def evaluate(
        self,
        start_date: date,
        as_of: date,
        start_capital: float,
        current_equity: float,
        drawdown: float,
        profit_factor: float,
        sharpe: float,
    ) -> TargetProgress:
        elapsed_days = max(1, (as_of - start_date).days)
        expected_pnl = self.annual_target * min(1.0, elapsed_days / 365)
        realized_pnl = current_equity - start_capital
        current_gap = expected_pnl - realized_pnl
        required_run_rate = max(0.0, current_gap) / max(1, 365 - elapsed_days)

        if drawdown > 0.06:
            bias = "reduce_risk"
            multiplier = 0.50
        elif current_gap > 0 and profit_factor >= 1.3 and sharpe >= 1.0:
            bias = "selective_scale"
            multiplier = 1.20
        elif profit_factor < 1.0 or sharpe < 0:
            bias = "rotate_or_pause"
            multiplier = 0.75
        else:
            bias = "steady"
            multiplier = 1.0

        return TargetProgress(
            annual_target=self.annual_target,
            start_capital=start_capital,
            current_equity=current_equity,
            realized_pnl=realized_pnl,
            elapsed_days=elapsed_days,
            required_run_rate=required_run_rate,
            current_gap=current_gap,
            allocation_bias=bias,
            max_scaling_multiplier=multiplier,
        )
