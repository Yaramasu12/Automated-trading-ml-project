from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from trading_platform.data.instrument_master import build_default_universe
from trading_platform.domain.enums import ExecutionMode, OptionType, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal
from trading_platform.portfolio.ledger import PortfolioSnapshot
from trading_platform.risk.engine import RiskEngine, RiskLimits


class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.master = build_default_universe(date(2026, 1, 5))
        self.snapshot = PortfolioSnapshot(
            cash=1_000_000,
            equity=1_000_000,
            realized_pnl=0,
            unrealized_pnl=0,
            drawdown=0.0,
            peak_equity=1_000_000,
            open_positions=0,
        )

    def test_live_orders_require_arming(self):
        instrument = self.master.get("RELIANCE")
        intent = self._intent(instrument, Side.BUY, 1, 2800)

        decision = RiskEngine().evaluate(
            intent,
            self.snapshot,
            datetime(2026, 1, 5, 10, 0),
            ExecutionMode.LIVE,
            live_armed=False,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "live_mode_not_armed")

    def test_blocks_naked_option_selling(self):
        option = self.master.select_option("NIFTY", date(2026, 1, 5), 22500, OptionType.CE)
        intent = self._intent(option, Side.SELL, 1, 120)

        decision = RiskEngine().evaluate(
            intent,
            self.snapshot,
            datetime(2026, 1, 5, 10, 0),
            ExecutionMode.BACKTEST,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "naked_option_selling_blocked")

    def test_rejects_drawdown_breach(self):
        instrument = self.master.get("TCS")
        intent = self._intent(instrument, Side.BUY, 1, 3500)
        snapshot = PortfolioSnapshot(900_000, 900_000, 0, 0, 0.11, 1_000_000, 0)

        decision = RiskEngine(RiskLimits(max_drawdown=0.10)).evaluate(
            intent,
            snapshot,
            datetime(2026, 1, 5, 10, 0),
            ExecutionMode.BACKTEST,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "max_drawdown_breached")

    def test_rejects_strategy_loss_breach(self):
        instrument = self.master.get("RELIANCE")
        intent = self._intent(instrument, Side.BUY, 1, 2800)

        decision = RiskEngine(RiskLimits(max_loss_per_strategy_pct=0.01)).evaluate(
            intent,
            self.snapshot,
            datetime(2026, 1, 5, 10, 0),
            ExecutionMode.BACKTEST,
            strategy_daily_pnl=-11_000,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "max_strategy_loss_breached")

    def test_rejects_near_expiry_gamma_breach(self):
        option = self.master.select_option("NIFTY", date(2026, 1, 8), 22500, OptionType.CE)
        intent = self._intent(option, Side.BUY, 1, 120)

        decision = RiskEngine(RiskLimits(max_gamma_near_expiry=0.02)).evaluate(
            intent,
            self.snapshot,
            datetime(2026, 1, 8, 10, 0),
            ExecutionMode.BACKTEST,
            gamma_exposure=0.03,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "near_expiry_gamma_exceeds_limit")

    def test_rejects_correlated_exposure_breach(self):
        instrument = self.master.get("RELIANCE")
        intent = self._intent(instrument, Side.BUY, 1, 2800)

        decision = RiskEngine(RiskLimits(max_correlated_exposure_pct=0.20)).evaluate(
            intent,
            self.snapshot,
            datetime(2026, 1, 5, 10, 0),
            ExecutionMode.BACKTEST,
            correlated_exposure_pct=0.22,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "correlated_exposure_exceeds_limit")

    def _intent(self, instrument, side, quantity, price):
        signal = Signal("test", instrument.symbol, side, 0.9, price, "unit test", datetime.now(timezone.utc))
        return OrderIntent(signal, instrument, quantity, OrderType.MARKET, ProductType.INTRADAY)


class CapitalProtectionMarginTests(unittest.TestCase):
    """A held futures position drives paper CASH negative (full notional deducted
    on entry) while EQUITY stays intact. The margin check must measure against
    equity, else every subsequent order — incl. the short-vol condor — is blocked."""

    def setUp(self):
        from trading_platform.risk.capital_protection import CapitalProtection
        self.master = build_default_universe(date(2026, 1, 5))
        self.cap = CapitalProtection()

    def _intent(self, instrument, side, quantity, price):
        signal = Signal("test", instrument.symbol, side, 0.9, price, "unit test", datetime.now(timezone.utc))
        return OrderIntent(signal, instrument, quantity, OrderType.MARKET, ProductType.INTRADAY)

    def test_order_approved_when_cash_negative_but_equity_solvent(self):
        # Reproduces the live bug: cash = -596,964 (a futures notional deducted),
        # equity still ~1,000,000. A small new order must NOT be rejected.
        snap = PortfolioSnapshot(
            cash=-596_964, equity=999_900, realized_pnl=0, unrealized_pnl=0,
            drawdown=0.0, peak_equity=1_000_000, open_positions=1,
        )
        intent = self._intent(self.master.get("RELIANCE"), Side.BUY, 1, 2800)
        result = self.cap.check(intent, snap, daily_pnl=0.0)
        self.assertTrue(result.approved, result.reason)
        self.assertNotIn("Insufficient margin", result.reason)

    def test_still_rejects_when_equity_truly_insufficient(self):
        # Genuinely broke account (equity ~0) must still be rejected.
        snap = PortfolioSnapshot(
            cash=50, equity=50, realized_pnl=0, unrealized_pnl=0,
            drawdown=0.0, peak_equity=1_000_000, open_positions=0,
        )
        intent = self._intent(self.master.get("RELIANCE"), Side.BUY, 1, 2800)
        result = self.cap.check(intent, snap, daily_pnl=0.0)
        self.assertFalse(result.approved)


if __name__ == "__main__":
    unittest.main()
