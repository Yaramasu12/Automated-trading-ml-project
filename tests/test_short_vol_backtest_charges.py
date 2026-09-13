"""Regression tests for audit_results/2026-09-13/AUDIT_REPORT.md finding #3
(high, second half): short_vol_backtest.py's _charges() was handed a
condor's NET entry credit / net exit cost and treated the whole thing as
one sell-side turnover. Real STT is charged on the GROSS premium of each
SOLD leg individually — a 4-leg condor's true sell-side turnover is the sum
of the two short legs' own premiums, not (short premiums minus long wing
premiums). Fixed via _leg_turnovers(), which prices each leg separately and
keeps the two sides apart instead of netting them.
"""
from __future__ import annotations

import unittest

from trading_platform.backtesting.short_vol_backtest import ShortVolBacktester
from trading_platform.domain.enums import OptionType, Side
from trading_platform.strategies.short_vol import CondorLegSpec


class LegTurnoversTests(unittest.TestCase):
    def setUp(self):
        self.bt = ShortVolBacktester(underlying="NIFTY")

    def test_condor_splits_gross_premium_by_side_not_net(self):
        # A condor: 2 short legs (near the money, big premium), 2 long
        # wings (far OTM, small premium). Net credit would be small, but
        # gross sell-side turnover (what STT actually taxes) is large.
        legs = (
            CondorLegSpec(OptionType.CE, 24100, Side.SELL, is_wing=False),
            CondorLegSpec(OptionType.PE, 23900, Side.SELL, is_wing=False),
            CondorLegSpec(OptionType.CE, 24500, Side.BUY, is_wing=True),
            CondorLegSpec(OptionType.PE, 23500, Side.BUY, is_wing=True),
        )
        sell_gross, buy_gross = self.bt._leg_turnovers(legs, spot=24000.0, atm_iv=0.15, t_years=5 / 252)

        # Both SELL legs are near the money -> meaningfully priced; both BUY
        # legs are far OTM -> small but nonzero. Sell-side gross must be
        # comfortably larger than the far-wing buy-side gross, and NEITHER
        # should be zero (a net-credit bug could produce a near-zero
        # aggregate if the two roughly offset).
        self.assertGreater(sell_gross, 0)
        self.assertGreater(buy_gross, 0)
        self.assertGreater(sell_gross, buy_gross)

    def test_all_short_legs_have_zero_buy_side_gross(self):
        legs = (
            CondorLegSpec(OptionType.CE, 24100, Side.SELL, is_wing=False),
            CondorLegSpec(OptionType.PE, 23900, Side.SELL, is_wing=False),
        )
        sell_gross, buy_gross = self.bt._leg_turnovers(legs, spot=24000.0, atm_iv=0.15, t_years=5 / 252)
        self.assertGreater(sell_gross, 0)
        self.assertEqual(buy_gross, 0.0)

    def test_all_long_legs_have_zero_sell_side_gross(self):
        # A long_straddle/strangle-style all-BUY structure.
        legs = (
            CondorLegSpec(OptionType.CE, 24000, Side.BUY, is_wing=False),
            CondorLegSpec(OptionType.PE, 24000, Side.BUY, is_wing=False),
        )
        sell_gross, buy_gross = self.bt._leg_turnovers(legs, spot=24000.0, atm_iv=0.15, t_years=5 / 252)
        self.assertEqual(sell_gross, 0.0)
        self.assertGreater(buy_gross, 0)


class ChargesUsesGrossNotNetTests(unittest.TestCase):
    """The core regression: STT must scale with GROSS sell turnover, not a
    structure's net credit -- reproduced by comparing a case where net
    credit is deliberately small against one where it's large, at the
    SAME gross sell-side turnover."""

    def setUp(self):
        self.bt = ShortVolBacktester(underlying="NIFTY")

    def test_stt_component_matches_gross_sell_turnover_not_net(self):
        legs = (
            CondorLegSpec(OptionType.CE, 24100, Side.SELL, is_wing=False),
            CondorLegSpec(OptionType.PE, 23900, Side.SELL, is_wing=False),
            CondorLegSpec(OptionType.CE, 24500, Side.BUY, is_wing=True),
            CondorLegSpec(OptionType.PE, 23500, Side.BUY, is_wing=True),
        )
        sell_gross_pts, buy_gross_pts = self.bt._leg_turnovers(
            legs, spot=24000.0, atm_iv=0.15, t_years=5 / 252,
        )
        net_credit_pts = sell_gross_pts - buy_gross_pts
        qty = 1 * self.bt.lot_size

        charges_on_gross = self.bt._charges(legs, 1, sell_gross_pts, buy_gross_pts)
        # The bug: treating (sell_gross - buy_gross) as if it were the
        # entire sell-side turnover, with nothing on the buy side.
        charges_on_net_bug = self.bt._charges(legs, 1, net_credit_pts, 0.0)

        self.assertGreater(sell_gross_pts, net_credit_pts)  # the gap the bug hid
        self.assertGreater(charges_on_gross, charges_on_net_bug)

        # Replicate _charges' own formula directly (not by differencing two
        # calls, which also mixes in brokerage/exchange/GST/sebi scaling
        # with buy_turnover) to pin down the STT component precisely.
        sell_turnover = sell_gross_pts * qty
        buy_turnover = buy_gross_pts * qty
        expected_stt = 0.0015 * sell_turnover
        n_legs = len(legs)
        brokerage = min(20.0, 0.0003 * (sell_turnover + buy_turnover)) * n_legs * 2
        exch = 0.0005 * (sell_turnover + buy_turnover)
        gst = 0.18 * (brokerage + exch)
        sebi = 0.000001 * (sell_turnover + buy_turnover)
        stamp = 0.00003 * buy_turnover
        expected_total = brokerage + expected_stt + exch + gst + sebi + stamp
        self.assertAlmostEqual(charges_on_gross, expected_total, places=2)

    def test_round_trip_via_run_produces_nontrivial_charges_on_real_history(self):
        # End-to-end smoke test: the fixed code must not crash across a real
        # run and must produce charges of a sane order of magnitude (not
        # zero, not absurdly larger than the premium collected).
        import csv
        from pathlib import Path
        from trading_platform.backtesting.short_vol_backtest import load_daily_closes

        root = Path(__file__).resolve().parent.parent
        nifty_path = root / "data" / "historical" / "NIFTY__ONE_DAY_deep.csv"
        vix_path = root / "data" / "historical" / "INDIAVIX__ONE_DAY_deep.csv"
        if not nifty_path.exists() or not vix_path.exists():
            self.skipTest("historical cache not present in this environment")
        bars = load_daily_closes(nifty_path)
        vix = {b.day: b.close for b in load_daily_closes(vix_path)}
        result = self.bt.run(bars, vix)
        self.assertGreater(len(result.trades), 0)
        for trade in result.trades:
            self.assertGreaterEqual(trade.charges, 0.0)
            # Charges on one lot of index options shouldn't run into the
            # thousands per trade -- a sign the gross-turnover wiring broke.
            self.assertLess(trade.charges, 5000.0)


if __name__ == "__main__":
    unittest.main()
