from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    pass  # forward refs resolved by __future__ annotations


@dataclass
class EvidenceRef:
    """Pointer to a retrieved RAG document or data source."""
    doc_id: str
    source: str
    relevance: float = 1.0
    excerpt: str = ""

    def to_dict(self) -> dict:
        return {"doc_id": self.doc_id, "source": self.source, "relevance": self.relevance}


@dataclass
class AgentInputContext:
    """Immutable context passed to every specialist agent."""
    trace_id: str
    symbols: list[str]
    execution_mode: str
    features: dict[str, Any] = field(default_factory=dict)
    portfolio_state: dict[str, Any] = field(default_factory=dict)
    market_regime: str = "unknown"
    evidence_ids: list[str] = field(default_factory=list)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "symbols": self.symbols,
            "execution_mode": self.execution_mode,
            "market_regime": self.market_regime,
            "ts": self.ts.isoformat(),
        }


@dataclass
class AgentVote:
    """Typed vote from one specialist agent."""
    agent_name: str
    action: Literal["BUY", "SELL", "HOLD", "REDUCE", "HALT", "HEDGE"]
    confidence: float  # 0.0-1.0
    reasoning: str
    evidence_ids: list[str] = field(default_factory=list)
    model_id: str = "stub"
    schema_version: str = "1.0"
    failure_mode: str | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "action": self.action,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "evidence_ids": self.evidence_ids,
            "model_id": self.model_id,
            "schema_version": self.schema_version,
            "failure_mode": self.failure_mode,
            "ts": self.ts.isoformat(),
        }


@dataclass
class StrategyProposal:
    """Structured proposal from a strategy specialist agent."""
    agent_name: str
    symbol: str
    side: str
    edge_estimate: float
    risk_estimate: float
    confidence: float
    evidence_ids: list[str] = field(default_factory=list)
    invalidation_rule: str = ""
    reasoning: str = ""
    model_id: str = "stub"

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "symbol": self.symbol,
            "side": self.side,
            "edge_estimate": self.edge_estimate,
            "risk_estimate": self.risk_estimate,
            "confidence": self.confidence,
            "invalidation_rule": self.invalidation_rule,
            "reasoning": self.reasoning,
            "model_id": self.model_id,
        }


@dataclass
class RiskCritique:
    """Risk critic assessment of a proposed trade."""
    veto: bool
    risk_score: float   # 0.0-1.0
    concerns: list[str] = field(default_factory=list)
    recommended_action: Literal["PROCEED", "REDUCE", "HALT"] = "PROCEED"
    evidence_ids: list[str] = field(default_factory=list)
    model_id: str = "stub"

    def to_dict(self) -> dict:
        return {
            "veto": self.veto,
            "risk_score": self.risk_score,
            "concerns": self.concerns,
            "recommended_action": self.recommended_action,
        }


@dataclass
class PortfolioProposal:
    """Portfolio manager's allocation recommendation."""
    preferred_basket: list[str]
    expected_return_estimate: float
    max_heat: float
    hedge_request: str | None = None
    target_run_rate_ok: bool = True
    reasoning: str = ""
    model_id: str = "stub"

    def to_dict(self) -> dict:
        return {
            "preferred_basket": self.preferred_basket,
            "expected_return_estimate": self.expected_return_estimate,
            "max_heat": self.max_heat,
            "hedge_request": self.hedge_request,
            "target_run_rate_ok": self.target_run_rate_ok,
        }


@dataclass
class ExecutionAdvice:
    """Execution analyst's timing and slicing advice."""
    avoid_windows: list[str] = field(default_factory=list)
    preferred_order_type: str = "LIMIT"
    max_slice_size_pct: float = 1.0
    reasoning: str = ""
    model_id: str = "stub"

    def to_dict(self) -> dict:
        return {
            "avoid_windows": self.avoid_windows,
            "preferred_order_type": self.preferred_order_type,
            "max_slice_size_pct": self.max_slice_size_pct,
        }


@dataclass
class DebateRound:
    """Record of one full 5-party council debate.

    Parties (in order): bull (trend) → bear (reversion) → risk critic → PM → supervisor.
    The supervisor_verdict may override the main council action.
    """
    bull_vote: AgentVote
    bear_vote: AgentVote
    risk_critique_updated: RiskCritique
    pm_adjudication: PortfolioProposal
    supervisor_verdict: Literal["CONFIRM", "REDUCE", "ABORT"]
    supervisor_reasoning: str
    action_override: str | None = None  # set when supervisor overrides the final action

    def to_dict(self) -> dict:
        return {
            "bull": {
                "agent": self.bull_vote.agent_name,
                "action": self.bull_vote.action,
                "confidence": round(self.bull_vote.confidence, 4),
                "reasoning": self.bull_vote.reasoning[:200],
            },
            "bear": {
                "agent": self.bear_vote.agent_name,
                "action": self.bear_vote.action,
                "confidence": round(self.bear_vote.confidence, 4),
                "reasoning": self.bear_vote.reasoning[:200],
            },
            "risk_score_updated": round(self.risk_critique_updated.risk_score, 4),
            "risk_recommended": self.risk_critique_updated.recommended_action,
            "risk_concerns": self.risk_critique_updated.concerns[:3],
            "pm_basket": self.pm_adjudication.preferred_basket,
            "pm_run_rate_ok": self.pm_adjudication.target_run_rate_ok,
            "supervisor_verdict": self.supervisor_verdict,
            "supervisor_reasoning": self.supervisor_reasoning,
            "action_override": self.action_override,
        }


@dataclass
class AgentCouncilDecision:
    """Final typed decision produced by AgentCouncilSupervisor.

    IMPORTANT: never contains OrderIntent or broker credentials.
    """
    trace_id: str
    action: Literal["PROCEED", "REDUCE", "HALT", "NO_TRADE"]
    confidence: float
    consensus_score: float   # 0.0-1.0; lower = more disagreement
    votes: list[AgentVote] = field(default_factory=list)
    strategy_proposals: list[StrategyProposal] = field(default_factory=list)
    risk_critique: RiskCritique | None = None
    portfolio_proposal: PortfolioProposal | None = None
    execution_advice: ExecutionAdvice | None = None
    debate_summary: str = ""
    debate_round: DebateRound | None = None
    model_ids_used: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "action": self.action,
            "confidence": self.confidence,
            "consensus_score": self.consensus_score,
            "votes": [v.to_dict() for v in self.votes],
            "strategy_proposals": [p.to_dict() for p in self.strategy_proposals],
            "risk_critique": self.risk_critique.to_dict() if self.risk_critique else None,
            "portfolio_proposal": self.portfolio_proposal.to_dict() if self.portfolio_proposal else None,
            "execution_advice": self.execution_advice.to_dict() if self.execution_advice else None,
            "debate_summary": self.debate_summary,
            "debate_round": self.debate_round.to_dict() if self.debate_round else None,
            "model_ids_used": self.model_ids_used,
            "evidence_ids": self.evidence_ids,
            "ts": self.ts.isoformat(),
        }
