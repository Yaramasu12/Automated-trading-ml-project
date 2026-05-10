from __future__ import annotations

"""Local market simulation environment for RL policy training and evaluation.

Simulation-only. Policies trained here cannot submit live orders
until promoted through shadow → paper → canary → manual approval.
"""

import math
import random
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EnvObservation:
    features: list[float]
    portfolio_pnl: float
    open_positions: int
    volatility: float
    liquidity: float
    step: int

    def to_list(self) -> list[float]:
        return [
            *self.features,
            self.portfolio_pnl,
            float(self.open_positions),
            self.volatility,
            self.liquidity,
            float(self.step),
        ]


@dataclass
class EnvStep:
    observation: EnvObservation
    reward: float
    done: bool
    info: dict[str, Any] = field(default_factory=dict)


# Action space constants
ACTION_NOOP = 0
ACTION_PROPOSE_ENTRY = 1
ACTION_PROPOSE_EXIT = 2
ACTION_PROPOSE_HEDGE = 3
ACTION_SIZE_UP = 4
ACTION_SIZE_DOWN = 5
N_ACTIONS = 6


class TradingSimEnv:
    """Deterministic, self-contained trading simulation environment.

    No live market data — uses synthetic price paths.
    No live order submission at any point.
    """

    def __init__(
        self,
        returns: list[float] | None = None,
        volatility: float = 0.15,
        initial_capital: float = 1_000_000.0,
        max_steps: int = 252,
        drawdown_penalty: float = 2.0,
        turnover_penalty: float = 0.001,
        tail_risk_penalty: float = 1.0,
        seed: int = 42,
    ) -> None:
        self._base_returns = returns or []
        self._volatility = volatility
        self._initial_capital = initial_capital
        self._max_steps = max_steps
        self._drawdown_penalty = drawdown_penalty
        self._turnover_penalty = turnover_penalty
        self._tail_risk_penalty = tail_risk_penalty
        self._rng = random.Random(seed)
        self.reset()

    # ── Gym-like interface ───────────────────────────────────────────────────

    def reset(self) -> EnvObservation:
        self._step = 0
        self._equity = self._initial_capital
        self._peak = self._initial_capital
        self._positions = 0
        self._daily_pnl = 0.0
        self._cum_pnl = 0.0
        self._returns: list[float] = list(self._base_returns) or self._gen_returns()
        return self._obs()

    def step(self, action: int) -> EnvStep:
        if self._step >= self._max_steps or self._step >= len(self._returns):
            return EnvStep(self._obs(), reward=0.0, done=True, info={"reason": "max_steps"})

        r = self._returns[self._step]
        self._step += 1

        pnl = 0.0
        turnover = 0.0

        if action == ACTION_PROPOSE_ENTRY and self._positions < 3:
            pnl = r * self._equity * 0.05
            self._positions += 1
            turnover = self._equity * 0.05

        elif action == ACTION_PROPOSE_EXIT and self._positions > 0:
            pnl = r * self._equity * 0.05
            self._positions -= 1
            turnover = self._equity * 0.05

        elif action == ACTION_PROPOSE_HEDGE:
            pnl = -abs(r) * self._equity * 0.01  # hedge cost
            turnover = self._equity * 0.01

        elif action == ACTION_SIZE_UP and self._positions > 0:
            pnl = r * self._equity * 0.02
            self._positions = min(self._positions + 1, 5)

        elif action == ACTION_SIZE_DOWN and self._positions > 0:
            pnl = r * self._equity * 0.01
            self._positions = max(self._positions - 1, 0)

        self._equity += pnl
        self._cum_pnl += pnl
        self._daily_pnl = pnl

        drawdown = (self._peak - self._equity) / max(self._peak, 1.0)
        if self._equity > self._peak:
            self._peak = self._equity

        # Reward: PnL - penalties
        reward = pnl / self._initial_capital
        reward -= self._drawdown_penalty * max(0.0, drawdown)
        reward -= self._turnover_penalty * (turnover / self._initial_capital)
        if abs(r) > 3 * self._volatility / math.sqrt(252):
            reward -= self._tail_risk_penalty * abs(pnl) / self._initial_capital

        done = self._step >= self._max_steps or self._equity <= self._initial_capital * 0.50

        return EnvStep(
            observation=self._obs(),
            reward=reward,
            done=done,
            info={
                "pnl": pnl,
                "equity": self._equity,
                "drawdown": drawdown,
                "positions": self._positions,
                "step": self._step,
            },
        )

    def observation_space_dim(self) -> int:
        return len(self._obs().to_list())

    def action_space_size(self) -> int:
        return N_ACTIONS

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _obs(self) -> EnvObservation:
        daily_vol = self._volatility / math.sqrt(252)
        feat = [
            self._cum_pnl / max(self._initial_capital, 1.0),
            float(self._positions) / 5.0,
            daily_vol,
            min(1.0, self._step / max(1, self._max_steps)),
        ]
        return EnvObservation(
            features=feat,
            portfolio_pnl=self._cum_pnl / max(self._initial_capital, 1.0),
            open_positions=self._positions,
            volatility=daily_vol,
            liquidity=1.0,
            step=self._step,
        )

    def _gen_returns(self) -> list[float]:
        daily_vol = self._volatility / math.sqrt(252)
        drift = 0.08 / 252
        return [
            drift + self._rng.gauss(0, daily_vol)
            for _ in range(self._max_steps)
        ]
