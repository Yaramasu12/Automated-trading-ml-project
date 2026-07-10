"""SpecialistCrew — CrewAI-inspired role-based agent crew for trading decisions.

Implements the CrewAI multi-agent pattern where each agent has:
  - A dedicated ROLE (expertise domain)
  - A GOAL (what it optimises for)
  - A BACKSTORY (reasoning bias)
  - A TOOL set (data it examines)
  - VOTE output (action + confidence + structured reasoning)

The crew coordinator runs all agents, collects votes, detects dissent,
triggers a structured DEBATE if consensus < debate_threshold, then
resolves to a final crew action.

Pattern references from the 500-AI-Agents repo:
  - CrewAI: Market Analysis Multi-Agent System
  - AutoGen: Group Chat with Reflection
  - Agno: Finance Research Agent Crew
  - LangGraph: Supervisor + Specialist workers
"""
from __future__ import annotations

import logging
import os
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from trading_platform.orchestrator.state import OrchestratorState, NodeResult, AgentVoteSummary

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

DEBATE_THRESHOLD = 0.55        # consensus below this triggers debate
MIN_CONFIDENCE = 0.45          # agents below this vote HOLD by convention
CREW_PROCEED_THRESHOLD = 0.52  # crew consensus must exceed this to proceed
# |score| below this band = HOLD (no directional conviction). Tunable via env:
# lower it to trade weaker directional signals, raise it to be more selective.
HOLD_BAND = float(os.getenv("CREW_HOLD_BAND", "0.10"))


# ── Agent role definitions ────────────────────────────────────────────────────

@dataclass
class SpecialistRole:
    name: str
    role: str
    goal: str
    weights: dict[str, float]   # how much each signal type influences this agent
    bias: float                 # action bias: +1 aggressive, -1 conservative, 0 neutral


SPECIALIST_ROLES: list[SpecialistRole] = [
    SpecialistRole(
        name="TrendAnalyst",
        role="Momentum & Trend Expert",
        goal="Identify and follow strong directional trends with high win probability",
        weights={"momentum": 0.40, "neural_dir": 0.25, "regime": 0.20, "rag": 0.15},
        bias=0.05,   # slightly aggressive — trend following benefits from early entry
    ),
    SpecialistRole(
        name="MeanReversionSpecialist",
        role="Statistical Arbitrage & Reversion Expert",
        goal="Capitalise on overextended prices returning to fair value",
        weights={"rsi": 0.35, "volatility": 0.20, "neural_dir": 0.25, "rag": 0.20},
        bias=-0.05,  # slightly conservative — requires confirmation
    ),
    SpecialistRole(
        name="VolatilityArbitrageur",
        role="Options & Volatility Structure Expert",
        goal="Exploit mispriced volatility and non-directional premium opportunities",
        weights={"volatility": 0.45, "neural_uncertainty": 0.25, "tail_risk": 0.20, "regime": 0.10},
        bias=-0.10,  # prefers range-bound, avoids trending
    ),
    SpecialistRole(
        name="MacroSentimentAnalyst",
        role="News & Macro Sentiment Expert",
        goal="Filter trades against macro headwinds and news-driven risks",
        weights={"news_sentiment": 0.50, "event_risk": 0.30, "regime": 0.20},
        bias=-0.15,  # conservative — macro risk often underweighted by others
    ),
    SpecialistRole(
        name="RiskCritic",
        role="Portfolio Risk & Drawdown Guardian",
        goal="Veto trades that threaten the portfolio's survival or annual target",
        weights={"drawdown": 0.35, "daily_loss": 0.25, "consecutive_losses": 0.25, "rag": 0.15},
        bias=-0.20,  # most conservative — acts as the council's circuit breaker
    ),
    SpecialistRole(
        name="QuantitativeStrategist",
        role="Quantitative Signal Synthesis Expert",
        goal="Synthesise all quantitative signals into a high-confidence direction",
        weights={"neural_dir": 0.30, "rag": 0.25, "momentum": 0.25, "volatility": 0.20},
        bias=0.0,    # balanced — synthesises rather than leans
    ),
]


