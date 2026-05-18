from __future__ import annotations

"""EnsembleDecisionEngine — combines rule strategies, neural forecasts,
agent votes, quantum optimizer, and RL advisory into a single typed decision.

Never creates OrderIntent. Returns EnsembleOutput.
"""

import logging
from typing import TYPE_CHECKING

from trading_platform.decision_fusion.schemas import DecisionBlackboard, EnsembleOutput

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Regime-dependent weights for each source
_REGIME_WEIGHTS: dict[str, dict[str, float]] = {
    "TRENDING": {"rule": 0.35, "neural": 0.30, "agent": 0.20, "quantum": 0.10, "rl": 0.05},
    "MEAN_REVERTING": {"rule": 0.30, "neural": 0.25, "agent": 0.25, "quantum": 0.15, "rl": 0.05},
    "HIGH_VOLATILITY": {"rule": 0.40, "neural": 0.20, "agent": 0.25, "quantum": 0.10, "rl": 0.05},
    "BREAKOUT": {"rule": 0.35, "neural": 0.30, "agent": 0.20, "quantum": 0.10, "rl": 0.05},
    "unknown": {"rule": 0.40, "neural": 0.25, "agent": 0.20, "quantum": 0.10, "rl": 0.05},
}

_NO_TRADE_THRESHOLD = 0.40     # weighted score must exceed this to PROCEED
_UNCERTAINTY_VETO = 0.75       # neural uncertainty above this → NO_TRADE


class EnsembleDecisionEngine:
    """Combines all advisory signals into a typed EnsembleOutput.

    Supports:
    - weighted voting by regime
    - uncertainty penalty
    - no-trade threshold
    - champion/challenger policy routing
    """

    def __init__(
        self,
        no_trade_threshold: float = _NO_TRADE_THRESHOLD,
        uncertainty_veto: float = _UNCERTAINTY_VETO,
    ) -> None:
        self._no_trade_threshold = no_trade_threshold
        self._uncertainty_veto = uncertainty_veto

    def decide(self, bb: DecisionBlackboard) -> EnsembleOutput:
        reasoning: list[str] = []
        weights = _REGIME_WEIGHTS.get(bb.market_regime, _REGIME_WEIGHTS["unknown"])

        # ── Risk pre-check ─────────────────────────────────────────────────
        if not bb.risk_precheck_ok:
            return EnsembleOutput(
                trace_id=bb.trace_id,
                proceed=False,
                action="HALT",
                confidence=0.0,
                weighted_score=0.0,
                regime=bb.market_regime,
                uncertainty_penalty=0.0,
                reasoning=[f"risk_precheck_failed: {bb.risk_precheck_reason}"],
            )

        # ── Neural uncertainty veto ─────────────────────────────────────────
        neural_uncertainty = 0.5
        if bb.neural_bundle:
            neural_uncertainty = bb.neural_bundle.overall_uncertainty
        if neural_uncertainty > self._uncertainty_veto:
            reasoning.append(f"neural_uncertainty={neural_uncertainty:.2f} > veto threshold")
            return EnsembleOutput(
                trace_id=bb.trace_id,
                proceed=False,
                action="NO_TRADE",
                confidence=0.0,
                weighted_score=0.0,
                regime=bb.market_regime,
                uncertainty_penalty=neural_uncertainty,
                reasoning=reasoning,
            )

        # ── Agent council veto ─────────────────────────────────────────────
        agent_action = "HOLD"
        agent_confidence = 0.5
        if bb.agent_council_decision:
            agent_action = bb.agent_council_decision.action
            agent_confidence = bb.agent_council_decision.confidence
            if agent_action in ("HALT", "NO_TRADE"):
                reasoning.append(f"agent_council_veto: {agent_action}")
                return EnsembleOutput(
                    trace_id=bb.trace_id,
                    proceed=False,
                    action="NO_TRADE",
                    confidence=0.0,
                    weighted_score=0.0,
                    regime=bb.market_regime,
                    uncertainty_penalty=neural_uncertainty,
                    reasoning=reasoning,
                )

        # ── Score each source ──────────────────────────────────────────────
        # Rule strategies: score = fraction of pipeline candidates approved
        rule_score = 0.0
        if bb.pipeline_candidates:
            approved = sum(1 for c in bb.pipeline_candidates if c.get("approved", False))
            rule_score = approved / len(bb.pipeline_candidates)
        reasoning.append(f"rule_score={rule_score:.2f}")

        # Neural: direction probability mean
        neural_score = 0.5
        if bb.neural_bundle and bb.neural_bundle.forecasts:
            neural_score = sum(f.direction_probability for f in bb.neural_bundle.forecasts) / len(
                bb.neural_bundle.forecasts
            )
        reasoning.append(f"neural_score={neural_score:.2f}")

        # Agent council
        agent_score = agent_confidence if agent_action == "PROCEED" else agent_confidence * 0.5
        reasoning.append(f"agent_score={agent_score:.2f}")

        # Quantum: dual gate — requires both beats_baseline AND constraints_satisfied.
        # A result that beats the classical objective but violates cardinality or
        # liquidity constraints is penalised more harshly than one that merely
        # underperforms (it signals a solver defect, not just a weak solution).
        quantum_score = 0.5
        if bb.quantum_result:
            q = bb.quantum_result
            if q.beats_baseline and q.constraints_satisfied:
                quantum_score = 0.7   # ideal: better than baseline and basket is valid
            elif not q.constraints_satisfied:
                quantum_score = 0.2   # constraint violation — solver defect, strong penalty
            else:
                quantum_score = 0.3   # underperforms baseline but basket is valid
        reasoning.append(
            f"quantum_score={quantum_score:.2f}"
            + (
                f" (beats_baseline={bb.quantum_result.beats_baseline},"
                f" constraints_satisfied={bb.quantum_result.constraints_satisfied})"
                if bb.quantum_result else " (no_quantum_result)"
            )
        )

        # RL advisory — translate majority_action + majority_confidence into a score
        rl_advisory = bb.rl_advisory or {}
        majority_action = rl_advisory.get("majority_action")
        majority_confidence = float(rl_advisory.get("majority_confidence", 0.5))
        if majority_action in (1, 2):   # 1=ENTRY, 2=EXIT per TradingSimEnv actions
            rl_score = majority_confidence
        elif majority_action == 0:      # 0=HOLD
            rl_score = 0.5
        else:
            rl_score = float(rl_advisory.get("score", 0.5))
        reasoning.append(f"rl_score={rl_score:.2f}")

        # ── Weighted vote ──────────────────────────────────────────────────
        weighted = (
            weights["rule"] * rule_score
            + weights["neural"] * neural_score
            + weights["agent"] * agent_score
            + weights["quantum"] * quantum_score
            + weights["rl"] * rl_score
        )

        # Uncertainty penalty
        uncertainty_penalty = neural_uncertainty * 0.2
        weighted -= uncertainty_penalty
        reasoning.append(f"weighted={weighted:.3f} (after uncertainty_penalty={uncertainty_penalty:.3f})")

        # ── Final decision ─────────────────────────────────────────────────
        if weighted >= self._no_trade_threshold:
            action = "PROCEED"
            proceed = True
            if agent_action == "REDUCE":
                action = "REDUCE"
                proceed = False
        else:
            action = "NO_TRADE"
            proceed = False

        return EnsembleOutput(
            trace_id=bb.trace_id,
            proceed=proceed,
            action=action,
            confidence=weighted,
            weighted_score=weighted,
            regime=bb.market_regime,
            uncertainty_penalty=uncertainty_penalty,
            reasoning=reasoning,
        )
