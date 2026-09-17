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


if __name__ == "__main__":
    unittest.main()
