"""trading_platform.governance.eval_harness.evaluate_council_skill — the
join/scoring logic shared by scripts/run_council_skill_eval.py (CLI, fetches
over HTTP) and GET /ai-council/skill-eval (in-process). Tested directly
against synthetic fixtures — the live system currently has real council
decisions under scan-* trace_ids and real outcomes under order-* trace_ids,
which never overlap (a real, documented structural gap, not a bug in this
logic), so these fixtures exercise the JOIN MECHANICS the function applies
once that structural gap is closed, not a claim about current live data.
"""
from __future__ import annotations

import unittest

from trading_platform.governance.eval_harness import (
    evaluate_council_skill,
    MIN_REAL_DECISIONS_FOR_CORRELATION,
    HIGH_CONFIDENCE_THRESHOLD,
)


def _vote(action: str = "HOLD", confidence: float = 0.5, failure_mode: str | None = None,
          trace_id: str = "scan-fixture-0001") -> dict:
    return {
        "trace_id": trace_id,
        "action": action,
        "confidence": confidence,
        "reasoning": "synthetic fixture vote, not a real agent output",
        "evidence_ids": [],
        "model_id": "fixture-model",
        "failure_mode": failure_mode,
    }


class EmptyInputTests(unittest.TestCase):
    def test_empty_decisions_and_reflections_returns_well_formed_zero_result(self) -> None:
        result = evaluate_council_skill([], [])
        self.assertEqual(result["joined_count"], 0)
        self.assertEqual(result["total_decisions_traced"], 0)
        self.assertEqual(result["total_outcomes_traced"], 0)
        self.assertIsNotNone(result["structural_note"])
        self.assertFalse(result["sample_size_sufficient"])

    def test_never_raises_on_malformed_decision_rows(self) -> None:
        # An empty vote list for one traced cycle — legitimate (a scan that
        # produced no council consult), must not crash the join.
        result = evaluate_council_skill([[]], [{"trace_id": "order-x", "won": True}])
        self.assertEqual(result["joined_count"], 0)


class JoinLogicTests(unittest.TestCase):
    def test_decision_with_matching_outcome_trace_id_is_joined(self) -> None:
        decisions = [[_vote(confidence=0.7, trace_id="scan-a")]]
        reflections = [{"trace_id": "scan-a", "won": True, "pnl_pct": 0.5, "quality": 0.9}]
        result = evaluate_council_skill(decisions, reflections)
        self.assertEqual(result["joined_count"], 1)
        self.assertEqual(result["joined"][0]["trace_id"], "scan-a")
        self.assertEqual(result["joined"][0]["won"], True)

    def test_decision_with_no_matching_outcome_is_not_joined(self) -> None:
        decisions = [[_vote(trace_id="scan-a")]]
        reflections = [{"trace_id": "order-b", "won": True, "pnl_pct": 0.1, "quality": 0.5}]
        result = evaluate_council_skill(decisions, reflections)
        self.assertEqual(result["joined_count"], 0)
        self.assertIsNotNone(result["structural_note"])

    def test_stub_fallback_votes_excluded_from_real_vote_count(self) -> None:
        decisions = [[
            _vote(confidence=0.5, failure_mode="URLError: server_unavailable", trace_id="scan-a"),
            _vote(confidence=0.6, failure_mode=None, trace_id="scan-a"),
        ]]
        reflections = [{"trace_id": "scan-a", "won": True, "pnl_pct": 0.2, "quality": 0.8}]
        result = evaluate_council_skill(decisions, reflections)
        self.assertEqual(result["joined"][0]["n_real_votes"], 1)
        self.assertEqual(result["joined"][0]["n_stub_votes"], 1)

    def test_mean_confidence_computed_across_all_votes(self) -> None:
        decisions = [[_vote(confidence=0.4, trace_id="scan-a"), _vote(confidence=0.8, trace_id="scan-a")]]
        reflections = [{"trace_id": "scan-a", "won": True, "pnl_pct": 0.1, "quality": 0.5}]
        result = evaluate_council_skill(decisions, reflections)
        self.assertAlmostEqual(result["joined"][0]["mean_confidence"], 0.6)


class SampleSizeGateTests(unittest.TestCase):
    def test_below_minimum_reports_insufficient_sample_no_buckets(self) -> None:
        decisions = [[_vote(confidence=0.7, trace_id=f"scan-{i}")] for i in range(MIN_REAL_DECISIONS_FOR_CORRELATION - 1)]
        reflections = [{"trace_id": f"scan-{i}", "won": True, "pnl_pct": 0.1, "quality": 0.5}
                        for i in range(MIN_REAL_DECISIONS_FOR_CORRELATION - 1)]
        result = evaluate_council_skill(decisions, reflections)
        self.assertFalse(result["sample_size_sufficient"])
        self.assertIsNone(result["buckets"])
        self.assertIn("below the minimum", result["structural_note"].lower())

    def test_at_or_above_minimum_computes_confidence_buckets(self) -> None:
        decisions = [[_vote(confidence=0.9, trace_id=f"scan-{i}")] for i in range(MIN_REAL_DECISIONS_FOR_CORRELATION)]
        reflections = [{"trace_id": f"scan-{i}", "won": True, "pnl_pct": 0.1, "quality": 1.0}
                        for i in range(MIN_REAL_DECISIONS_FOR_CORRELATION)]
        result = evaluate_council_skill(decisions, reflections)
        self.assertTrue(result["sample_size_sufficient"])
        self.assertIsNotNone(result["buckets"])
        self.assertIn("high_confidence", result["buckets"])
        self.assertEqual(result["buckets"]["high_confidence"]["n"], MIN_REAL_DECISIONS_FOR_CORRELATION)
        self.assertEqual(result["buckets"]["high_confidence"]["win_rate"], 1.0)

    def test_low_confidence_bucket_separated_from_high(self) -> None:
        n = MIN_REAL_DECISIONS_FOR_CORRELATION
        decisions = (
            [[_vote(confidence=0.9, trace_id=f"scan-hi-{i}")] for i in range(n)]
            + [[_vote(confidence=0.2, trace_id=f"scan-lo-{i}")] for i in range(n)]
        )
        reflections = (
            [{"trace_id": f"scan-hi-{i}", "won": True, "pnl_pct": 0.1, "quality": 1.0} for i in range(n)]
            + [{"trace_id": f"scan-lo-{i}", "won": False, "pnl_pct": -0.1, "quality": 0.0} for i in range(n)]
        )
        result = evaluate_council_skill(decisions, reflections)
        self.assertEqual(result["buckets"]["high_confidence"]["win_rate"], 1.0)
        self.assertEqual(result["buckets"]["low_confidence"]["win_rate"], 0.0)
        self.assertLess(HIGH_CONFIDENCE_THRESHOLD, 0.9)
        self.assertGreater(HIGH_CONFIDENCE_THRESHOLD, 0.2)


if __name__ == "__main__":
    unittest.main()
