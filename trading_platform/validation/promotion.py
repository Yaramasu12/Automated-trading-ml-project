from __future__ import annotations

"""Policy promotion gate — requires multiple gates before live trading."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PromotionStatus(str, Enum):
    RESEARCH = "research"
    SHADOW = "shadow"
    PAPER = "paper"
    LIVE_CANARY = "live_canary"
    LIVE_APPROVED = "live_approved"
    DISABLED = "disabled"


_PROMOTION_ORDER = [
    PromotionStatus.RESEARCH,
    PromotionStatus.SHADOW,
    PromotionStatus.PAPER,
    PromotionStatus.LIVE_CANARY,
    PromotionStatus.LIVE_APPROVED,
]


@dataclass
class PromotionGate:
    """Requirements for promoting to the next status."""
    tests_pass: bool = False
    data_quality_pass: bool = False
    oos_profit_factor_pass: bool = False
    sharpe_pass: bool = False
    max_drawdown_pass: bool = False
    slippage_sensitivity_pass: bool = False
    paper_canary_pass: bool = False
    manual_approval: bool = False

    @property
    def all_pass(self) -> bool:
        return all([
            self.tests_pass,
            self.data_quality_pass,
            self.oos_profit_factor_pass,
            self.sharpe_pass,
            self.max_drawdown_pass,
            self.slippage_sensitivity_pass,
        ])

    @property
    def live_approved(self) -> bool:
        return self.all_pass and self.paper_canary_pass and self.manual_approval

    def to_dict(self) -> dict:
        return {
            "tests_pass": self.tests_pass,
            "data_quality_pass": self.data_quality_pass,
            "oos_profit_factor_pass": self.oos_profit_factor_pass,
            "sharpe_pass": self.sharpe_pass,
            "max_drawdown_pass": self.max_drawdown_pass,
            "slippage_sensitivity_pass": self.slippage_sensitivity_pass,
            "paper_canary_pass": self.paper_canary_pass,
            "manual_approval": self.manual_approval,
            "all_pass": self.all_pass,
            "live_approved": self.live_approved,
        }


@dataclass
class PolicyRecord:
    policy_id: str
    status: PromotionStatus = PromotionStatus.RESEARCH
    rollback_to: PromotionStatus | None = None
    gate: PromotionGate = field(default_factory=PromotionGate)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "policy_id": self.policy_id,
            "status": self.status.value,
            "rollback_to": self.rollback_to.value if self.rollback_to else None,
            "gate": self.gate.to_dict(),
        }


class PolicyPromoter:
    """Manages promotion lifecycle with rollback support."""

    def __init__(self) -> None:
        self._records: dict[str, PolicyRecord] = {}

    def register(self, policy_id: str, gate: PromotionGate | None = None) -> PolicyRecord:
        record = PolicyRecord(policy_id=policy_id, gate=gate or PromotionGate())
        self._records[policy_id] = record
        return record

    def promote(self, policy_id: str) -> tuple[bool, str]:
        """Attempt to promote to the next status. Returns (success, reason)."""
        record = self._records.get(policy_id)
        if record is None:
            return False, "policy_not_found"

        current = record.status
        if current == PromotionStatus.DISABLED:
            return False, "policy_disabled"
        if current == PromotionStatus.LIVE_APPROVED:
            return False, "already_at_max_status"

        idx = _PROMOTION_ORDER.index(current)
        next_status = _PROMOTION_ORDER[idx + 1]

        # Validate gate requirements
        if next_status == PromotionStatus.LIVE_APPROVED:
            if not record.gate.live_approved:
                return False, "live_approval_gate_not_satisfied"
        elif not record.gate.all_pass:
            return False, "promotion_gate_not_satisfied"

        record.rollback_to = current
        record.status = next_status
        return True, f"promoted to {next_status.value}"

    def rollback(self, policy_id: str) -> tuple[bool, str]:
        record = self._records.get(policy_id)
        if record is None:
            return False, "policy_not_found"
        if record.rollback_to is None:
            record.status = PromotionStatus.DISABLED
            return True, "disabled (no rollback target)"
        record.status = record.rollback_to
        record.rollback_to = None
        return True, f"rolled back to {record.status.value}"

    def get(self, policy_id: str) -> PolicyRecord | None:
        return self._records.get(policy_id)

    def list_all(self) -> list[dict]:
        return [r.to_dict() for r in self._records.values()]
