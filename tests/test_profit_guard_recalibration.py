"""Behavioural tests for the recalibrated ProfitGuard.

The whole point of the recalibration is that the EV math now models the REAL
exit structure (1.5% stop / 3.8% target) and an honest win probability, instead
of a fictional 0.8% stop with a 2× profit floor that waved coin-flips through.

These tests lock in the intended behaviour:
  * a no-edge signal is REJECTED (its true EV is negative after costs)
  * a genuine directional edge is APPROVED
  * a bearish edge produces a profitable SHORT, not a forced long
"""
from __future__ import annotations

from trading_platform.orchestrator.profit_guard import ProfitGuard
from trading_platform.orchestrator.state import OrchestratorState
from trading_platform.orchestrator.master_orchestrator import _directional_lean


def _state(**kw) -> OrchestratorState:
    base = dict(
        underlying="NIFTY",
        crew_action="BUY",
        crew_confidence=0.60,
        crew_consensus=0.60,
        rag_win_rate=0.50,
        neural_direction_prob=0.50,
        neural_uncertainty=0.50,
        neural_passed=False,
    )
    base.update(kw)
    return OrchestratorState(**base)


def test_no_edge_signal_is_rejected():
    """A coin-flip (no neural, neutral RAG, modest crew confidence) must NOT pass.

    Under the old model EV came out ~+0.0037 and this passed. With the honest
    barrier probability (~0.29 at 2.5:1) and costs, its real EV is negative.
    """
    guard = ProfitGuard()
    result = guard.evaluate(_state())
    assert result.halt is True
    gate = result.updates["profit_gate"]
    assert gate.passed is False
    assert gate.expected_value < guard._ev_threshold


def test_genuine_long_edge_is_approved():
    """A real bullish edge (neural P(up)=0.66, supportive RAG) should pass."""
    guard = ProfitGuard()
    result = guard.evaluate(_state(
        crew_action="BUY",
        crew_confidence=0.72,
        rag_win_rate=0.60,
        neural_direction_prob=0.66,
        neural_uncertainty=0.35,
        neural_passed=True,
    ))
    assert result.halt is False
    gate = result.updates["profit_gate"]
    assert gate.passed is True
    assert gate.expected_value > guard._ev_threshold
    assert gate.win_probability > 0.29  # tilted above the no-edge barrier


def test_bearish_edge_makes_a_profitable_short():
    """A SELL with a real down-edge (neural P(up)=0.34) must be positive-EV.

    _estimate_win_probability flips the directional prob for shorts, so a strong
    bearish forecast should produce a passing short — proving the system can
    profit from down moves, not just buy-and-hope.
    """
    guard = ProfitGuard()
    result = guard.evaluate(_state(
        crew_action="SELL",
        crew_confidence=0.72,
        rag_win_rate=0.60,
        neural_direction_prob=0.34,   # P(up) low ⇒ P(down) high ⇒ good for a short
        neural_uncertainty=0.35,
        neural_passed=True,
    ))
    assert result.halt is False
    gate = result.updates["profit_gate"]
    assert gate.passed is True


def test_directional_lean_shorts_bearish_regime():
    bearish = _state(regime="BEARISH", news_sentiment=-0.4)
    bullish = _state(regime="TRENDING_UP", news_sentiment=0.3)
    assert _directional_lean(bearish) < 0
    assert _directional_lean(bullish) > 0
