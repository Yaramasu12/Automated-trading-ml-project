from __future__ import annotations

"""RL policy evaluator — measures simulated policy quality.

Advisory only. Results feed the validation factory but do NOT
produce live orders.
"""

import logging
from dataclasses import dataclass

from trading_platform.rl.env import TradingSimEnv

logger = logging.getLogger(__name__)


@dataclass
class EvalResult:
    policy_id: str
    mean_reward: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    episodes: int
    advisory_only: bool = True


class RLEvaluator:
    """Evaluate a policy on historical return sequences."""

    def evaluate(
        self,
        policy: object,
        policy_id: str,
        returns: list[float],
        n_episodes: int = 5,
    ) -> EvalResult:
        rewards: list[float] = []
        drawdowns: list[float] = []
        wins: list[bool] = []

        for _ in range(n_episodes):
            env = TradingSimEnv(returns=returns)
            obs = env.reset()
            ep_reward = 0.0
            peak = 0.0
            cum = 0.0
            done = False
            while not done:
                action = policy.act(obs)  # type: ignore[attr-defined]
                step = env.step(action)
                ep_reward += step.reward
                cum += step.info.get("pnl", 0.0)
                if cum > peak:
                    peak = cum
                dd = (peak - cum) / max(abs(peak), 1.0) if peak > 0 else 0.0
                drawdowns.append(dd)
                obs = step.observation
                done = step.done
            rewards.append(ep_reward)
            wins.append(ep_reward > 0)

        mean_r = sum(rewards) / max(1, len(rewards))
        std_r = (sum((r - mean_r) ** 2 for r in rewards) / max(1, len(rewards))) ** 0.5
        sharpe = mean_r / max(std_r, 1e-6) * (252 ** 0.5)
        max_dd = max(drawdowns) if drawdowns else 0.0
        win_rate = sum(wins) / max(1, len(wins))

        return EvalResult(
            policy_id=policy_id,
            mean_reward=mean_r,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            win_rate=win_rate,
            episodes=n_episodes,
        )
