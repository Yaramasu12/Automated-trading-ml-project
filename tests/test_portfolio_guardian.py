"""Unit tests for the always-on Portfolio Guardian safety net, added
2026-08-05 after a short-vol condor breached its stop-loss by over 3x with
nothing watching for hours (see runtime.py's
_periodic_portfolio_guardian_loop docstring for the full incident)."""
from __future__ import annotations

import unittest

from trading_platform.api.runtime import TradingRuntime


class PortfolioGuardianTests(unittest.TestCase):
    def _runtime(self) -> TradingRuntime:
        rt = TradingRuntime()
        rt.portfolio.positions.clear()
        rt.kill_switch_active = False
        return rt

    def test_no_breach_does_not_set_kill_switch(self):
        rt = self._runtime()
        rt.portfolio.cash = 100_000.0
        rt.portfolio.peak_equity = 100_000.0
        rt._run_portfolio_guardian_tick()
        self.assertFalse(rt.kill_switch_active)

    def test_drawdown_breach_sets_kill_switch(self):
        rt = self._runtime()
        rt.portfolio.peak_equity = 100_000.0
        rt.portfolio.cash = 100_000.0 * (1 - rt.capital_protection.drawdown_halt_pct - 0.01)
        rt._run_portfolio_guardian_tick()
        self.assertTrue(rt.kill_switch_active)

    def test_daily_loss_breach_sets_kill_switch(self):
        rt = self._runtime()
        rt.portfolio.peak_equity = 1_000_000.0
        rt.portfolio.cash = 1_000_000.0 * (1 - rt.capital_protection.daily_loss_limit_pct - 0.005)
        rt.scheduler._session_start_equity = 1_000_000.0
        rt._run_portfolio_guardian_tick()
        self.assertTrue(rt.kill_switch_active)

    def test_already_tripped_kill_switch_short_circuits(self):
        rt = self._runtime()
        rt.kill_switch_active = True
        rt.portfolio.cash = 1.0
        rt.portfolio.peak_equity = 1_000_000.0
        rt._run_portfolio_guardian_tick()   # must not raise; early return
        self.assertTrue(rt.kill_switch_active)

    def test_healthy_portfolio_across_repeated_ticks_stays_clear(self):
        rt = self._runtime()
        rt.portfolio.cash = 100_000.0
        rt.portfolio.peak_equity = 100_000.0
        for _ in range(5):
            rt._run_portfolio_guardian_tick()
        self.assertFalse(rt.kill_switch_active)


if __name__ == "__main__":
    unittest.main()
