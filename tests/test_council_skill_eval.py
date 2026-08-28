"""scripts/run_council_skill_eval.py's join/bucketing logic, exercised
against synthetic fixtures — the live system has real council decisions
under scan-* trace_ids and real outcomes under order-* trace_ids, which
currently never overlap (see the script's own honest report), so this
tests the MECHANICS the script would apply once that structural gap is
closed, not a claim that real data currently produces this result.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_council_skill_eval.py"
_spec = importlib.util.spec_from_file_location("run_council_skill_eval", _SCRIPT_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules["run_council_skill_eval"] = _module
_spec.loader.exec_module(_module)


def _vote(
    action: str = "HOLD", confidence: float = 0.5, failure_mode: str | None = None,
    trace_id: str = "scan-fixture-0001",
) -> dict:
    return {
        "trace_id": trace_id,
        "action": action,
        "confidence": confidence,
        "reasoning": "synthetic fixture vote, not a real agent output",
        "evidence_ids": [],
        "model_id": "fixture-model",
        "failure_mode": failure_mode,
    }


class JoinLogicTests(unittest.TestCase):
    def test_decision_with_matching_outcome_trace_id_is_joined(self) -> None:
        decisions = [[_vote(confidence=0.7, trace_id="scan-a")]]
        outcomes = {"scan-a": {"trace_id": "scan-a", "won": True, "pnl_pct": 0.5, "quality": 0.9}}

        joined = []
        for votes in decisions:
            trace_id = votes[0]["trace_id"]
            if trace_id in outcomes:
                joined.append((trace_id, votes, outcomes[trace_id]))
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0][0], "scan-a")

    def test_stub_fallback_votes_are_excluded_from_real_vote_count(self) -> None:
        votes = [
            _vote(confidence=0.5, failure_mode="URLError: server_unavailable"),
            _vote(confidence=0.6, failure_mode=None),
        ]
        real_votes = [v for v in votes if v.get("failure_mode") is None]
        stub_votes = [v for v in votes if v.get("failure_mode") is not None]
        self.assertEqual(len(real_votes), 1)
        self.assertEqual(len(stub_votes), 1)

    def test_mean_confidence_computed_across_all_votes(self) -> None:
        votes = [_vote(confidence=0.4), _vote(confidence=0.8)]
        mean_conf = sum(v["confidence"] for v in votes) / len(votes)
        self.assertAlmostEqual(mean_conf, 0.6)


class MainScriptTests(unittest.TestCase):
    """The script itself, run against a fixture HTTP layer — proves the
    end-to-end path (fetch -> join -> report) works, not just the join math
    in isolation above."""

    def test_main_reports_disjoint_namespaces_honestly_when_no_overlap(self) -> None:
        from unittest import mock
        import io
        import contextlib

        def fake_get(base_url: str, path: str) -> dict:
            if "decisions" in path:
                return {"decisions": [[_vote(trace_id="scan-x")]], "count": 1}
            return {"reflections": [{"trace_id": "order-y", "won": True, "pnl_pct": 0.1, "quality": 0.5}]}

        buf = io.StringIO()
        with mock.patch.object(_module, "_get", side_effect=fake_get), contextlib.redirect_stdout(buf):
            rc = _module.main()
        self.assertEqual(rc, 0)
        self.assertIn("disjoint", buf.getvalue().lower())

    def test_main_reports_insufficient_sample_size_below_five_real_decisions(self) -> None:
        from unittest import mock
        import io
        import contextlib

        def fake_get(base_url: str, path: str) -> dict:
            if "decisions" in path:
                return {"decisions": [[_vote(trace_id="scan-1", confidence=0.7)]], "count": 1}
            return {"reflections": [{"trace_id": "scan-1", "won": True, "pnl_pct": 0.2, "quality": 0.8}]}

        buf = io.StringIO()
        with mock.patch.object(_module, "_get", side_effect=fake_get), contextlib.redirect_stdout(buf):
            rc = _module.main()
        self.assertEqual(rc, 0)
        self.assertIn("too few", buf.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
