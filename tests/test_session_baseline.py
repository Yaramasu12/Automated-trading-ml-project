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


if __name__ == "__main__":
    unittest.main()
