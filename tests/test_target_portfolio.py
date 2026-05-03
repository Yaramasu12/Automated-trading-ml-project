from __future__ import annotations

import unittest
from datetime import date

from trading_platform.portfolio.target import AnnualTargetTracker


class TargetPortfolioTests(unittest.TestCase):
    def test_target_tracker_scales_only_when_quality_is_high(self):
        progress = AnnualTargetTracker(annual_target=50_000_000).evaluate(
            start_date=date(2026, 1, 1),
            as_of=date(2026, 2, 1),
            start_capital=1_000_000,
            current_equity=1_200_000,
            drawdown=0.02,
            profit_factor=1.4,
            sharpe=1.2,
        )

        self.assertEqual(progress.allocation_bias, "selective_scale")
        self.assertGreater(progress.required_run_rate, 0)

    def test_target_tracker_reduces_risk_during_drawdown(self):
        progress = AnnualTargetTracker().evaluate(
            start_date=date(2026, 1, 1),
            as_of=date(2026, 3, 1),
            start_capital=1_000_000,
            current_equity=950_000,
            drawdown=0.07,
            profit_factor=1.5,
            sharpe=1.3,
        )

        self.assertEqual(progress.allocation_bias, "reduce_risk")
        self.assertLess(progress.max_scaling_multiplier, 1.0)


if __name__ == "__main__":
    unittest.main()
