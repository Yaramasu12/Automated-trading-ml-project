from __future__ import annotations

"""Strategy tournament — champion/challenger process."""

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StrategyScore:
    strategy_id: str
    variant: str   # baseline | neural | llm | quantum | rl
    profit_factor: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    oos_improvement: float
    eligible_for_promotion: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "variant": self.variant,
            "profit_factor": round(self.profit_factor, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "win_rate": round(self.win_rate, 4),
            "oos_improvement": round(self.oos_improvement, 4),
            "eligible_for_promotion": self.eligible_for_promotion,
        }


class StrategyTournament:
    """Champion/challenger tournament.

    Compares baseline vs neural-enhanced, LLM-reasoned, quantum-allocated,
    and RL-trained variants. Promotes only after costs and risk checks.
    """

    def __init__(
        self,
        min_profit_factor: float = 1.10,
        min_sharpe: float = 0.20,
        max_drawdown: float = 0.20,
    ) -> None:
        self._min_pf = min_profit_factor
        self._min_sharpe = min_sharpe
        self._max_dd = max_drawdown
        self._scores: list[StrategyScore] = []

    def register_result(
        self,
        strategy_id: str,
        variant: str,
        returns: list[float],
        oos_improvement: float = 0.0,
    ) -> StrategyScore:
        if not returns:
            score = StrategyScore(
                strategy_id=strategy_id, variant=variant,
                profit_factor=0.0, sharpe_ratio=0.0,
                max_drawdown=1.0, win_rate=0.0,
                oos_improvement=oos_improvement,
            )
            self._scores.append(score)
            return score

        mean = sum(returns) / len(returns)
        std = math.sqrt(sum((r - mean) ** 2 for r in returns) / max(1, len(returns)))
        sharpe = mean / max(std, 1e-6) * math.sqrt(252)

        gains = sum(r for r in returns if r > 0)
        losses = abs(sum(r for r in returns if r < 0))
        pf = gains / max(losses, 1e-6)

        cum, peak, max_dd = 0.0, 0.0, 0.0
        for r in returns:
            cum += r
            if cum > peak:
                peak = cum
            dd = (peak - cum) / max(abs(peak), 1.0) if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        win_rate = sum(1 for r in returns if r > 0) / len(returns)

        eligible = (
            pf >= self._min_pf
            and sharpe >= self._min_sharpe
            and max_dd <= self._max_dd
            and oos_improvement >= 0.0
        )

        score = StrategyScore(
            strategy_id=strategy_id,
            variant=variant,
            profit_factor=pf,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            win_rate=win_rate,
            oos_improvement=oos_improvement,
            eligible_for_promotion=eligible,
        )
        self._scores.append(score)
        return score

    def leaderboard(self) -> list[StrategyScore]:
        return sorted(
            self._scores,
            key=lambda s: (s.eligible_for_promotion, s.sharpe_ratio),
            reverse=True,
        )

    def champion(self) -> StrategyScore | None:
        eligible = [s for s in self._scores if s.eligible_for_promotion]
        if not eligible:
            return None
        return max(eligible, key=lambda s: s.sharpe_ratio)
