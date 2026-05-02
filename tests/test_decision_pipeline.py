from __future__ import annotations

import unittest
from datetime import date

from trading_platform.data.instrument_master import build_default_universe
from trading_platform.decision.pipeline import DecisionPipeline
from trading_platform.domain.enums import ExecutionMode
from trading_platform.portfolio.ledger import PortfolioLedger
from trading_platform.risk.engine import RiskEngine
from trading_platform.strategies.factory import StrategyFactory


class DecisionPipelineTests(unittest.TestCase):
    def test_signal_scan_builds_features_regime_and_candidates(self):
        master = build_default_universe(date(2026, 1, 1))
        pipeline = DecisionPipeline(master, StrategyFactory(), RiskEngine(), PortfolioLedger(1_000_000))

        result = pipeline.scan(
            underlying="NIFTY",
            start=date(2026, 1, 1),
            days=30,
            execution_mode=ExecutionMode.BACKTEST,
            live_armed=False,
            kill_switch_active=False,
            strategy_names=["futures_trend", "defined_risk_option_spread"],
        )
        payload = result.to_dict()

        self.assertEqual(payload["underlying"], "NIFTY")
        self.assertIn("regime", payload)
        self.assertEqual(len(payload["candidates"]), 2)
        self.assertEqual(payload["submitted_orders"] if "submitted_orders" in payload else 0, 0)

    def test_live_scan_rejects_unarmed_candidates(self):
        master = build_default_universe(date(2026, 1, 1))
        pipeline = DecisionPipeline(master, StrategyFactory(), RiskEngine(), PortfolioLedger(1_000_000))

        result = pipeline.scan(
            underlying="RELIANCE",
            start=date(2026, 1, 1),
            days=30,
            execution_mode=ExecutionMode.LIVE,
            live_armed=False,
            kill_switch_active=False,
            strategy_names=["equity_momentum"],
        )
        candidates = result.to_dict()["candidates"]
        risk_reasons = [
            candidate["risk_decision"]["reason"]
            for candidate in candidates
            if candidate["risk_decision"] is not None
        ]

        self.assertIn("live_mode_not_armed", risk_reasons)


if __name__ == "__main__":
    unittest.main()
