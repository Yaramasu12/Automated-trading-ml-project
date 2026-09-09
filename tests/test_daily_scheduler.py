"""Tests for scripts/daily_scheduler.py's job-timing logic.

Found 2026-09-07 (TradingQA's investigation into trades_db.daily_pnl having
zero rows for its entire existence): daily_pnl_report (15:36 IST) sits just
1 minute after stop_feed (15:35 IST), and the main loop's own post-job
time.sleep(90) cooldown reliably pushed "now" past 15:36 before _next_run()
was next evaluated for it — so it always looked like "already passed today,
defer to tomorrow," forever. These tests guard the fix (a bounded grace
window) directly, without needing to run the real scheduler loop.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import daily_scheduler  # noqa: E402


def _ist(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=daily_scheduler.IST)


class NextRunGracePeriodTests(unittest.TestCase):
    MONDAY = (2026, 9, 7)  # a real trading day (not a holiday, not a weekend)

    def test_slot_a_minute_in_the_past_still_runs_today(self):
        # Exactly the observed bug: stop_feed (15:35) finished, its 90s
        # cooldown pushed the clock to 15:36:30, and daily_pnl_report (15:36)
        # must still be treated as due NOW, not deferred to tomorrow.
        now = _ist(*self.MONDAY, 15, 36) + timedelta(seconds=30)
        with mock.patch.object(daily_scheduler, "_now_ist", return_value=now):
            result = daily_scheduler._next_run(15, 36)
        self.assertEqual(result.date(), now.date())
        self.assertEqual((result.hour, result.minute), (15, 36))

    def test_slot_far_in_the_past_defers_to_tomorrow(self):
        # A genuinely stale slot (scheduler was down for hours) must NOT
        # fire late — only the near-miss case (cooldown overshoot) should.
        now = _ist(*self.MONDAY, 18, 0)  # 15:36 was 2h24m ago
        with mock.patch.object(daily_scheduler, "_now_ist", return_value=now):
            result = daily_scheduler._next_run(15, 36)
        self.assertGreater(result.date(), now.date())
        self.assertEqual((result.hour, result.minute), (15, 36))

    def test_slot_still_in_the_future_is_unaffected(self):
        now = _ist(*self.MONDAY, 15, 30)
        with mock.patch.object(daily_scheduler, "_now_ist", return_value=now):
            result = daily_scheduler._next_run(15, 36)
        self.assertEqual(result.date(), now.date())
        self.assertEqual((result.hour, result.minute), (15, 36))

    def test_just_past_the_grace_window_defers_to_tomorrow(self):
        # 10 minutes exactly is still within grace (strict > in the
        # implementation); one second past it must roll to tomorrow.
        now = _ist(*self.MONDAY, 15, 46) + timedelta(seconds=1)
        with mock.patch.object(daily_scheduler, "_now_ist", return_value=now):
            result = daily_scheduler._next_run(15, 36)
        self.assertGreater(result.date(), now.date())

    def test_exactly_the_grace_window_boundary_still_runs_today(self):
        now = _ist(*self.MONDAY, 15, 46)  # exactly 10 minutes after 15:36
        with mock.patch.object(daily_scheduler, "_now_ist", return_value=now):
            result = daily_scheduler._next_run(15, 36)
        self.assertEqual(result.date(), now.date())

    def test_recently_passed_slot_on_a_non_trading_day_still_rolls_forward(self):
        # A Saturday recently-passed slot must not fire on the weekend just
        # because it's within the grace window.
        saturday = _ist(2026, 9, 5, 15, 40)  # 2026-09-05 is a Saturday
        with mock.patch.object(daily_scheduler, "_now_ist", return_value=saturday):
            result = daily_scheduler._next_run(15, 36)
        self.assertTrue(daily_scheduler._is_trading_day(result))
        self.assertGreater(result.date(), saturday.date())


class AlreadyRanTodaySuppressesReselectionTests(unittest.TestCase):
    """Found live 2026-09-08: the grace-window fix above (correctly) makes a
    recently-passed slot still count as "due today" — but main()'s loop had
    no memory of whether that slot's job had already actually run, so it kept
    re-selecting and re-executing the SAME job every ~90s for the entire
    10-minute grace window (~6-7 duplicate runs). Confirmed via the live
    risk_events audit trail: eod_square_off_triggered fired 7 times 90s apart
    for scheduled_eod_square_off_15:20_ist, and square_off_requested fired 7
    times 90s apart for mcx_eod_squareoff_23:25 — both exactly matching this
    pattern. These tests guard the already_ran_today parameter that fixes it.
    """

    MONDAY = (2026, 9, 7)

    def test_already_ran_today_rolls_to_tomorrow_even_inside_grace_window(self):
        # Same instant as test_slot_a_minute_in_the_past_still_runs_today
        # above (30s after 15:36, well inside the 10-minute grace window) —
        # but this time the caller says the job already ran today.
        now = _ist(*self.MONDAY, 15, 36) + timedelta(seconds=30)
        with mock.patch.object(daily_scheduler, "_now_ist", return_value=now):
            result = daily_scheduler._next_run(15, 36, already_ran_today=True)
        self.assertGreater(result.date(), now.date())
        self.assertEqual((result.hour, result.minute), (15, 36))

    def test_already_ran_today_false_is_unaffected(self):
        # Default value preserves the original grace-window behavior exactly.
        now = _ist(*self.MONDAY, 15, 36) + timedelta(seconds=30)
        with mock.patch.object(daily_scheduler, "_now_ist", return_value=now):
            result = daily_scheduler._next_run(15, 36, already_ran_today=False)
        self.assertEqual(result.date(), now.date())

    def test_a_job_never_run_today_is_reselected_as_due_until_it_runs(self):
        # A job that has NOT run yet must still be reported as due within the
        # grace window — already_ran_today alone must not suppress genuinely
        # pending work, only re-firing of work that already completed.
        now = _ist(*self.MONDAY, 15, 20) + timedelta(minutes=3)
        with mock.patch.object(daily_scheduler, "_now_ist", return_value=now):
            result = daily_scheduler._next_run(15, 20, already_ran_today=False)
        self.assertEqual(result.date(), now.date())

    def test_main_loop_selects_each_job_at_most_once_per_day(self):
        """End-to-end simulation of main()'s own selection loop (without
        actually running it) proving a job caught by the grace window is
        picked exactly once, not on every iteration until the window lapses."""
        job_hour, job_minute = 15, 20
        job_name = "eod_square_off"
        start = _ist(*self.MONDAY, job_hour, job_minute) + timedelta(seconds=30)

        last_run_date: dict[str, object] = {}
        run_count = 0
        # Simulate ~10 loop iterations, 90s apart, exactly like main()'s
        # post-job time.sleep(90) cadence, all inside the 10-minute grace
        # window that triggered the live bug.
        for i in range(10):
            simulated_now = start + timedelta(seconds=90 * i)
            with mock.patch.object(daily_scheduler, "_now_ist", return_value=simulated_now):
                already_ran = last_run_date.get(job_name) == simulated_now.date()
                next_time = daily_scheduler._next_run(
                    job_hour, job_minute, already_ran_today=already_ran,
                )
                is_due_now = next_time <= simulated_now
                if is_due_now and not already_ran:
                    run_count += 1
                    last_run_date[job_name] = simulated_now.date()

        self.assertEqual(run_count, 1)


if __name__ == "__main__":
    unittest.main()