class SpecialistCrew:
    """6-agent specialist crew with structured debate.

    Designed to replace the basic AI Council stub when LLM gateway is unavailable.
    All decisions are deterministic, explainable, and reproducible.
    """

    def __init__(self, debate_threshold: float = DEBATE_THRESHOLD) -> None:
        self._debate_threshold = debate_threshold
        self._roles = SPECIALIST_ROLES

    # ── Node function ─────────────────────────────────────────────────────────

    def deliberate(self, state: OrchestratorState) -> NodeResult:
        """Run all 6 agents, collect votes, optionally debate, return crew verdict."""
        signals = self._extract_signals(state)
        votes: list[AgentVoteSummary] = []

        for role in self._roles:
            vote = self._agent_vote(role, signals, state)
            votes.append(vote)
            logger.debug("Agent %s → %s (conf=%.2f)", role.name, vote.action, vote.confidence)

        # ── Consensus calculation ──────────────────────────────────────────
        action_counts: dict[str, float] = {}
        total_weight = sum(v.weight for v in votes)
        for vote in votes:
            action_counts[vote.action] = (
                action_counts.get(vote.action, 0.0) + vote.weight * vote.confidence
            )

        if not action_counts:
            return NodeResult(
                updates={"crew_votes": votes, "crew_action": "HOLD", "crew_consensus": 0.0},
                halt=True, halt_reason="crew: no votes produced",
            )

        # Majority action by weighted confidence
        majority_action = max(action_counts, key=lambda a: action_counts[a])
        majority_score = action_counts[majority_action]
        consensus = majority_score / max(total_weight, 1e-9)

        # ── Debate trigger (AutoGen group-chat pattern) ───────────────────
        debate_triggered = False
        debate_resolution = ""
        if consensus < self._debate_threshold:
            debate_triggered = True
            majority_action, consensus, debate_resolution = self._debate(
                votes, signals, state
            )

        # Weighted average confidence
        aligned_votes = [v for v in votes if v.action == majority_action]
        crew_confidence = (
            sum(v.confidence * v.weight for v in aligned_votes) / total_weight
            if aligned_votes else 0.0
        )

        # Risk Critic veto: if RiskCritic voted HALT, override
        risk_critic_vote = next((v for v in votes if v.agent_name == "RiskCritic"), None)
        if risk_critic_vote and risk_critic_vote.action == "HALT":
            logger.info("SpecialistCrew: RiskCritic vetoed → HALT")
            return NodeResult(
                updates={
                    "crew_votes": votes,
                    "crew_action": "HALT",
                    "crew_consensus": risk_critic_vote.confidence,
                    "crew_confidence": risk_critic_vote.confidence,
                    "crew_debate_triggered": debate_triggered,
                    "crew_debate_resolution": debate_resolution or "RiskCritic veto",
                },
                halt=True,
                halt_reason=f"RiskCritic veto: {risk_critic_vote.reasoning}",
            )

        # Crew consensus gate
        if consensus < CREW_PROCEED_THRESHOLD:
            majority_action = "HOLD"
            crew_confidence = crew_confidence * 0.5

        logger.info(
            "SpecialistCrew: action=%s consensus=%.2f confidence=%.2f debate=%s",
            majority_action, consensus, crew_confidence, debate_triggered,
        )

        return NodeResult(updates={
            "crew_votes": votes,
            "crew_action": majority_action,
            "crew_consensus": round(consensus, 4),
            "crew_confidence": round(crew_confidence, 4),
            "crew_debate_triggered": debate_triggered,
            "crew_debate_resolution": debate_resolution,
        })

    # ─────────────────────────────────────────────── private: per-agent logic

    def _extract_signals(self, state: OrchestratorState) -> dict[str, Any]:
        feats = state.market_features
        return {
            "regime": state.regime,
            "regime_confidence": state.regime_confidence,
            "news_sentiment": state.news_sentiment,
            "event_risk": float(state.event_risk_active),
            "neural_dir": state.neural_direction_prob,
            "neural_uncertainty": state.neural_uncertainty,
            "tail_risk": state.neural_tail_risk,
            "correlation_risk": state.neural_correlation_risk,
            "rag_win_rate": state.rag_win_rate,
            "momentum": feats.get("momentum_20", feats.get("momentum_5", 0.0)),
            "rsi": feats.get("rsi_14", 50.0),
            "volatility": feats.get("realized_volatility", feats.get("predicted_volatility", 0.20)),
            "drawdown": state.drawdown_pct,
            "daily_loss": state.daily_loss_pct,
            "consecutive_losses": state.consecutive_losses,
        }

    def _agent_vote(
        self, role: SpecialistRole, signals: dict[str, Any], state: OrchestratorState
    ) -> AgentVoteSummary:
        """Deterministic rule-based vote per specialist role."""

        # Compute a weighted score for each signal this agent cares about
        score = 0.0
        weights = role.weights
        total_w = sum(weights.values()) or 1.0

        # momentum → +1 strong up, -1 strong down
        if "momentum" in weights:
            mom = signals["momentum"]
            mom_score = max(-1.0, min(1.0, mom * 30))
            score += weights["momentum"] * mom_score / total_w

        # neural_dir → maps [0,1] to [-1,1]
        if "neural_dir" in weights:
            nd = signals["neural_dir"]
            score += weights["neural_dir"] * (nd * 2 - 1) / total_w

        # neural_uncertainty → negative contribution (more uncertainty = lower score)
        if "neural_uncertainty" in weights:
            unc = signals["neural_uncertainty"]
            score += weights["neural_uncertainty"] * (-(unc - 0.5)) / total_w

        # regime → directional score
        if "regime" in weights:
            rs = self._regime_score(signals["regime"])
            score += weights["regime"] * rs / total_w

        # rag_win_rate → maps [0,1] to [-1,1] centred at 0.5
        if "rag" in weights:
            wr = signals["rag_win_rate"]
            score += weights["rag"] * (wr * 2 - 1) / total_w

        # rsi → contrarian signal for mean reversion
        if "rsi" in weights:
            rsi = signals["rsi"]
            rsi_score = -((rsi - 50) / 50)   # oversold→+1, overbought→-1
            score += weights["rsi"] * rsi_score / total_w

        # volatility → role-specific interpretation
        if "volatility" in weights:
            vol = signals["volatility"]
            if role.name == "VolatilityArbitrageur":
                # High vol = opportunity for premium selling
                vol_score = min(vol / 0.30, 1.0)
            else:
                # High vol = risk, negative for directional agents
                vol_score = -(min(vol / 0.30, 1.0) - 0.5)
            score += weights["volatility"] * vol_score / total_w

        # news_sentiment → direct contribution
        if "news_sentiment" in weights:
            score += weights["news_sentiment"] * signals["news_sentiment"] / total_w

        # event_risk → hard negative
        if "event_risk" in weights:
            score += weights["event_risk"] * (-signals["event_risk"]) / total_w

        # tail_risk → penalise
        if "tail_risk" in weights:
            tr = signals["tail_risk"]
            score += weights["tail_risk"] * (-(tr)) / total_w

        # drawdown → protect capital
        if "drawdown" in weights:
            dd = signals["drawdown"]
            dd_score = -(dd / 0.10)  # normalised to max_drawdown 10%
            score += weights["drawdown"] * dd_score / total_w

        if "daily_loss" in weights:
            dl = signals["daily_loss"]
            dl_score = -(dl / 0.02)
            score += weights["daily_loss"] * dl_score / total_w

        if "consecutive_losses" in weights:
            cl = signals["consecutive_losses"]
            cl_score = -(cl / 4.0)
            score += weights["consecutive_losses"] * cl_score / total_w

        # Apply role bias
        score += role.bias

        # Map score → action + confidence
        action, confidence = self._score_to_vote(score, role)
        ev_contribution = score * confidence

        reasoning = self._build_reasoning(role, signals, score, action, confidence)

        # Agent weight = role importance (can be tuned by reflection engine)
        weight = state.agent_weights.get(role.name, 1.0)

        return AgentVoteSummary(
            agent_name=role.name,
            action=action,
            confidence=round(confidence, 4),
            reasoning=reasoning,
            weight=weight,
            ev_contribution=round(ev_contribution, 4),
        )

    def _score_to_vote(self, score: float, role: SpecialistRole) -> tuple[str, float]:
        """Convert composite score to (action, confidence)."""
        # RiskCritic triggers HALT at severe portfolio stress
        if role.name == "RiskCritic":
            if score < -0.6:
                return "HALT", min(abs(score), 1.0)
            if score < -0.3:
                return "REDUCE", min(abs(score), 1.0)
            return "HOLD", max(0.4, 1.0 - abs(score))

        abs_score = abs(score)
        if abs_score < HOLD_BAND:
            return "HOLD", max(MIN_CONFIDENCE, 0.50 - abs_score)
        if abs_score < 0.25:
            # weak signal
            action = "BUY" if score > 0 else "SELL"
            confidence = 0.50 + abs_score
            return action, min(confidence, 0.70)
        if abs_score < 0.50:
            action = "BUY" if score > 0 else "SELL"
            confidence = 0.60 + abs_score * 0.5
            return action, min(confidence, 0.82)
        # strong signal
        action = "BUY" if score > 0 else "SELL"
        confidence = min(0.65 + abs_score * 0.6, 0.95)
        return action, confidence

    @staticmethod
    def _regime_score(regime: str) -> float:
        return {
            "TRENDING_UP": 0.8, "BULLISH": 0.7, "BREAKOUT": 0.6,
            "TRENDING": 0.5,
            "NEUTRAL": 0.0, "MEAN_REVERTING": 0.0, "unknown": 0.0,
            "HIGH_VOLATILITY": -0.2, "VOLATILE": -0.2,
            "TRENDING_DOWN": -0.7, "BEARISH": -0.8,
        }.get(regime, 0.0)

    @staticmethod
    def _build_reasoning(
        role: SpecialistRole,
        signals: dict,
        score: float,
        action: str,
        confidence: float,
    ) -> str:
        parts = [f"{role.role}: score={score:.3f} → {action}({confidence:.2f})"]
        if abs(signals["momentum"]) > 0.01:
            parts.append(f"mom={signals['momentum']:.3f}")
        if signals["neural_dir"] != 0.5:
            parts.append(f"P(up)={signals['neural_dir']:.2f}")
        if signals["neural_uncertainty"] > 0.60:
            parts.append(f"unc={signals['neural_uncertainty']:.2f}!")
        if signals["event_risk"]:
            parts.append("event_risk=ACTIVE")
        if signals["rag_win_rate"] < 0.45:
            parts.append(f"rag_wr={signals['rag_win_rate']:.2f}(low)")
        return " | ".join(parts)

    # ─────────────────────────────────────────────── debate (AutoGen pattern)

    def _debate(
        self,
        votes: list[AgentVoteSummary],
        signals: dict[str, Any],
        state: OrchestratorState,
    ) -> tuple[str, float, str]:
        """Structured 2-round debate between disagreeing agents.

        Round 1: Each agent states their strongest argument.
        Round 2: Agents that lose the argument concede and adjust.
        Returns (resolved_action, resolved_consensus, resolution_summary).
        """
        action_groups: dict[str, list[AgentVoteSummary]] = {}
        for v in votes:
            action_groups.setdefault(v.action, []).append(v)

        # Find the two largest groups
        sorted_groups = sorted(action_groups.items(), key=lambda x: -len(x[1]))
        if len(sorted_groups) < 2:
            majority = sorted_groups[0][0] if sorted_groups else "HOLD"
            return majority, 0.70, "no_debate_needed"

        top_action, top_agents = sorted_groups[0]
        second_action, second_agents = sorted_groups[1]

        # Debate arbiter: weight by confidence × agent seniority
        top_strength = sum(v.confidence * v.weight for v in top_agents)
        second_strength = sum(v.confidence * v.weight for v in second_agents)

        # Tie-breaker: safety bias — HOLD > SELL > BUY (prefer not to lose money)
        safety_order = {"HALT": 5, "REDUCE": 4, "HOLD": 3, "HEDGE": 2, "SELL": 1, "BUY": 0}
        if abs(top_strength - second_strength) < 0.05:
            # Near-tie → choose safer action
            winner = max(
                [(top_action, top_strength), (second_action, second_strength)],
                key=lambda x: (safety_order.get(x[0], 0), x[1])
            )
            resolved_action, resolved_strength = winner
        else:
            resolved_action = top_action if top_strength >= second_strength else second_action
            resolved_strength = max(top_strength, second_strength)

        total_strength = top_strength + second_strength + 1e-9
        resolved_consensus = resolved_strength / total_strength

        resolution = (
            f"debate: {top_action}({top_strength:.2f}) vs "
            f"{second_action}({second_strength:.2f}) → {resolved_action}"
        )
        logger.info("SpecialistCrew debate resolved: %s", resolution)
        return resolved_action, resolved_consensus, resolution
