from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from trading_platform.broker.simulated import SimulatedBrokerClient
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.domain.enums import ExecutionMode, OrderStatus, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal
from trading_platform.execution.router import ExecutionRouter
from trading_platform.portfolio.ledger import PortfolioLedger
from trading_platform.risk.engine import RiskEngine


class ExecutionRouterTests(unittest.TestCase):
    def test_simulated_execution_updates_portfolio(self):
        master = build_default_universe(date(2026, 1, 5))
        instrument = master.get("RELIANCE")
        portfolio = PortfolioLedger(1_000_000)
        router = ExecutionRouter(
            broker=SimulatedBrokerClient(),
            risk_engine=RiskEngine(),
            portfolio=portfolio,
            execution_mode=ExecutionMode.PAPER,
        )
        signal = Signal("equity_momentum", "RELIANCE", Side.BUY, 0.9, 2800, "test", datetime.now(timezone.utc))
        intent = OrderIntent(signal, instrument, 5, OrderType.MARKET, ProductType.INTRADAY)

        report = router.submit(intent, datetime.now(timezone.utc), {"RELIANCE": 2800}, charges=10)

        self.assertEqual(report.order.status, OrderStatus.FILLED)
        self.assertIsNotNone(report.trade)
        self.assertIn("RELIANCE", portfolio.position_symbols())
        self.assertLess(portfolio.cash, 1_000_000)


if __name__ == "__main__":
    unittest.main()
