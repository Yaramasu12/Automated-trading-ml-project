"""Regression: the AI council must never independently gate a trade.

2026-08-22 architecture review flagged this as a risk to watch ("AI council
and quantum layer are explicitly advisory/stub... any enhancement that
quietly lets an advisory layer gain veto power over money would violate the
project's core 'advisory != safety' invariant"). Reading
_node_specialist_crew confirmed this is NOT currently a bug: a council
HALT/REDUCE verdict only dampens crew_confidence/crew_consensus by a fixed
multiplier (0.60/0.70) -- it never sets halt=True itself. Only the crew's
OWN consensus threshold (CREW_CONSENSUS_MIN) and the real risk gates
(RiskEngine/ProfitGuard/EventRiskGuard/kill switch, all downstream of this
node) can actually stop an order. This test pins that invariant so a future
change to the council-blend logic can't silently regress it.
"""
from __future__ import annotations

import unittest
from collections import deque
from types import SimpleNamespace

from trading_platform.orchestrator.master_orchestrator import (
    CREW_CONSENSUS_MIN,
    MasterOrchestrator,
)
from trading_platform.orchestrator.state import NodeResult, OrchestratorState


class _FixedCrew:
    """Deterministic stand-in for SpecialistCrew.deliberate: always returns a
    confident, actionable BUY with consensus comfortably above the halt bar,
    so any halt observed in the test must have come from the council blend,
    not from the crew's own decision."""

    def __init__(self, action: str, consensus: float, confidence: float):
        self._action, self._consensus, self._confidence = action, consensus, confidence

    def deliberate(self, state: OrchestratorState) -> NodeResult:
        return NodeResult(updates={
            "crew_action": self._action,
            "crew_consensus": self._consensus,
            "crew_confidence": self._confidence,
        })


class _FixedCouncil:
    """Stand-in for AgentCouncilSupervisor: always returns the scripted
    action/confidence, regardless of context."""

    def __init__(self, action: str, confidence: float = 0.9):
        self.action, self.confidence = action, confidence

    def run(self, ctx):
        return SimpleNamespace(action=self.action, confidence=self.confidence)


def _orchestrator(crew, council) -> MasterOrchestrator:
    o = MasterOrchestrator.__new__(MasterOrchestrator)   # skip __init__, no runtime needed
    o._council_call_times = deque()
    o._specialist_crew = crew
    o._runtime = SimpleNamespace(agent_council=council)  # no .portfolio -- the council
    # blend's inner try/except already tolerates that (see module docstring)
    return o


def _state() -> OrchestratorState:
    return OrchestratorState(trace_id="t", underlying="NIFTY", symbol_universe=["NIFTY"], regime="TRENDING_UP")


class CouncilCannotIndependentlyGateTests(unittest.TestCase):
    def test_council_halt_does_not_block_a_confident_crew_buy(self):
        """The safety-critical property: a HALT from the council alone, with
        the crew otherwise confidently actionable, must not stop the trade."""
        crew = _FixedCrew("BUY", consensus=0.90, confidence=0.80)
        council = _FixedCouncil("HALT")
        result = _orchestrator(crew, council)._node_specialist_crew(_state())

        self.assertFalse(result.halt, "council HALT alone must never set halt=True")
        self.assertEqual(result.updates["crew_action"], "BUY")

    def test_council_halt_dampens_confidence_and_consensus(self):
        """HALT/REDUCE is real feedback, not a no-op -- it should visibly
        pull conviction down (the documented 0.60/0.70 multipliers), just
        never enough by itself to flip an actionable trade to a halt."""
        crew_consensus, crew_confidence = 0.90, 0.80
        crew = _FixedCrew("BUY", consensus=crew_consensus, confidence=crew_confidence)
        council = _FixedCouncil("HALT")
        result = _orchestrator(crew, council)._node_specialist_crew(_state())

        self.assertLess(result.updates["crew_confidence"], crew_confidence)
        self.assertLess(result.updates["crew_consensus"], crew_consensus)

    def test_council_no_trade_leaves_crew_decision_unchanged(self):
        """NO_TRADE is documented as a no-op on the crew's own numbers --
        confirms the council cannot silently veto via this action either."""
        crew_consensus, crew_confidence = 0.90, 0.80
        crew = _FixedCrew("BUY", consensus=crew_consensus, confidence=crew_confidence)
        council = _FixedCouncil("NO_TRADE")
        result = _orchestrator(crew, council)._node_specialist_crew(_state())

        self.assertFalse(result.halt)
        self.assertEqual(result.updates["crew_consensus"], crew_consensus)
        self.assertEqual(result.updates["crew_confidence"], crew_confidence)

    def test_only_the_crews_own_threshold_can_halt(self):
        """Control case: proves halt=True is reachable at all in this setup
        (via the crew's OWN low consensus, not the council), so the PROCEED
        assertions above are meaningful rather than the node never halting."""
        crew = _FixedCrew("HOLD", consensus=0.0, confidence=0.0)
        council = _FixedCouncil("PROCEED")   # even an enthusiastic council...
        result = _orchestrator(crew, council)._node_specialist_crew(_state())

        self.assertTrue(result.halt, "an inert HOLD must still halt regardless of council opinion")

    def test_council_proceed_can_flip_a_crew_hold_to_directional(self):
        """Documented rescue behavior (not a regression target, just
        confirms the blend path itself runs): council PROCEED with decent
        confidence may flip a crew HOLD into a directional trade. This is
        the crew's OWN post-blend action moving, still gated by the crew's
        own consensus check afterward -- not the council bypassing it."""
        crew = _FixedCrew("HOLD", consensus=CREW_CONSENSUS_MIN + 0.05, confidence=0.5)
        council = _FixedCouncil("PROCEED", confidence=0.9)
        result = _orchestrator(crew, council)._node_specialist_crew(_state())

        self.assertIn(result.updates.get("crew_action"), {"BUY", "SELL"})
        self.assertFalse(result.halt)


if __name__ == "__main__":
    unittest.main()
