from __future__ import annotations

"""GoalGovernor — tracks yearly profit target and computes Monte Carlo probability.

SAFETY: GoalGovernor can NEVER automatically raise hard risk limits.
It can only recommend increasing research intensity or reducing risk.
"""

import collections
import math
import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from trading_platform.agent.market_hours import now_ist

logger = logging.getLogger(__name__)

_MONTE_CARLO_SIMULATIONS = 500


@dataclass
class GoalState:
    """Current goal governor state."""
    yearly_target: float
    realized_pnl: float
    drawdown_budget_remaining: float
    days_elapsed: int
    trading_days_in_year: int = 252
    target_probability: float = 0.0
    required_daily_run_rate: float = 0.0
    actual_daily_run_rate: float = 0.0
    on_track: bool = False
    recommendation: str = "normal"  # normal | increase_research | reduce_risk
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "yearly_target": self.yearly_target,
            "realized_pnl": self.realized_pnl,
            "drawdown_budget_remaining": self.drawdown_budget_remaining,
            "days_elapsed": self.days_elapsed,
            "target_probability": round(self.target_probability, 4),
            "required_daily_run_rate": self.required_daily_run_rate,
            "actual_daily_run_rate": self.actual_daily_run_rate,
            "on_track": self.on_track,
            "recommendation": self.recommendation,
            "ts": self.ts.isoformat(),
        }


class GoalGovernor:
    """Tracks yearly profit target and provides guidance — never raises hard limits.

    Monte Carlo uses historical daily PnL to estimate probability of reaching the
    yearly target given remaining trading days and current trajectory.
    """

    def __init__(
        self,
        yearly_target: float = 50_000_000.0,
        max_drawdown_budget: float = 0.10,
        initial_capital: float = 1_000_000.0,
        seed: int = 42,
    ) -> None:
        self._target = yearly_target
        self._max_drawdown_budget = max_drawdown_budget
        self._initial_capital = initial_capital
        self._rng = random.Random(seed)
        self._daily_pnls: collections.deque[float] = collections.deque(maxlen=252)
        self._total_days: int = 0
        self._realized_pnl = 0.0
        self._peak_equity = initial_capital
        self._start_date = now_ist().date()

    def record_daily_pnl(self, pnl: float, equity: float) -> None:
        self._daily_pnls.append(pnl)
        self._total_days += 1
        self._realized_pnl += pnl
        if equity > self._peak_equity:
            self._peak_equity = equity

    def compute_state(self, equity: float | None = None) -> GoalState:
        current_equity = equity or (self._initial_capital + self._realized_pnl)
        days_elapsed = max(1, self._total_days)
        days_remaining = max(1, 252 - days_elapsed)

        drawdown = (self._peak_equity - current_equity) / max(self._peak_equity, 1.0)
        drawdown_budget_remaining = max(0.0, self._max_drawdown_budget - drawdown)

        # Run-rate
        actual_daily = self._realized_pnl / days_elapsed
        remaining_needed = self._target - self._realized_pnl
        required_daily = remaining_needed / days_remaining

        # Monte Carlo: sample daily PnL from historical distribution
        target_prob = self._monte_carlo_target_prob(remaining_needed, days_remaining)

        on_track = actual_daily >= required_daily * 0.80
        if target_prob < 0.20:
            recommendation = "increase_research"
        elif drawdown_budget_remaining < self._max_drawdown_budget * 0.3:
            recommendation = "reduce_risk"
        else:
            recommendation = "normal"

        return GoalState(
            yearly_target=self._target,
            realized_pnl=self._realized_pnl,
            drawdown_budget_remaining=drawdown_budget_remaining,
            days_elapsed=days_elapsed,
            target_probability=target_prob,
            required_daily_run_rate=required_daily,
            actual_daily_run_rate=actual_daily,
            on_track=on_track,
            recommendation=recommendation,
        )

    def _monte_carlo_target_prob(self, remaining_needed: float, days: int) -> float:
        if not self._daily_pnls or days <= 0:
            return 0.05

        mean = sum(self._daily_pnls) / len(self._daily_pnls)
        var = sum((p - mean) ** 2 for p in self._daily_pnls) / max(1, len(self._daily_pnls))
        std = math.sqrt(var) if var > 0 else abs(mean) * 0.5 + 1.0

        hits = 0
        for _ in range(_MONTE_CARLO_SIMULATIONS):
            simulated = sum(self._rng.gauss(mean, std) for _ in range(days))
            if simulated >= remaining_needed:
                hits += 1

        return hits / _MONTE_CARLO_SIMULATIONS

    def can_raise_risk_limits(self) -> bool:
        """ALWAYS returns False. Goal pressure must never raise hard risk limits."""
        return False

    def status(self) -> dict:
        state = self.compute_state()
        return state.to_dict()
