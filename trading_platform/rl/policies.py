from __future__ import annotations

"""RL policy definitions.

SAFETY: policies cannot place live orders. They return integer action IDs only.
Promotion to live requires shadow → paper → canary → manual approval.
"""

import random
from dataclasses import dataclass, field
from typing import Any, Callable

from trading_platform.rl.env import (
    ACTION_NOOP,
    ACTION_PROPOSE_ENTRY,
    ACTION_PROPOSE_EXIT,
    N_ACTIONS,
    EnvObservation,
)


class MockPolicy:
    """Deterministic mock policy for tests — never calls live systems."""

    def __init__(self, fixed_action: int = ACTION_NOOP) -> None:
        self._action = fixed_action

    def act(self, obs: EnvObservation) -> int:
        return self._action

    @property
    def can_submit_live_orders(self) -> bool:
        return False


class RandomPolicy:
    """Random action policy — baseline for comparison."""

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(seed)

    def act(self, obs: EnvObservation) -> int:
        return self._rng.randint(0, N_ACTIONS - 1)

    @property
    def can_submit_live_orders(self) -> bool:
        return False


class SimpleMomentumPolicy:
    """Heuristic momentum policy — entry when cumPnL rising, exit on drawdown."""

    def __init__(self, entry_threshold: float = 0.002, exit_threshold: float = -0.005) -> None:
        self._entry_threshold = entry_threshold
        self._exit_threshold = exit_threshold
        self._prev_pnl: float | None = None

    def act(self, obs: EnvObservation) -> int:
        pnl = obs.portfolio_pnl
        if self._prev_pnl is None:
            self._prev_pnl = pnl
            return ACTION_NOOP

        delta = pnl - self._prev_pnl
        self._prev_pnl = pnl

        if delta >= self._entry_threshold and obs.open_positions < 3:
            return ACTION_PROPOSE_ENTRY
        if delta <= self._exit_threshold and obs.open_positions > 0:
            return ACTION_PROPOSE_EXIT
        return ACTION_NOOP

    @property
    def can_submit_live_orders(self) -> bool:
        return False


@dataclass
class PolicyRecord:
    """Registry entry for a trained policy."""
    policy_id: str
    role: str                  # entry | exit | sizing | hedge | execution | adversarial
    status: str = "research"   # research | shadow | paper | live_canary | live_approved | disabled
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def can_submit_live_orders(self) -> bool:
        return False  # policies NEVER submit live orders — that stays in ExecutionScheduler


class PolicyRegistry:
    """In-memory registry of RL policies and their promotion status."""

    def __init__(self) -> None:
        self._records: dict[str, PolicyRecord] = {}
        self._policies: dict[str, Any] = {}

    def register(self, record: PolicyRecord, policy: Any) -> None:
        self._records[record.policy_id] = record
        self._policies[record.policy_id] = policy

    def get(self, policy_id: str) -> Any | None:
        return self._policies.get(policy_id)

    def get_record(self, policy_id: str) -> PolicyRecord | None:
        return self._records.get(policy_id)

    def promote(self, policy_id: str, new_status: str) -> bool:
        valid = {"research", "shadow", "paper", "live_canary", "live_approved", "disabled"}
        if new_status not in valid:
            return False
        if policy_id not in self._records:
            return False
        self._records[policy_id].status = new_status
        return True

    def list_all(self) -> list[dict]:
        return [
            {
                "policy_id": r.policy_id,
                "role": r.role,
                "status": r.status,
                "version": r.version,
            }
            for r in self._records.values()
        ]
