"""Tests for MasterOrchestrator._node_consensus_fusion()'s risk_score penalty.

Found 2026-09-14 (Section 19 audit sweep for advisory signals with no real
downstream effect): _node_risk_critic (Node 4) computes risk_score =
max(drawdown, daily_loss_pct) specifically "so Node 7 can weight it" per its
own comment — but nothing downstream ever read state.risk_score. The hard
drawdown/daily-loss breach halts in Node 4 are unaffected either way (they
halt directly, inline, regardless of Node 7), so this was not a safety gap,
but a real gap between a documented design intent and what the code
actually did: the fusion score gave zero extra weight to drawdown/daily-loss
proximity below the hard breach threshold. Fixed by folding risk_score into
the same penalty term uncertainty/tail-risk/correlation already use.
"""
from __future__ import annotations

from types import SimpleNamespace

from trading_platform.orchestrator.master_orchestrator import MasterOrchestrator
from trading_platform.orchestrator.state import OrchestratorState


def _orchestrator() -> MasterOrchestrator:
    # _node_consensus_fusion's RL-vote block wraps all runtime attribute
    # access in its own try/except (non-critical, logged and skipped), so a
    # bare namespace with nothing on it is sufficient here.
    return MasterOrchestrator(SimpleNamespace())


def _state(risk_score: float) -> OrchestratorState:
    return OrchestratorState(
        trace_id="t1",
        underlying="RELIANCE",
        symbol_universe=["RELIANCE"],
        crew_action="BUY",
        crew_confidence=0.8,
        neural_direction_prob=0.7,
        rag_win_rate=0.6,
        risk_score=risk_score,
    )


def test_nonzero_risk_score_reduces_fusion_score_versus_zero():
    orch = _orchestrator()

    calm = orch._node_consensus_fusion(_state(risk_score=0.0))
    stressed = orch._node_consensus_fusion(_state(risk_score=0.9))

    assert stressed.updates["fusion_score"] < calm.updates["fusion_score"]
    # 0.9 * 0.3 penalty / 2 (score is remapped from [-1,1] to [0,1]) = 0.135
    assert abs((calm.updates["fusion_score"] - stressed.updates["fusion_score"]) - 0.135) < 1e-6


def test_risk_score_can_push_fusion_below_the_proceed_threshold():
    """A high (but sub-breach) risk_score must be able to tip an otherwise
    proceed-worthy signal into a HOLD/halt — this is the actual behavioral
    change; before the fix risk_score had no way to influence this at all."""
    from trading_platform.orchestrator.master_orchestrator import FUSION_PROCEED_THRESHOLD

    orch = _orchestrator()
    borderline_state = OrchestratorState(
        trace_id="t2", underlying="RELIANCE", symbol_universe=["RELIANCE"],
        crew_action="BUY", crew_confidence=0.55, neural_direction_prob=0.55,
        rag_win_rate=0.5, risk_score=0.0,
    )
    calm = orch._node_consensus_fusion(borderline_state)
    assert calm.updates["fusion_score"] >= FUSION_PROCEED_THRESHOLD  # proceeds when calm

    borderline_state.risk_score = 1.0
    stressed = orch._node_consensus_fusion(borderline_state)
    assert stressed.halt is True
    assert stressed.updates["fusion_action"] == "HOLD"
