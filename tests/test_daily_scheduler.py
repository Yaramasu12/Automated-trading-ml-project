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


if __name__ == "__main__":
    unittest.main()
