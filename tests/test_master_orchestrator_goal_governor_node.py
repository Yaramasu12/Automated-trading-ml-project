"""Tests for MasterOrchestrator._node_goal_governor() — Section 19 audit finding.

Found 2026-09-14: GoalGovernor's own module docstring says "SAFETY:
GoalGovernor can NEVER automatically raise hard risk limits. It can only
recommend increasing research intensity or reducing risk" — but this node
mapped its "increase_research" recommendation (falling behind the
configurable YEARLY_PROFIT_TARGET) to "SCALE_UP", which multiplied
position_size_multiplier up to 1.5x, and trading_agent.py multiplies that
directly into real order quantity
(`int(default_quantity * state.position_size_multiplier)`). Falling behind
an arbitrary profit target was silently widening real order sizes — the
exact "automatic risk escalation" the implementation prompt's Section 19
prohibits ("Replace the hardcoded annual-profit target with a
user-configurable planning input that cannot drive automatic risk
escalation"), and a direct contradiction of GoalGovernor's own contract.
"""
from __future__ import annotations

from types import SimpleNamespace

from trading_platform.orchestrator.master_orchestrator import MasterOrchestrator
from trading_platform.orchestrator.state import OrchestratorState


def _orchestrator(goal_state) -> MasterOrchestrator:
    runtime = SimpleNamespace(goal_governor=SimpleNamespace(compute_state=lambda: goal_state))
    return MasterOrchestrator(runtime)


def _state(**overrides) -> OrchestratorState:
    base = OrchestratorState(trace_id="t1", underlying="RELIANCE", symbol_universe=["RELIANCE"])
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_increase_research_never_scales_up_the_position_size():
    # The exact bug: behind target -> "increase_research" -> must NOT inflate
    # real order sizing, only (optionally) be reported for dashboards.
    goal_state = SimpleNamespace(recommendation="increase_research", on_track=False)
    orch = _orchestrator(goal_state)

    result = orch._node_goal_governor(_state(neural_uncertainty=0.1, fusion_confidence=1.0))

    assert result.updates["position_size_multiplier"] <= 1.0
    assert result.updates["goal_recommendation"] == "SCALE_UP"  # still reported, just inert on sizing


def test_reduce_risk_still_shrinks_the_position_size():
    # De-escalation must still work — this fix narrows the unsafe path, it
    # does not disable GoalGovernor's legitimate risk-reduction guidance.
    goal_state = SimpleNamespace(recommendation="reduce_risk", on_track=False)
    orch = _orchestrator(goal_state)

    result = orch._node_goal_governor(_state(neural_uncertainty=0.3, fusion_confidence=1.0))

    assert result.updates["position_size_multiplier"] < 1.0
    assert result.updates["goal_recommendation"] == "REDUCE"


def test_normal_recommendation_leaves_sizing_unchanged():
    goal_state = SimpleNamespace(recommendation="normal", on_track=True)
    orch = _orchestrator(goal_state)

    result = orch._node_goal_governor(_state(neural_uncertainty=0.2, fusion_confidence=1.0))

    assert result.updates["position_size_multiplier"] == 1.0
    assert result.updates["goal_recommendation"] == "MAINTAIN"
