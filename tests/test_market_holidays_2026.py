"""Regression tests for audit_results/2026-09-13/AUDIT_REPORT.md finding #8
(high): the 2026 NSE holiday calendar was wrong on multiple dates and
missing several holidays — most urgently, 14 September 2026 (Ganesh
Chaturthi, the day right after this correction) was classified as an
ordinary trading day.

Dates cross-verified 2026-09-13 against two independent sources
(groww.in/p/nse-holidays, smallcase.com/learn/nse-holidays-2026) that agree
exactly; re-verify against NSE's own circular
(nsearchives.nseindia.com/content/circulars/FAOP71777.pdf) when reachable.
"""
from __future__ import annotations

import unittest
from datetime import date

from trading_platform.agent.market_hours import is_trading_day


class Nse2026HolidayCalendarTests(unittest.TestCase):
    def test_ganesh_chaturthi_sept_14_is_not_a_trading_day(self):
        # The audit's single most urgent finding: this app runs unattended
        # and would have scanned/traded on a real exchange holiday the very
        # next day after this fix.
        self.assertFalse(is_trading_day(date(2026, 9, 14)))

    def test_holi_is_march_3_not_march_25(self):
        self.assertFalse(is_trading_day(date(2026, 3, 3)))
        # March 25 2026 is a real trading day (Wednesday) that the old,
        # wrong entry incorrectly closed the market for.
        self.assertTrue(is_trading_day(date(2026, 3, 25)))

    def test_diwali_observed_closure_is_november_10_not_november_4(self):
        # "Diwali-Balipratipada" — the actual observed exchange closure.
        self.assertFalse(is_trading_day(date(2026, 11, 10)))
        # November 4 2026 (Wednesday) is a real trading day; the old entry
        # both misnamed and mis-dated this holiday.
        self.assertTrue(is_trading_day(date(2026, 11, 4)))

    def test_november_8_muhurat_session_is_a_sunday_not_a_weekday_closure(self):
        # A special Sunday Muhurat trading session for Laxmi Pujan, not a
        # regular weekday holiday — must not be (and isn't) in _HOLIDAYS;
        # it's already excluded as a weekend by is_trading_day's own
        # weekday check, independent of the holiday set.
        self.assertEqual(date(2026, 11, 8).weekday(), 6)  # Sunday
        self.assertFalse(is_trading_day(date(2026, 11, 8)))

    def test_previously_missing_holidays_are_now_recognized(self):
        for day, name in [
            (date(2026, 3, 26), "Ram Navami"),
            (date(2026, 3, 31), "Mahavir Jayanti"),
            (date(2026, 5, 28), "Bakri Id"),
            (date(2026, 6, 26), "Muharram"),
            (date(2026, 10, 20), "Dussehra"),
            (date(2026, 11, 24), "Guru Nanak Jayanti"),
        ]:
            self.assertFalse(is_trading_day(day), f"{name} ({day}) should be a holiday")

    def test_unchanged_holidays_are_still_correct(self):
        for day in [
            date(2026, 1, 26),   # Republic Day
            date(2026, 4, 3),    # Good Friday
            date(2026, 4, 14),   # Ambedkar Jayanti
            date(2026, 5, 1),    # Maharashtra Day
            date(2026, 10, 2),   # Gandhi Jayanti
            date(2026, 12, 25),  # Christmas
        ]:
            self.assertFalse(is_trading_day(day))

    def test_independence_day_falls_on_a_saturday_already_excluded(self):
        self.assertEqual(date(2026, 8, 15).weekday(), 5)  # Saturday
        self.assertFalse(is_trading_day(date(2026, 8, 15)))


if __name__ == "__main__":
    unittest.main()
