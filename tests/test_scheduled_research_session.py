"""Tests for scripts/scheduled_research_session.py — the Phase 3 scheduled
wrapper around research/llm_researcher.py's existing propose/validate/holdout
loop. Mocks LLMResearcher.research() throughout: this tests the scheduling
wrapper's own orchestration (multi-instrument looping, survivor logging,
missing-data handling), not the LLM/harness logic underneath, which already
has its own test coverage.
"""
from __future__ import annotations

import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts.scheduled_research_session import _append_needs_review, main


def _reset_logging():
    """main() calls logging.basicConfig() with a file handler bound to a
    per-test temp dir — basicConfig() is a documented no-op on any call
    after the first in a process, so without this, the second test in this
    file inherits the first test's handler, pointed at an already-deleted
    temp directory. Call before each main() invocation in a test."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()


def _make_verdict(passed: bool, cagr: float = 0.1, sharpe: float = 0.5):
    from types import SimpleNamespace
    return SimpleNamespace(passed=passed, best_cagr=cagr, best_sharpe=sharpe, best_max_drawdown=0.1)


class AppendNeedsReviewTests(unittest.TestCase):
    def test_writes_a_readable_block_and_appends_across_calls(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "needs_review.log"
            with mock.patch("scripts.scheduled_research_session.NEEDS_REVIEW_PATH", path):
                _append_needs_review(
                    "RELIANCE", "vol_regime_flip", "test rationale", "def hypothesis(bars, params): ...",
                    _make_verdict(True, 0.12, 0.8), _make_verdict(True, 0.09, 0.6),
                )
                _append_needs_review(
                    "TCS", "gap_fade", "another rationale", "def hypothesis(bars, params): ...",
                    _make_verdict(True, 0.05, 0.4), _make_verdict(True, 0.04, 0.3),
                )

            text = path.read_text(encoding="utf-8")
            self.assertIn("RELIANCE", text)
            self.assertIn("vol_regime_flip", text)
            self.assertIn("TCS", text)
            self.assertIn("gap_fade", text)
            self.assertIn("NEEDS HUMAN REVIEW", text)


class MainOrchestrationTests(unittest.TestCase):
    def setUp(self):
        _reset_logging()

    def test_skips_instruments_with_no_historical_data(self):
        with mock.patch("sys.argv", ["prog", "--instruments", "NOT_A_REAL_SYMBOL_XYZ"]):
            with TemporaryDirectory() as tmp:
                with mock.patch("scripts.scheduled_research_session.LOG_DIR", Path(tmp)):
                    rc = main()
                _reset_logging()  # close the FileHandler before tmp's own cleanup (Windows can't rmtree an open file)
        self.assertEqual(rc, 0)

    def test_survivor_is_written_to_needs_review_and_non_survivor_is_not(self):
        from types import SimpleNamespace

        session = SimpleNamespace(
            attempts=[
                (SimpleNamespace(name="edge_a", rationale="r1", code="c1"), _make_verdict(True)),
                (SimpleNamespace(name="edge_b", rationale="r2", code="c2"), _make_verdict(False)),
            ],
            holdout_results={
                "edge_a": _make_verdict(True),   # survives holdout too -> should be logged
                "edge_b": _make_verdict(False),  # never reaches holdout in practice, but guard anyway
            },
            report=lambda: "mock report",
        )

        # Relies on data/historical/RELIANCE__ONE_DAY_deep.csv genuinely
        # existing in this checkout (it does — used elsewhere by
        # test_strategy_validator.py's own real-data tests) so main()'s own
        # Path.exists() check takes the real branch; load_daily_closes is
        # mocked so the CONTENT doesn't matter, avoiding a broad/risky
        # global Path.exists patch.
        real_csv = Path("data/historical/RELIANCE__ONE_DAY_deep.csv")
        if not real_csv.exists():
            self.skipTest("real historical CSV not present in this checkout")

        with mock.patch("sys.argv", ["prog", "--instruments", "RELIANCE", "--rounds", "1"]):
            with TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                with mock.patch("scripts.scheduled_research_session.LOG_DIR", tmp_path), \
                     mock.patch("scripts.scheduled_research_session.NEEDS_REVIEW_PATH", tmp_path / "needs_review.log"), \
                     mock.patch("scripts.scheduled_research_session.LLMResearcher") as mock_researcher_cls, \
                     mock.patch(
                         "scripts.scheduled_research_session.load_daily_closes",
                         return_value=[SimpleNamespace(day=f"day{i}", close=100.0 + i) for i in range(250)],
                     ):
                    mock_researcher_cls.return_value.research.return_value = session
                    rc = main()

                _reset_logging()  # close the FileHandler before tmp's own cleanup (Windows can't rmtree an open file)
                self.assertEqual(rc, 0)
                needs_review = tmp_path / "needs_review.log"
                self.assertTrue(needs_review.exists())
                text = needs_review.read_text(encoding="utf-8")
                self.assertIn("edge_a", text)
                self.assertNotIn("edge_b", text)


if __name__ == "__main__":
    unittest.main()
