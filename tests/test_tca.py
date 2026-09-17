"""Tests for execution/tca.py's TransactionCostAnalyzer.

Section 18 audit (2026-09-16): no test file exercised this module's
correctness at all — only its constructor, via test_hypothesis_harness.py.
Found a real bug while building a cost-drift-vs-model comparison on top of
it: compute_summary()'s headline avg/median/percentile/per-symbol "cost"
stats aggregated `TCAResult.total_cost_bps` — but analyze_fill()'s own
comment says total_cost_bps "is a DIAGNOSTIC decomposition only ... and
must never be used as 'the cost'", because its three components use
different sign conventions (impact_bps is positive-means-FAVOURABLE, the
other two are positive-means-cost). A large favourable price move after a
fill inflates total_cost_bps by ADDING that favourable-signed number
rather than netting it against the real shortfall — `total_inr` a few
lines below in the same function already correctly uses
abs(implementation_shortfall_bps) instead; compute_summary() just never
matched it. Fixed to use the same convention.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from trading_platform.execution.tca import FillRecord, TransactionCostAnalyzer


def _fill(**overrides) -> FillRecord:
    defaults = dict(
        correlation_id="c1", symbol="RELIANCE", exchange="NSE", side="BUY",
        quantity=10, fill_price=100.0, fill_time=datetime.now(timezone.utc),
        arrival_price=100.0, benchmark_price=100.0, order_type="MARKET",
        urgency="IMMEDIATE", strategy="test_strategy", fill_ratio=1.0,
        time_to_fill_ms=0.0,
    )
    defaults.update(overrides)
    return FillRecord(**defaults)


class AnalyzeFillTests(unittest.TestCase):
    def test_implementation_shortfall_is_the_real_signed_cost_for_a_buy(self):
        tca = TransactionCostAnalyzer()
        # Paid 1% more than arrival on a BUY — a real, unambiguous cost.
        result = tca.analyze_fill(_fill(side="BUY", arrival_price=100.0, fill_price=101.0))

        self.assertAlmostEqual(result.implementation_shortfall_bps, 100.0, places=4)
        # total_cost_inr must reflect the real shortfall (₹1/share * 10 shares).
        self.assertAlmostEqual(result.total_cost_inr, 10.0, places=2)

    def test_a_large_favourable_price_move_after_fill_does_not_blow_up_total_cost_inr(self):
        """Even before this session's fix, total_cost_inr (used for the real
        rupee cost) was already correctly built on implementation_shortfall,
        not total_cost_bps — this locks that in."""
        tca = TransactionCostAnalyzer()
        # Real cost is a modest 100bps (paid 101 vs 100 arrival), but the
        # market then surged to 130 — a huge "favourable" impact reading.
        result = tca.analyze_fill(_fill(
            side="BUY", arrival_price=100.0, fill_price=101.0, benchmark_price=130.0,
        ))

        self.assertAlmostEqual(result.implementation_shortfall_bps, 100.0, places=4)
        self.assertAlmostEqual(result.total_cost_inr, 10.0, places=2)  # 100bps * 100 arrival * 10 qty / 10000


class ComputeSummaryTests(unittest.TestCase):
    def test_headline_cost_reflects_real_shortfall_not_the_diagnostic_decomposition(self):
        """Regression: before this fix, a fill with a large favourable
        impact reading inflated avg_cost_bps far past the real ~100bps
        shortfall (the diagnostic total_cost_bps summed to ~4089bps for
        this exact scenario — a fill that really cost about 1%, reported
        as an ~41% cost)."""
        tca = TransactionCostAnalyzer()
        tca.analyze_fill(_fill(
            correlation_id="c1", side="BUY", arrival_price=100.0, fill_price=101.0,
            benchmark_price=130.0, order_type="MARKET",
        ))

        summary = tca.compute_summary()

        self.assertEqual(summary.total_fills, 1)
        self.assertAlmostEqual(summary.avg_cost_bps, 100.0, places=4)
        self.assertLess(summary.avg_cost_bps, 500.0)  # sanity: nowhere near the old ~4089bps

    def test_per_symbol_breakdown_uses_the_same_corrected_metric(self):
        tca = TransactionCostAnalyzer()
        tca.analyze_fill(_fill(
            correlation_id="c1", symbol="RELIANCE", side="BUY",
            arrival_price=100.0, fill_price=101.0, benchmark_price=130.0,
        ))

        summary = tca.compute_summary()

        self.assertAlmostEqual(summary.per_symbol["RELIANCE"]["avg_cost_bps"], 100.0, places=4)

    def test_empty_period_reports_zero_not_a_crash(self):
        tca = TransactionCostAnalyzer()
        summary = tca.compute_summary()
        self.assertEqual(summary.total_fills, 0)
        self.assertEqual(summary.avg_cost_bps, 0.0)


class StrategyAndOrderTypeAttributionTests(unittest.TestCase):
    """Section 9 audit (2026-09-17): FillRecord already carried strategy/
    order_type on every real fill, but analyze_fill() dropped both when
    building TCAResult — the single root cause behind three separate-
    looking bugs: the strategies= filter had nothing to filter on,
    per_strategy stayed permanently empty, and market_order_pct/
    limit_order_pct were hardcoded 0.0."""

    def test_analyze_fill_preserves_strategy_and_order_type(self):
        tca = TransactionCostAnalyzer()
        result = tca.analyze_fill(_fill(strategy="short_vol_condor", order_type="LIMIT"))
        self.assertEqual(result.strategy, "short_vol_condor")
        self.assertEqual(result.order_type, "LIMIT")

    def test_strategies_filter_actually_filters(self):
        tca = TransactionCostAnalyzer()
        tca.analyze_fill(_fill(correlation_id="c1", strategy="short_vol_condor"))
        tca.analyze_fill(_fill(correlation_id="c2", strategy="pair_hedge"))

        summary = tca.compute_summary(strategies=["short_vol_condor"])

        self.assertEqual(summary.total_fills, 1)

    def test_per_strategy_breakdown_is_populated(self):
        tca = TransactionCostAnalyzer()
        tca.analyze_fill(_fill(
            correlation_id="c1", strategy="short_vol_condor",
            arrival_price=100.0, fill_price=101.0,
        ))
        tca.analyze_fill(_fill(
            correlation_id="c2", strategy="pair_hedge",
            arrival_price=200.0, fill_price=198.0, side="SELL",
        ))

        summary = tca.compute_summary()

        self.assertEqual(set(summary.per_strategy.keys()), {"short_vol_condor", "pair_hedge"})
        self.assertEqual(summary.per_strategy["short_vol_condor"]["fills"], 1)
        self.assertAlmostEqual(summary.per_strategy["short_vol_condor"]["avg_cost_bps"], 100.0, places=4)

    def test_market_and_limit_order_pct_reflect_real_fills(self):
        tca = TransactionCostAnalyzer()
        tca.analyze_fill(_fill(correlation_id="c1", order_type="MARKET"))
        tca.analyze_fill(_fill(correlation_id="c2", order_type="MARKET"))
        tca.analyze_fill(_fill(correlation_id="c3", order_type="LIMIT"))
        tca.analyze_fill(_fill(correlation_id="c4", order_type="LIMIT"))

        summary = tca.compute_summary()

        self.assertAlmostEqual(summary.market_order_pct, 0.5)
        self.assertAlmostEqual(summary.limit_order_pct, 0.5)


if __name__ == "__main__":
    unittest.main()
