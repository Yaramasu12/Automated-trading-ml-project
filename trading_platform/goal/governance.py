from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum


class GoalPhase(str, Enum):
    ON_TRACK = "ON_TRACK"
    LAGGING = "LAGGING"
    AT_RISK = "AT_RISK"
    HALTED = "HALTED"


@dataclass
class GoalState:
    phase: GoalPhase
    annual_target_pct: float
    required_run_rate: float
    actual_run_rate: float
    days_elapsed: int
    days_remaining: int
    current_equity: float
    target_equity: float
    gap_pct: float
    scaling_factor: float
    message: str


class GoalGovernance:
    """Monitors annual return target and adjusts the operational phase.

    - ON_TRACK: run-rate is sufficient → full position sizing
    - LAGGING: run-rate below required → reduce sizing by 20%
    - AT_RISK: drawdown approaching limit OR gap > 2× required → reduce 40%
    - HALTED: drawdown limit breached → no new entries

    The `scaling_factor` is consumed by PositionScaler to resize OrderIntents.
    """

    def __init__(
        self,
        annual_target_pct: float = 0.40,
        start_date: date | None = None,
        start_capital: float = 2_000_000.0,
        drawdown_halt_pct: float = 0.10,
    ) -> None:
        self.annual_target_pct = annual_target_pct
        self.start_date = start_date or date(datetime.now(timezone.utc).year, 1, 1)
        self.start_capital = start_capital
        self.drawdown_halt_pct = drawdown_halt_pct

    def evaluate(
        self,
        current_equity: float,
        drawdown: float,
        as_of: date | None = None,
    ) -> GoalState:
        today = as_of or datetime.now(timezone.utc).date()
        year_start = self.start_date
        year_end = date(year_start.year, 12, 31)
        total_days = (year_end - year_start).days or 1
        days_elapsed = max((today - year_start).days, 1)
        days_remaining = max((year_end - today).days, 1)

        target_equity = self.start_capital * (1 + self.annual_target_pct)
        gap_pct = (target_equity - current_equity) / self.start_capital

        actual_gain = current_equity - self.start_capital
        actual_run_rate = (actual_gain / days_elapsed) * 365 / self.start_capital
        required_remaining = target_equity - current_equity
        required_run_rate = (required_remaining / max(days_remaining, 1)) * 365 / self.start_capital

        if drawdown >= self.drawdown_halt_pct:
            return GoalState(
                phase=GoalPhase.HALTED,
                annual_target_pct=self.annual_target_pct,
                required_run_rate=required_run_rate,
                actual_run_rate=actual_run_rate,
                days_elapsed=days_elapsed,
                days_remaining=days_remaining,
                current_equity=current_equity,
                target_equity=target_equity,
                gap_pct=gap_pct,
                scaling_factor=0.0,
                message=f"Trading halted: drawdown {drawdown:.1%} >= limit {self.drawdown_halt_pct:.1%}",
            )

        if gap_pct > 0 and required_run_rate > actual_run_rate * 2:
            return GoalState(
                phase=GoalPhase.AT_RISK,
                annual_target_pct=self.annual_target_pct,
                required_run_rate=required_run_rate,
                actual_run_rate=actual_run_rate,
                days_elapsed=days_elapsed,
                days_remaining=days_remaining,
                current_equity=current_equity,
                target_equity=target_equity,
                gap_pct=gap_pct,
                scaling_factor=0.60,
                message="Goal at risk: reduce sizing by 40% and review strategies",
            )

        if gap_pct > 0 and required_run_rate > actual_run_rate * 1.2:
            return GoalState(
                phase=GoalPhase.LAGGING,
                annual_target_pct=self.annual_target_pct,
                required_run_rate=required_run_rate,
                actual_run_rate=actual_run_rate,
                days_elapsed=days_elapsed,
                days_remaining=days_remaining,
                current_equity=current_equity,
                target_equity=target_equity,
                gap_pct=gap_pct,
                scaling_factor=0.80,
                message="Goal lagging: reduce sizing by 20% to preserve capital",
            )

        return GoalState(
            phase=GoalPhase.ON_TRACK,
            annual_target_pct=self.annual_target_pct,
            required_run_rate=required_run_rate,
            actual_run_rate=actual_run_rate,
            days_elapsed=days_elapsed,
            days_remaining=days_remaining,
            current_equity=current_equity,
            target_equity=target_equity,
            gap_pct=gap_pct,
            scaling_factor=1.0,
            message="Goal on track",
        )
