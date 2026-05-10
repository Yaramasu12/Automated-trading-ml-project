from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from trading_platform.domain.models import OrderIntent


@dataclass(frozen=True)
class FinalGateDecision:
    """Authoritative pre-broker decision for an OrderIntent."""

    approved: bool
    reason: str
    stage: str = "final_execution_gate"
    risk_score: float = 0.0
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "stage": self.stage,
            "risk_score": self.risk_score,
            "details": self.details,
        }

    @classmethod
    def approve(
        cls,
        reason: str = "approved",
        *,
        stage: str = "final_execution_gate",
        risk_score: float = 0.0,
        details: dict | None = None,
    ) -> FinalGateDecision:
        return cls(True, reason, stage=stage, risk_score=risk_score, details=details or {})

    @classmethod
    def reject(
        cls,
        reason: str,
        *,
        stage: str = "final_execution_gate",
        risk_score: float = 1.0,
        details: dict | None = None,
    ) -> FinalGateDecision:
        return cls(False, reason, stage=stage, risk_score=risk_score, details=details or {})


FinalGateFn = Callable[
    [OrderIntent, datetime | None, dict[str, float] | None],
    FinalGateDecision,
]
