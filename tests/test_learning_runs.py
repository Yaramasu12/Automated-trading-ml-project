"""Tests for TradingDatabase's learning_runs claim/lease mechanism.

Implementation prompt Section 21B: a durable, lease-protected run record
per (job, IST trading session date) so duplicate scheduler processes or a
mid-day restart cannot double-count a training run. daily_scheduler.py's
own in-memory last_run_date dict already prevented re-selection within one
process's lifetime, but resets to empty on every restart. These tests
exercise the actual claim/lease/terminal-state semantics directly against
a throwaway SQLite-backed TradingDatabase — no live Postgres needed.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from trading_platform.data.persistence import TradingDatabase


class LearningRunClaimTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(db_path=Path(self._tmpdir.name) / "test.db")

    def tearDown(self):
        # Windows refuses to unlink a file with an open handle — close the
        # sqlite connection before TemporaryDirectory cleanup, same fix
        # already established in test_audit_fixes.py's _scheduler_in_tmpdir.
        self.db.close()
        self._tmpdir.cleanup()

    def test_first_claim_succeeds_and_is_running(self):
        run = self.db.claim_learning_run(
            "model_retrain", date(2026, 9, 17), lease_owner="proc-1",
        )
        self.assertIsNotNone(run)
        self.assertEqual(run["state"], "RUNNING")
        self.assertEqual(run["lease_owner"], "proc-1")

    def test_second_claim_same_day_while_lease_is_fresh_is_refused(self):
        """Two scheduler processes racing to claim the same day's job —
        only one may proceed."""
        first = self.db.claim_learning_run(
            "model_retrain", date(2026, 9, 17), lease_owner="proc-1", lease_seconds=1800,
        )
        self.assertIsNotNone(first)

        second = self.db.claim_learning_run(
            "model_retrain", date(2026, 9, 17), lease_owner="proc-2", lease_seconds=1800,
        )
        self.assertIsNone(second)

    def test_claim_after_reaching_a_terminal_state_is_refused(self):
        """The durable replacement for last_run_date: once today's run
        finished (whatever the outcome), a restarted process must not
        re-trigger it."""
        run = self.db.claim_learning_run(
            "model_retrain", date(2026, 9, 17), lease_owner="proc-1",
        )
        self.db.update_learning_run(run["id"], "TRAINED_OR_SKIPPED", detail="no edge found")

        retry = self.db.claim_learning_run(
            "model_retrain", date(2026, 9, 17), lease_owner="proc-2",
        )
        self.assertIsNone(retry)

    def test_claim_after_lease_expiry_recovers_a_crashed_run(self):
        """A process that claimed the run and then crashed (never reached a
        terminal state) must not permanently block today's job — once its
        lease expires, a new process may take over."""
        first = self.db.claim_learning_run(
            "model_retrain", date(2026, 9, 17), lease_owner="proc-1", lease_seconds=0.05,
        )
        self.assertIsNotNone(first)
        # proc-1 crashed here — never called update_learning_run.

        time.sleep(0.15)  # let the short lease actually expire

        second = self.db.claim_learning_run(
            "model_retrain", date(2026, 9, 17), lease_owner="proc-2", lease_seconds=1800,
        )
        self.assertIsNotNone(second)
        self.assertEqual(second["lease_owner"], "proc-2")
        self.assertEqual(second["id"], first["id"])  # same row, taken over — not a duplicate

    def test_different_jobs_or_different_days_do_not_collide(self):
        run1 = self.db.claim_learning_run("model_retrain", date(2026, 9, 17), lease_owner="p1")
        run2 = self.db.claim_learning_run("intraday_retrain", date(2026, 9, 17), lease_owner="p1")
        run3 = self.db.claim_learning_run("model_retrain", date(2026, 9, 18), lease_owner="p1")

        self.assertIsNotNone(run1)
        self.assertIsNotNone(run2)
        self.assertIsNotNone(run3)
        self.assertEqual(len({run1["id"], run2["id"], run3["id"]}), 3)

    def test_update_learning_run_persists_state_and_detail(self):
        run = self.db.claim_learning_run("model_retrain", date(2026, 9, 17), lease_owner="p1")
        self.db.update_learning_run(
            run["id"], "CANDIDATE_VALIDATED", detail="horizon=3 OOS AUC=0.531",
            metadata={"horizon": 3, "oos_auc": 0.531},
        )

        rows = self.db.recent_learning_runs("model_retrain")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "CANDIDATE_VALIDATED")
        self.assertEqual(rows[0]["detail"], "horizon=3 OOS AUC=0.531")
        self.assertEqual(rows[0]["metadata"]["horizon"], 3)

    def test_recent_learning_runs_filters_by_job_name(self):
        self.db.claim_learning_run("model_retrain", date(2026, 9, 17), lease_owner="p1")
        self.db.claim_learning_run("intraday_retrain", date(2026, 9, 17), lease_owner="p1")

        rows = self.db.recent_learning_runs("intraday_retrain")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_name"], "intraday_retrain")

    def test_dataset_and_job_version_round_trip(self):
        run = self.db.claim_learning_run(
            "model_retrain", date(2026, 9, 17), lease_owner="p1",
            dataset_version="750d-ending-2026-09-16", job_version="git:d1d9565",
        )
        self.assertEqual(run["dataset_version"], "750d-ending-2026-09-16")
        self.assertEqual(run["job_version"], "git:d1d9565")


if __name__ == "__main__":
    unittest.main()
