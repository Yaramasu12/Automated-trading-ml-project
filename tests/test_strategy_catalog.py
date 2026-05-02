from __future__ import annotations

import unittest

from trading_platform.strategies.factory import StrategyFactory


class StrategyCatalogTests(unittest.TestCase):
    def test_catalog_contains_required_strategy_families(self):
        catalog = StrategyFactory().catalog()
        names = {strategy["name"] for strategy in catalog}
        families = {strategy["family"] for strategy in catalog}

        self.assertIn("futures_trend", names)
        self.assertIn("iron_condor", names)
        self.assertIn("equity_momentum", names)
        self.assertIn("options", families)
        self.assertIn("futures", families)
        self.assertIn("equity", families)

    def test_catalog_exposes_exit_and_expiry_rules(self):
        strategy = next(item for item in StrategyFactory().catalog() if item["name"] == "expiry_rollover")

        self.assertTrue(strategy["supports_rollover"])
        self.assertIn("stop_loss_pct", strategy["exit_rules"])
        self.assertTrue(strategy["expiry_rules"]["allow_rollover"])


if __name__ == "__main__":
    unittest.main()
