"""Council admission gate — bounds the per-underlying LLM fan-out.

Measured 2026-08-09: a full 10-agent council consult takes ~158s, and
_node_specialist_crew runs once PER UNDERLYING across 58 underlyings per
300s cycle — a ~30x overrun. The visible symptom was not a slow cycle but
SILENT DEGRADATION: per-agent timeouts fired, every vote fell back to a canned
stub, and the council still reported itself healthy.
"""
from __future__ import annotations

import unittest
from collections import deque

from trading_platform.orchestrator.master_orchestrator import (
    COUNCIL_MARGINAL_CONSENSUS_MIN,
    COUNCIL_MAX_CALLS_PER_CYCLE,
    MasterOrchestrator,
)
from trading_platform.orchestrator.state import OrchestratorState


def _orch() -> MasterOrchestrator:
    o = MasterOrchestrator.__new__(MasterOrchestrator)   # no runtime needed
    o._council_call_times = deque()
    return o


class CouncilAdmissionTests(unittest.TestCase):
    def test_inert_hold_is_skipped(self):
        """The 30x-overrun case: a HOLD nowhere near actionable has nothing to
        veto, and this node halts a few lines later regardless."""
        admitted, reason = _orch()._council_admission(
            crew_action="HOLD", crew_consensus=0.05)
        self.assertFalse(admitted)
        self.assertIn("inert HOLD", reason)

    def test_marginal_hold_is_admitted_while_budget_remains(self):
        admitted, _ = _orch()._council_admission(
            crew_action="HOLD", crew_consensus=COUNCIL_MARGINAL_CONSENSUS_MIN + 0.01)
        self.assertTrue(admitted)

    def test_actionable_candidate_is_always_admitted(self):
        for action in ("BUY", "SELL"):
            with self.subTest(action=action):
                admitted, _ = _orch()._council_admission(
                    crew_action=action, crew_consensus=0.0)
                self.assertTrue(admitted)

    def test_budget_caps_marginal_holds(self):
        o = _orch()
        c = COUNCIL_MARGINAL_CONSENSUS_MIN + 0.01
        for _ in range(COUNCIL_MAX_CALLS_PER_CYCLE):
            self.assertTrue(o._council_admission(crew_action="HOLD", crew_consensus=c)[0])
        admitted, reason = o._council_admission(crew_action="HOLD", crew_consensus=c)
        self.assertFalse(admitted)
        self.assertIn("budget", reason)

    def test_budget_NEVER_suppresses_an_actionable_candidate(self):
        """The safety-critical property. A budget that could silence the veto on
        a real entry would turn a cost control into a safety regression — the
        council's remaining job is vetting things that are about to trade."""
        o = _orch()
        c = COUNCIL_MARGINAL_CONSENSUS_MIN + 0.01
        for _ in range(COUNCIL_MAX_CALLS_PER_CYCLE * 3):
            o._council_admission(crew_action="HOLD", crew_consensus=c)
        self.assertTrue(o.council_budget_status()["exhausted"])
        admitted, reason = o._council_admission(crew_action="BUY", crew_consensus=0.9)
        self.assertTrue(admitted, f"actionable candidate was budget-skipped: {reason}")

    def test_budget_window_is_self_clearing(self):
        """Rolling window, not a per-cycle reset — it must not get stuck 'spent'
        if a cycle aborts partway through."""
        o = _orch()
        c = COUNCIL_MARGINAL_CONSENSUS_MIN + 0.01
        for _ in range(COUNCIL_MAX_CALLS_PER_CYCLE):
            o._council_admission(crew_action="HOLD", crew_consensus=c)
        self.assertTrue(o.council_budget_status()["exhausted"])
        o._council_call_times = deque(t - 10_000 for t in o._council_call_times)  # age them out
        self.assertFalse(o.council_budget_status()["exhausted"])
        self.assertTrue(o._council_admission(crew_action="HOLD", crew_consensus=c)[0])

    def test_skip_is_recorded_as_not_consulted_not_a_fake_vote(self):
        """A skipped council must never masquerade as one that ran and
        abstained — that is the 'inert component presented as intelligence'
        failure this repo exists to avoid."""
        s = OrchestratorState(trace_id="t", underlying="NIFTY", symbol_universe=[])
        self.assertTrue(s.council_consulted, "default must be True (council ran)")
        self.assertEqual(s.council_skip_reason, "")

    def test_status_is_observable(self):
        o = _orch()
        st = o.council_budget_status()
        for key in ("calls_in_window", "max_calls", "window_seconds", "exhausted"):
            self.assertIn(key, st)


if __name__ == "__main__":
    unittest.main()
