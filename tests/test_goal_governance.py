"""Tests for goal/governance.py::GoalGovernance -- the object that actually
scales live short_vol position sizing (see short_vol_executor.py's
_goal_scaling_factor).
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from trading_platform.goal.governance import GoalGovernance, GoalPhase


class MidYearResetWindowTests(unittest.TestCase):
    """2026-09-19 regression: reset_baseline() lets an operator re-anchor
    start_date to any day, not just Jan 1 -- confirmed live that
    evaluate() pinned year_end to Dec 31 of start_date's OWN year, which
    compresses the tracking window for any reset after Jan 1 (a Sept 19
    reset left only ~102 days to Dec 31, not a real year). Since
    required_run_rate divides by days_remaining then annualizes by *365,
    a compressed window demanded a pace up to 365/102 ~= 3.6x harsher than
    the configured annual_target_pct, regardless of real performance."""

    def test_required_run_rate_on_day_one_equals_the_configured_target(self):
        """On the very first day after a reset (zero elapsed gain), the
        RATE required to hit the target over a real 365-day window must
        equal annual_target_pct exactly -- not something larger just
        because the reset landed mid-year."""
        gov = GoalGovernance(annual_target_pct=0.08, start_capital=1_000_000.0,
                              start_date=date(2026, 9, 19))
        state = gov.evaluate(current_equity=1_000_000.0, drawdown=0.0,
                              as_of=date(2026, 9, 20))  # one day elapsed
        self.assertAlmostEqual(state.required_run_rate, 0.08, places=2)

    def test_mid_year_reset_matches_jan_1_default_at_the_same_elapsed_day(self):
        """A Jan-1 anchor and a Sept-19 anchor, evaluated at EXACTLY the same
        number of elapsed days with the same equity, must compute the same
        required_run_rate -- proving the window is a genuine rolling year
        regardless of when the anchor was last set, not compressed by how
        much of the calendar year happened to be left."""
        jan1 = GoalGovernance(annual_target_pct=0.08, start_capital=1_000_000.0,
                               start_date=date(2026, 1, 1))
        sep19 = GoalGovernance(annual_target_pct=0.08, start_capital=1_000_000.0,
                                start_date=date(2026, 9, 19))

        elapsed = timedelta(days=30)
        jan1_state = jan1.evaluate(current_equity=1_030_000.0, drawdown=0.0,
                                    as_of=date(2026, 1, 1) + elapsed)
        sep19_state = sep19.evaluate(current_equity=1_030_000.0, drawdown=0.0,
                                      as_of=date(2026, 9, 19) + elapsed)

        self.assertAlmostEqual(jan1_state.required_run_rate, sep19_state.required_run_rate, places=6)
        self.assertAlmostEqual(jan1_state.actual_run_rate, sep19_state.actual_run_rate, places=6)

    def test_reset_baseline_updates_start_date_and_capital(self):
        gov = GoalGovernance(annual_target_pct=0.08, start_capital=1_000_000.0,
                              start_date=date(2026, 1, 1))
        gov.reset_baseline(start_capital=956_620.77, start_date=date(2026, 9, 19))
        self.assertEqual(gov.start_date, date(2026, 9, 19))
        self.assertAlmostEqual(gov.start_capital, 956_620.77, places=2)

    def test_day_one_after_reset_still_reports_at_risk_by_construction(self):
        """Honest limitation, not a new bug: comparing ANY positive required
        rate against a zero actual_run_rate on day one always trips
        AT_RISK -- true for the Jan-1 default too, not something the
        rolling-window fix changes. Documented here so it isn't mistaken
        for a regression later."""
        gov = GoalGovernance(annual_target_pct=0.08, start_capital=1_000_000.0,
                              start_date=date(2026, 9, 19))
        state = gov.evaluate(current_equity=1_000_000.0, drawdown=0.0,
                              as_of=date(2026, 9, 20))
        self.assertEqual(state.phase, GoalPhase.AT_RISK)


if __name__ == "__main__":
    unittest.main()
