from __future__ import annotations

"""Monte Carlo stress desk — path reshuffling, volatility shocks, and target probability."""

import math
import random
from dataclasses import dataclass, field


@dataclass
class MonteCarloResult:
    n_simulations: int
    target_probability: float
    expected_max_drawdown: float
    cvar_95: float           # Conditional Value at Risk at 95%
    median_return: float
    p10_return: float
    p90_return: float
    stress_scenarios: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n_simulations": self.n_simulations,
            "target_probability": round(self.target_probability, 4),
            "expected_max_drawdown": round(self.expected_max_drawdown, 4),
            "cvar_95": round(self.cvar_95, 6),
            "median_return": round(self.median_return, 6),
            "p10_return": round(self.p10_return, 6),
            "p90_return": round(self.p90_return, 6),
            "stress_scenarios": self.stress_scenarios,
        }


class MonteCarloSimulator:
    """Runs Monte Carlo simulations on PnL return series.

    Supports:
    - return-path reshuffling (bootstrap)
    - volatility shock
    - gap risk
    - correlation breakdown
    - losing-streak simulation
    """

    def __init__(self, n_simulations: int = 1000, seed: int = 42) -> None:
        self._n = n_simulations
        self._rng = random.Random(seed)

    def simulate(
        self,
        daily_returns: list[float],
        target_total_return: float,
        days: int = 252,
    ) -> MonteCarloResult:
        if not daily_returns:
            return MonteCarloResult(
                n_simulations=0, target_probability=0.0,
                expected_max_drawdown=0.0, cvar_95=0.0,
                median_return=0.0, p10_return=0.0, p90_return=0.0,
            )

        mean = sum(daily_returns) / len(daily_returns)
        var = sum((r - mean) ** 2 for r in daily_returns) / max(1, len(daily_returns))
        std = math.sqrt(var) if var > 0 else abs(mean) * 0.5 + 1e-4

        total_returns: list[float] = []
        max_drawdowns: list[float] = []

        for _ in range(self._n):
            # Bootstrap resample with optional shock
            sim_rets = [
                self._rng.choice(daily_returns) * self._vol_shock()
                for _ in range(days)
            ]
            total = sum(sim_rets)
            total_returns.append(total)

            # Compute max drawdown
            cum, peak, max_dd = 0.0, 0.0, 0.0
            for r in sim_rets:
                cum += r
                if cum > peak:
                    peak = cum
                dd = (peak - cum) / max(abs(peak), 1.0) if peak > 0 else 0.0
                max_dd = max(max_dd, dd)
            max_drawdowns.append(max_dd)

        total_returns.sort()
        target_prob = sum(1 for r in total_returns if r >= target_total_return) / self._n
        median = total_returns[self._n // 2]
        p10 = total_returns[int(self._n * 0.10)]
        p90 = total_returns[int(self._n * 0.90)]
        avg_dd = sum(max_drawdowns) / max(1, len(max_drawdowns))

        # CVaR 95
        tail_idx = int(self._n * 0.05)
        cvar = sum(total_returns[:tail_idx]) / max(1, tail_idx)

        # Stress scenarios
        stress = [
            {"name": "vol_spike_2x", "return": self._stress_scenario(daily_returns, days, vol_mult=2.0)},
            {"name": "vol_spike_3x", "return": self._stress_scenario(daily_returns, days, vol_mult=3.0)},
            {"name": "losing_streak_10", "return": self._stress_streak(daily_returns, days, streak=10)},
        ]

        return MonteCarloResult(
            n_simulations=self._n,
            target_probability=target_prob,
            expected_max_drawdown=avg_dd,
            cvar_95=cvar,
            median_return=median,
            p10_return=p10,
            p90_return=p90,
            stress_scenarios=stress,
        )

    def _vol_shock(self) -> float:
        """Return a random volatility shock multiplier (mostly 1.0 with occasional spikes)."""
        if self._rng.random() < 0.05:
            return self._rng.uniform(1.5, 3.0)
        return 1.0

    def _stress_scenario(
        self,
        returns: list[float],
        days: int,
        vol_mult: float,
    ) -> float:
        mean = sum(returns) / len(returns)
        std = math.sqrt(sum((r - mean) ** 2 for r in returns) / max(1, len(returns)))
        return sum(self._rng.gauss(mean, std * vol_mult) for _ in range(days))

    def _stress_streak(
        self,
        returns: list[float],
        days: int,
        streak: int,
    ) -> float:
        neg_rets = [r for r in returns if r < 0] or [-0.005]
        forced = [self._rng.choice(neg_rets) for _ in range(streak)]
        rest = [self._rng.choice(returns) for _ in range(max(0, days - streak))]
        return sum(forced) + sum(rest)
