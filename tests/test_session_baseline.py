"""Regression tests for audit_results/2026-09-13/AUDIT_REPORT.md finding #7
(high): the daily-loss circuit breaker's session-start baseline was
recomputed from cost-basis equity on every process start, including a
mid-session restart, silently discarding any already-existing unrealized
P&L on positions carried from a prior session and resetting the loss
budget on every restart.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from trading_platform.api.price_service import PriceResolution
from trading_platform.api.runtime import TradingRuntime
from trading_platform.data.persistence import TradingDatabase
from trading_platform.domain.enums import OrderStatus
from trading_platform.domain.models import Order


class SessionBaselineTests(unittest.TestCase):
    def _seed_position(self, runtime: TradingRuntime, symbol: str = "RELIANCE",
                       quantity: int = 10, entry_price: float = 100.0) -> None:
        entry = runtime._intent_from_payload({
            "symbol": symbol, "side": "BUY", "quantity": quantity, "price": entry_price,
            "strategy_name": "test_seed", "metadata": {},
        })
        runtime.portfolio.apply_fill(Order(entry, status=OrderStatus.FILLED), entry_price,
                                     datetime.now(timezone.utc), 0.0)

    def test_marked_equity_uses_resolved_price_not_cost_basis(self):
        runtime = TradingRuntime()
        self._seed_position(runtime, "RELIANCE", 10, 100.0)
        cost_basis_equity = runtime.portfolio.equity

        with patch.object(
            runtime.price_service, "resolve",
            return_value=PriceResolution(price=150.0, source="live", is_stale=False),
        ):
            marked = runtime._marked_equity_now()

        # A +50/share unrealized gain on 10 shares must show up in the marked
        # value but is invisible to cost-basis equity.
        self.assertAlmostEqual(marked - cost_basis_equity, 500.0, places=2)

    def test_premarket_force_fresh_persists_a_new_baseline(self):
        runtime = TradingRuntime()
        self._seed_position(runtime, "RELIANCE", 10, 100.0)
        today = date(2026, 9, 13)

        with patch("trading_platform.api.runtime.now_ist") as mock_now_ist, \
             patch.object(runtime.price_service, "resolve",
                          return_value=PriceResolution(price=120.0, source="live", is_stale=False)):
            mock_now_ist.return_value = datetime(2026, 9, 13, 9, 0)
            runtime._establish_session_baseline(force_fresh=True)

        persisted = runtime.db.get_session_baseline(today)
        self.assertIsNotNone(persisted)
        self.assertAlmostEqual(runtime.scheduler._session_start_equity, persisted, places=2)

    def test_restart_restores_persisted_baseline_instead_of_recomputing(self):
        # The core regression: this morning's premarket routine captured a
        # baseline reflecting the position's mark at that time. Later the
        # SAME calendar day, the price moves further and the process
        # restarts (a redeploy, a crash) -- the restart must NOT silently
        # recompute a fresh (and now different, cost-basis) baseline; it
        # must restore exactly what premarket already captured.
        runtime = TradingRuntime()
        self._seed_position(runtime, "RELIANCE", 10, 100.0)

        with patch("trading_platform.api.runtime.now_ist") as mock_now_ist, \
             patch.object(runtime.price_service, "resolve",
                          return_value=PriceResolution(price=120.0, source="live", is_stale=False)):
            mock_now_ist.return_value = datetime(2026, 9, 13, 9, 0)
            runtime._establish_session_baseline(force_fresh=True)  # premarket, this morning

        baseline_this_morning = runtime.scheduler._session_start_equity

        # Simulate a mid-day restart: session_start_equity is wiped (as it
        # would be on a fresh process), and the price has moved further.
        runtime.scheduler._session_start_equity = 0.0
        with patch("trading_platform.api.runtime.now_ist") as mock_now_ist, \
             patch.object(runtime.price_service, "resolve",
                          return_value=PriceResolution(price=90.0, source="live", is_stale=False)):
            mock_now_ist.return_value = datetime(2026, 9, 13, 13, 0)  # same day, later
            runtime._establish_session_baseline(force_fresh=False)  # process startup

        self.assertAlmostEqual(runtime.scheduler._session_start_equity, baseline_this_morning, places=2)

    def test_cold_boot_with_no_persisted_baseline_computes_fresh(self):
        # No premarket routine has run yet today (e.g. a boot before 08:55
        # IST) -- force_fresh=False with nothing persisted must still
        # produce a real (marked, not stale-zero) baseline rather than
        # leaving it at 0.
        runtime = TradingRuntime()
        self._seed_position(runtime, "RELIANCE", 10, 100.0)

        with patch("trading_platform.api.runtime.now_ist") as mock_now_ist, \
             patch.object(runtime.price_service, "resolve",
                          return_value=PriceResolution(price=110.0, source="live", is_stale=False)):
            mock_now_ist.return_value = datetime(2026, 9, 13, 8, 0)
            runtime._establish_session_baseline(force_fresh=False)

        self.assertGreater(runtime.scheduler._session_start_equity, 0.0)
        self.assertIsNotNone(runtime.db.get_session_baseline(date(2026, 9, 13)))

    def test_baseline_persistence_round_trip_on_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = TradingDatabase(Path(tmp) / "trading.db")
            try:
                self.assertIsNone(db.get_session_baseline(date(2026, 9, 13)))
                db.save_session_baseline(date(2026, 9, 13), 987654.32)
                self.assertAlmostEqual(db.get_session_baseline(date(2026, 9, 13)), 987654.32, places=2)
                # A different date must not collide.
                self.assertIsNone(db.get_session_baseline(date(2026, 9, 14)))
                # Re-saving the same date overwrites, not duplicates.
                db.save_session_baseline(date(2026, 9, 13), 111111.11)
                self.assertAlmostEqual(db.get_session_baseline(date(2026, 9, 13)), 111111.11, places=2)
            finally:
                db.close()


class GoalGovernanceBaselineTests(unittest.TestCase):
    """2026-09-19, user-directed: GoalGovernance's Jan-1 default anchor meant
    a year-to-date loss alone triggered its AT_RISK 40% size cut regardless
    of annual_target_pct -- confirmed live after lowering the target from
    0.35 to 0.08 didn't unclamp sizing, because actual_run_rate was already
    negative against the Jan-1 baseline. This re-anchors to today's real
    equity instead, and (mirroring session_baselines' own established
    pattern) must persist that anchor so a restart restores it rather than
    silently re-seeding to a new "today" every time."""

    def _seed_position(self, runtime: TradingRuntime, symbol: str = "RELIANCE",
                       quantity: int = 10, entry_price: float = 100.0) -> None:
        entry = runtime._intent_from_payload({
            "symbol": symbol, "side": "BUY", "quantity": quantity, "price": entry_price,
            "strategy_name": "test_seed", "metadata": {},
        })
        runtime.portfolio.apply_fill(Order(entry, status=OrderStatus.FILLED), entry_price,
                                     datetime.now(timezone.utc), 0.0)

    def test_baseline_persistence_round_trip_on_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = TradingDatabase(Path(tmp) / "trading.db")
            try:
                self.assertIsNone(db.get_goal_governance_baseline())
                db.save_goal_governance_baseline(date(2026, 9, 19), 956_620.77)
                stored = db.get_goal_governance_baseline()
                self.assertEqual(stored["start_date"], date(2026, 9, 19))
                self.assertAlmostEqual(stored["start_capital"], 956_620.77, places=2)
                # Re-saving overwrites the singleton row, not a new one.
                db.save_goal_governance_baseline(date(2026, 9, 20), 1_000_000.0)
                stored = db.get_goal_governance_baseline()
                self.assertEqual(stored["start_date"], date(2026, 9, 20))
                self.assertAlmostEqual(stored["start_capital"], 1_000_000.0, places=2)
            finally:
                db.close()

    def test_first_run_seeds_a_new_baseline_from_todays_real_equity(self):
        runtime = TradingRuntime()
        # A year-to-date loss (equity below the class default start_capital)
        # -- exactly the scenario that motivated this fix.
        self._seed_position(runtime, "RELIANCE", 10, 100.0)
        with patch.object(
            runtime.price_service, "resolve",
            return_value=PriceResolution(price=80.0, source="live", is_stale=False),
        ), patch("trading_platform.api.runtime.now_ist") as mock_now_ist:
            mock_now_ist.return_value = datetime(2026, 9, 19, 7, 0)
            marked_equity = runtime._marked_equity_now()
            runtime._restore_or_seed_goal_governance_baseline()

        self.assertEqual(runtime.goal_governance.start_date, date(2026, 9, 19))
        self.assertAlmostEqual(runtime.goal_governance.start_capital, marked_equity, places=2)
        # And it must be persisted, not just applied in-memory.
        persisted = runtime.db.get_goal_governance_baseline()
        self.assertIsNotNone(persisted)
        self.assertAlmostEqual(persisted["start_capital"], marked_equity, places=2)

    def test_restart_restores_persisted_baseline_instead_of_reseeding(self):
        runtime = TradingRuntime()
        self._seed_position(runtime, "RELIANCE", 10, 100.0)
        with patch.object(
            runtime.price_service, "resolve",
            return_value=PriceResolution(price=80.0, source="live", is_stale=False),
        ), patch("trading_platform.api.runtime.now_ist") as mock_now_ist:
            mock_now_ist.return_value = datetime(2026, 9, 19, 7, 0)
            runtime._restore_or_seed_goal_governance_baseline()
        anchor_capital = runtime.goal_governance.start_capital
        anchor_date = runtime.goal_governance.start_date

        # Simulate a restart: in-memory GoalGovernance reverts to its
        # constructor default (as a fresh process would build), the price
        # has moved further, and a day has passed.
        runtime.goal_governance.reset_baseline(start_capital=1_000_000.0, start_date=date(2026, 1, 1))
        with patch.object(
            runtime.price_service, "resolve",
            return_value=PriceResolution(price=60.0, source="live", is_stale=False),
        ), patch("trading_platform.api.runtime.now_ist") as mock_now_ist:
            mock_now_ist.return_value = datetime(2026, 9, 20, 7, 0)
            runtime._restore_or_seed_goal_governance_baseline()

        self.assertEqual(runtime.goal_governance.start_date, anchor_date)
        self.assertAlmostEqual(runtime.goal_governance.start_capital, anchor_capital, places=2)

    def test_explicit_reset_reanchors_and_persists(self):
        runtime = TradingRuntime()
        self._seed_position(runtime, "RELIANCE", 10, 100.0)
        with patch.object(
            runtime.price_service, "resolve",
            return_value=PriceResolution(price=150.0, source="live", is_stale=False),
        ), patch("trading_platform.api.runtime.now_ist") as mock_now_ist:
            mock_now_ist.return_value = datetime(2026, 9, 19, 7, 0)
            result = runtime.reset_goal_governance_baseline()

        self.assertEqual(result["start_date"], "2026-09-19")
        self.assertGreater(result["start_capital"], 1_000_000.0)  # +50/share unrealized gain
        self.assertEqual(runtime.goal_governance.start_date, date(2026, 9, 19))
        persisted = runtime.db.get_goal_governance_baseline()
        self.assertAlmostEqual(persisted["start_capital"], result["start_capital"], places=2)


class IndexVolPremiumPersistenceTests(unittest.TestCase):
    """Persistence layer for the premium monitor (research/index_premium.py,
    daily_scheduler.py's premium_monitor_update job). REPORTING ONLY -- these
    tests cover storage/retrieval, not any order-submission effect (there is
    none: nothing reads this table on the money path)."""

    def test_round_trip_and_overwrite_is_keyed_by_date_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = TradingDatabase(Path(tmp) / "trading.db")
            try:
                db.save_index_premium(date(2026, 9, 18), "NIFTY", 23350.0, 10.5, None, None, None)
                db.save_index_premium(date(2026, 9, 19), "NIFTY", 23400.0, 10.8, 8.5, 2.3, None)
                db.save_index_premium(date(2026, 9, 19), "BANKNIFTY", 56000.0, 12.0, 13.0, -1.0, -0.4)

                recent = db.recent_index_premium("NIFTY", limit=90)
                self.assertEqual([r["trade_date"] for r in recent], [date(2026, 9, 18), date(2026, 9, 19)])
                self.assertIsNone(recent[0]["premium"])
                self.assertAlmostEqual(recent[1]["premium"], 2.3, places=4)

                # A different index's rows must not leak into NIFTY's query.
                self.assertTrue(all(r for r in recent))
                nifty_only = db.recent_index_premium("NIFTY", limit=90)
                self.assertTrue(all("BANKNIFTY" not in str(r) for r in nifty_only))

                # Re-saving the same (date, index) overwrites, not duplicates.
                db.save_index_premium(date(2026, 9, 19), "NIFTY", 23400.0, 10.8, 8.5, 2.3, 1.9)
                recent2 = db.recent_index_premium("NIFTY", limit=90)
                self.assertEqual(len(recent2), 2)
                self.assertAlmostEqual(recent2[1]["trailing_median_60d"], 1.9, places=4)
            finally:
                db.close()

    def test_latest_index_premium_returns_one_row_per_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = TradingDatabase(Path(tmp) / "trading.db")
            try:
                db.save_index_premium(date(2026, 9, 17), "NIFTY", 23300.0, 10.2, None, None, None)
                db.save_index_premium(date(2026, 9, 18), "NIFTY", 23350.0, 10.5, None, None, None)
                db.save_index_premium(date(2026, 9, 18), "FINNIFTY", 25400.0, 19.0, None, None, None)

                latest = {r["index_symbol"]: r for r in db.latest_index_premium()}
                self.assertEqual(set(latest), {"NIFTY", "FINNIFTY"})
                self.assertEqual(latest["NIFTY"]["trade_date"], date(2026, 9, 18))
                self.assertAlmostEqual(latest["NIFTY"]["spot"], 23350.0, places=2)
            finally:
                db.close()


class PremiumMonitorStatusTests(unittest.TestCase):
    """runtime.premium_monitor_status() -- the GET /research/premium-status
    backend. Pre-committed thetas from audit_results/2026-09-19/
    premium_regime_filter.md: NIFTY>=2, BANKNIFTY>=1, FINNIFTY>=4;
    MIDCPNIFTY has no validated strategy at all."""

    def test_signal_reflects_the_walk_forward_thetas(self):
        runtime = TradingRuntime()
        runtime.db.save_index_premium(date(2026, 9, 19), "NIFTY", 23400.0, 10.8, 8.5, 2.3, 2.5)   # >= 2.0
        runtime.db.save_index_premium(date(2026, 9, 19), "BANKNIFTY", 56000.0, 12.0, 13.0, -1.0, 0.4)  # < 1.0
        runtime.db.save_index_premium(date(2026, 9, 19), "MIDCPNIFTY", 14500.0, 14.3, 12.0, 2.3, 3.0)

        status = runtime.premium_monitor_status()
        by_index = {r["index"]: r for r in status["indices"]}
        self.assertEqual(by_index["NIFTY"]["signal"], "premium rich (trade)")
        self.assertEqual(by_index["BANKNIFTY"]["signal"], "premium thin (stand aside)")
        self.assertEqual(by_index["MIDCPNIFTY"]["signal"], "no validated strategy")
        self.assertIn("does not gate", status["note"])

    def test_missing_trailing_median_reports_building_history_not_a_crash(self):
        runtime = TradingRuntime()
        runtime.db.save_index_premium(date(2026, 9, 19), "NIFTY", 23400.0, 10.8, None, None, None)
        status = runtime.premium_monitor_status()
        self.assertEqual(status["indices"][0]["signal"], "building history")

    def test_no_data_yet_returns_empty_not_an_error(self):
        runtime = TradingRuntime()
        status = runtime.premium_monitor_status()
        self.assertEqual(status["indices"], [])


if __name__ == "__main__":
    unittest.main()
