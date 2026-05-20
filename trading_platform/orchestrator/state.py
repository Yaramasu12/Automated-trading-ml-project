"""OrchestratorState — the single immutable-style state bundle passed through
every node in the LangGraph-inspired pipeline.

Each node receives the current state, returns an updated copy (NodeResult),
and the orchestrator merges the delta before routing to the next node.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class AgentVoteSummary:
    agent_name: str
    action: str
    confidence: float
    reasoning: str
    weight: float = 1.0
    ev_contribution: float = 0.0


@dataclass
class PatternMatch:
    """Historical pattern retrieved via Adaptive RAG."""
    pattern_id: str
    description: str
    similarity: float          # 0–1 cosine-like score
    historical_win_rate: float # how often this pattern led to profit
    avg_return: float
    sample_size: int


@dataclass
class ProfitGateResult:
    passed: bool
    expected_value: float      # probability-weighted P&L per rupee risked
    kelly_fraction: float      # optimal fraction of capital to risk
    sharpe_estimate: float     # signal / noise ratio estimate
    win_probability: float
    risk_reward_ratio: float
    reason: str = ""


@dataclass
class OrchestratorState:
    """Complete state bundle for one orchestration cycle on one underlying.

    Nodes read from this and produce a NodeResult with delta fields.
    The orchestrator merges deltas sequentially — nothing is mutated in place
    by concurrent code (the loop is synchronous-within-cycle).
    """
    # ── Identity ──────────────────────────────────────────────────────────────
    trace_id: str = ""
    underlying: str = ""
    symbol_universe: list[str] = field(default_factory=list)
    execution_mode: str = "PAPER"
    cycle_id: int = 0

    # ── Node 1: Market Intelligence (Adaptive RAG) ────────────────────────────
    regime: str = "unknown"
    regime_confidence: float = 0.5
    pattern_matches: list[PatternMatch] = field(default_factory=list)
    rag_win_rate: float = 0.5          # weighted historical win rate from patterns
    news_sentiment: float = 0.0        # -1 (very bearish) to +1 (very bullish)
    event_risk_active: bool = False
    event_risk_reason: str = ""
    market_features: dict[str, Any] = field(default_factory=dict)

    # ── Node 2: Specialist Crew (CrewAI pattern) ──────────────────────────────
    crew_votes: list[AgentVoteSummary] = field(default_factory=list)
    crew_action: str = "HOLD"          # majority action
    crew_consensus: float = 0.0        # fraction of agents aligned
    crew_confidence: float = 0.0       # weighted confidence score
    crew_debate_triggered: bool = False
    crew_debate_resolution: str = ""

    # ── Node 3: Neural Forecast ───────────────────────────────────────────────
    neural_direction_prob: float = 0.5  # P(up)
    neural_expected_return: float = 0.0
    neural_uncertainty: float = 0.5    # neutral default — 1.0 would veto every cycle
    neural_tail_risk: float = 0.0
    neural_correlation_risk: float = 0.0
    neural_model_versions: dict[str, str] = field(default_factory=dict)
    neural_passed: bool = False

    # ── Node 4: Quantum Portfolio ─────────────────────────────────────────────
    quantum_selected_symbols: list[str] = field(default_factory=list)
    quantum_improvement: float = 0.0
    quantum_backend: str = "classical"
    quantum_beats_baseline: bool = False
    quantum_objective: float = 0.0

    # ── Node 5: Risk Critic (hard gates) ─────────────────────────────────────
    risk_approved: bool = False
    risk_score: float = 0.0
    risk_reason: str = ""
    drawdown_pct: float = 0.0
    daily_loss_pct: float = 0.0
    consecutive_losses: int = 0

    # ── Node 6: Profit Guard (EV/Kelly/Sharpe) ────────────────────────────────
    profit_gate: ProfitGateResult | None = None

    # ── Node 7: Consensus Fusion (AutoGen debate pattern) ────────────────────
    fusion_score: float = 0.0         # composite weighted score 0–1
    fusion_action: str = "HOLD"       # final recommended action
    fusion_confidence: float = 0.0
    fusion_reasoning: list[str] = field(default_factory=list)
    agent_weights: dict[str, float] = field(default_factory=dict)  # adaptive weights

    # ── Node 8: Goal Governor ─────────────────────────────────────────────────
    position_size_multiplier: float = 1.0
    goal_recommendation: str = "MAINTAIN"
    goal_on_track: bool = True

    # ── Node 9: Execution Plan ────────────────────────────────────────────────
    order_candidates: list[dict[str, Any]] = field(default_factory=list)
    enqueued_count: int = 0

    # ── Node 10: Reflection (async post-trade) ────────────────────────────────
    reflection_score: float = 0.0     # outcome quality 0–1
    reflection_labels: list[dict] = field(default_factory=list)
    weight_updates: dict[str, float] = field(default_factory=dict)

    # ── Routing & metadata ────────────────────────────────────────────────────
    current_node: str = "start"
    halt_reason: str = ""            # why the pipeline was stopped early
    errors: list[str] = field(default_factory=list)
    node_timings_ms: dict[str, float] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def halted(self) -> bool:
        return bool(self.halt_reason)

    def to_summary(self) -> dict:
        pg = self.profit_gate
        return {
            "trace_id": self.trace_id,
            "underlying": self.underlying,
            "regime": self.regime,
            "crew_action": self.crew_action,
            "crew_consensus": round(self.crew_consensus, 3),
            "neural_direction_prob": round(self.neural_direction_prob, 3),
            "neural_uncertainty": round(self.neural_uncertainty, 3),
            "risk_approved": self.risk_approved,
            "profit_gate_passed": pg.passed if pg else False,
            "expected_value": round(pg.expected_value, 4) if pg else 0.0,
            "kelly_fraction": round(pg.kelly_fraction, 4) if pg else 0.0,
            "fusion_score": round(self.fusion_score, 3),
            "fusion_action": self.fusion_action,
            "fusion_confidence": round(self.fusion_confidence, 3),
            "position_size_multiplier": round(self.position_size_multiplier, 3),
            "order_candidates": len(self.order_candidates),
            "halt_reason": self.halt_reason,
            "errors": self.errors,
        }


@dataclass
class NodeResult:
    """Partial state update returned by each node.

    Only set the fields you want to update. The orchestrator merges
    this into the full OrchestratorState using shallow copy.
    """
    updates: dict[str, Any] = field(default_factory=dict)
    next_node: str = ""        # empty = let the router decide
    halt: bool = False
    halt_reason: str = ""
    error: str = ""
