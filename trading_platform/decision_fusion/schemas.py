from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from trading_platform.agents.schemas import AgentCouncilDecision
    from trading_platform.neural.schemas import NeuralPredictionBundle


@dataclass
class DecisionBlackboard:
    """Single typed state bundle per scan cycle.

    Every downstream component reads from this. Nothing writes OrderIntent here.
    """
    trace_id: str
    execution_mode: str
    symbols: list[str]

    # From existing pipeline
    pipeline_candidates: list[dict] = field(default_factory=list)

    # From new layers (all optional / advisory)
    agent_council_decision: AgentCouncilDecision | None = None
    neural_bundle: NeuralPredictionBundle | None = None
    rl_advisory: dict[str, Any] = field(default_factory=dict)

    # Context
    portfolio_state: dict[str, Any] = field(default_factory=dict)
    market_regime: str = "unknown"
    risk_precheck_ok: bool = True
    risk_precheck_reason: str = ""

    # Goal governor state
    goal_governor_state: dict[str, Any] = field(default_factory=dict)

    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "execution_mode": self.execution_mode,
            "symbols": self.symbols,
            "n_pipeline_candidates": len(self.pipeline_candidates),
            "agent_council_action": self.agent_council_decision.action if self.agent_council_decision else None,
            "neural_uncertainty": self.neural_bundle.overall_uncertainty if self.neural_bundle else None,
            "market_regime": self.market_regime,
            "risk_precheck_ok": self.risk_precheck_ok,
            "ts": self.ts.isoformat(),
        }


@dataclass
class EnsembleOutput:
    """Final typed output from the ensemble engine."""
    trace_id: str
    proceed: bool
    action: str                    # PROCEED | REDUCE | HALT | NO_TRADE
    confidence: float
    weighted_score: float
    regime: str
    uncertainty_penalty: float
    champion_policy_id: str | None = None
    reasoning: list[str] = field(default_factory=list)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "proceed": self.proceed,
            "action": self.action,
            "confidence": self.confidence,
            "weighted_score": self.weighted_score,
            "regime": self.regime,
            "uncertainty_penalty": self.uncertainty_penalty,
            "reasoning": self.reasoning,
            "ts": self.ts.isoformat(),
        }
