from __future__ import annotations

"""RL trainer — simulation only. RLlib/FinRL integration optional and lazy-loaded."""

import logging
from dataclasses import dataclass
from typing import Any

from trading_platform.rl.env import TradingSimEnv
from trading_platform.rl.policies import MockPolicy, PolicyRecord, PolicyRegistry

logger = logging.getLogger(__name__)


@dataclass
class TrainingResult:
    policy_id: str
    episodes: int
    mean_reward: float
    max_drawdown: float
    backend: str


class LocalTrainer:
    """Simple episode-based trainer using the local TradingSimEnv.

    Uses deterministic mock policies in tests.
    RLlib integration available if installed.
    """

    def __init__(self, registry: PolicyRegistry | None = None) -> None:
        self._registry = registry or PolicyRegistry()

    def train(
        self,
        policy_id: str,
        role: str,
        returns: list[float] | None = None,
        n_episodes: int = 10,
    ) -> TrainingResult:
        env = TradingSimEnv(returns=returns, max_steps=min(252, len(returns) if returns else 252))
        policy = MockPolicy()  # Placeholder: replace with trained policy

        total_rewards: list[float] = []
        max_drawdowns: list[float] = []

        for _ in range(n_episodes):
            obs = env.reset()
            ep_reward = 0.0
            peak = 0.0
            cum = 0.0
            done = False
            while not done:
                action = policy.act(obs)
                step_result = env.step(action)
                ep_reward += step_result.reward
                cum += step_result.info.get("pnl", 0.0)
                if cum > peak:
                    peak = cum
                dd = (peak - cum) / max(abs(peak), 1.0) if peak > 0 else 0.0
                max_drawdowns.append(dd)
                obs = step_result.observation
                done = step_result.done
            total_rewards.append(ep_reward)

        mean_reward = sum(total_rewards) / max(1, len(total_rewards))
        max_dd = max(max_drawdowns) if max_drawdowns else 0.0

        record = PolicyRecord(policy_id=policy_id, role=role, status="research", version=1)
        self._registry.register(record, policy)

        return TrainingResult(
            policy_id=policy_id,
            episodes=n_episodes,
            mean_reward=mean_reward,
            max_drawdown=max_dd,
            backend="local_mock",
        )


class RLlibTrainer:
    """Optional RLlib trainer — lazy import; graceful fallback when not installed."""

    def is_available(self) -> bool:
        try:
            import ray.rllib  # noqa: F401
            return True
        except ImportError:
            return False

    def train(self, *args: Any, **kwargs: Any) -> TrainingResult | None:
        if not self.is_available():
            logger.debug("RLlibTrainer: ray.rllib not installed — falling back to LocalTrainer")
            return None
        # Placeholder: real RLlib integration requires model config
        return None
