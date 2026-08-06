"""Unit tests for HistoricalVarCalculator (REDESIGN_PROMPT.md §6.1's
remaining explicit gap: historical-simulation VaR on the options book).

Note: Position.quantity is denominated in LOTS, not raw units (confirmed
via Position.market_value = quantity * price * lot_size) — every fixture
below uses quantity=-1/1 for "one lot short/long" accordingly."""
from __future__ import annotations

import math
import unittest
from datetime import date, timedelta

from trading_platform.derivatives.engine import black_scholes_price
from trading_platform.domain.enums import (
    AssetClass, Exchange, InstrumentType, OptionType, Segment,
)
from trading_platform.domain.models import Instrument, Position
from trading_platform.risk.historical_var import (
    MIN_VAR_OBSERVATIONS,
    HistoricalVarCalculator,
)


def _option(symbol, strike, ot, expiry, underlying="NIFTY", lot_size=50):
    return Instrument(
        symbol=symbol, name=underlying, exchange=Exchange.NFO, segment=Segment.OPTIONS,
        asset_class=AssetClass.INDEX, instrument_type=InstrumentType.OPTION,
        token="1", lot_size=lot_size, strike=strike, option_type=ot, expiry=expiry,
        underlying=underlying,
    )


class HistoricalVarCalculatorTests(unittest.TestCase):
    def setUp(self):
        self.calc = HistoricalVarCalculator()
        self.spot = 24000.0
        self.strike = 24000.0
        self.expiry = date.today() + timedelta(days=7)
        self.iv = 0.15
        self.t_years = 7 / 365.0
        self.lot_size = 50
        self.mark = black_scholes_price(self.spot, self.strike, self.t_years, self.iv, OptionType.CE)

    def _spot_price(self, underlying):
        return self.spot if underlying == "NIFTY" else None

    def _mark_price_const(self, price):
        return lambda pos: price

    def test_insufficient_history_returns_none(self):
        positions = {"x": Position(instrument=_option("NIFTY24000CE", self.strike, OptionType.CE, self.expiry),
                                    quantity=-1, average_price=self.mark)}
        short_history = [0.0] * (MIN_VAR_OBSERVATIONS - 1)
        result = self.calc.compute(positions, self._spot_price, self._mark_price_const(self.mark),
                                    lambda u: short_history)
        self.assertIsNone(result)

    def test_no_priceable_positions_returns_none(self):
        result = self.calc.compute({}, self._spot_price, self._mark_price_const(self.mark), lambda u: [0.0] * 30)
        self.assertIsNone(result)

    def test_flat_scenarios_give_zero_var(self):
        """Every historical return is 0 -> spot never moves -> every
        scenario reprices identically to the current mark -> zero P&L
        everywhere -> VaR is exactly 0."""
        positions = {"x": Position(instrument=_option("NIFTY24000CE", self.strike, OptionType.CE, self.expiry),
                                    quantity=-1, average_price=self.mark)}
        result = self.calc.compute(positions, self._spot_price, self._mark_price_const(self.mark),
                                    lambda u: [0.0] * 30)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.var_95, 0.0, places=6)
        self.assertAlmostEqual(result.var_99, 0.0, places=6)
        self.assertAlmostEqual(result.worst_case_pnl, 0.0, places=6)

    def test_short_call_worst_case_is_a_big_up_move(self):
        """19 flat days + 1 day with a large +15% move. A short call loses
        badly on a big up move -> that single scenario must be the worst
        case, and its exact P&L is hand-computable via black_scholes_price."""
        positions = {"x": Position(instrument=_option("NIFTY24000CE", self.strike, OptionType.CE, self.expiry),
                                    quantity=-1, average_price=self.mark)}  # 1 lot short
        history = [0.0] * (MIN_VAR_OBSERVATIONS - 1) + [0.15]
        result = self.calc.compute(positions, self._spot_price, self._mark_price_const(self.mark),
                                    lambda u: history)
        self.assertIsNotNone(result)
        self.assertEqual(result.scenario_count, MIN_VAR_OBSERVATIONS)

        shocked_spot = self.spot * math.exp(0.15)
        shocked_price = black_scholes_price(shocked_spot, self.strike, self.t_years, self.iv, OptionType.CE)
        expected_pnl = (shocked_price - self.mark) * (-1 * self.lot_size)
        self.assertAlmostEqual(result.worst_case_pnl, expected_pnl, places=2)
        self.assertLess(result.worst_case_pnl, 0.0)  # short call genuinely loses on an up move

        # With exactly MIN_VAR_OBSERVATIONS=20 scenarios sorted ascending,
        # only the single big-loss scenario is non-zero -> idx_95=1 lands on
        # a flat (zero) scenario, idx_99=0 lands on the big loss itself.
        self.assertAlmostEqual(result.var_95, 0.0, places=6)
        self.assertAlmostEqual(result.var_99, -expected_pnl, places=2)

    def test_long_put_worst_case_is_a_big_down_move(self):
        put_mark = black_scholes_price(self.spot, self.strike, self.t_years, self.iv, OptionType.PE)
        positions = {"x": Position(instrument=_option("NIFTY24000PE", self.strike, OptionType.PE, self.expiry),
                                    quantity=1, average_price=put_mark)}  # long 1 lot
        history = [0.0] * (MIN_VAR_OBSERVATIONS - 1) + [-0.15]
        result = self.calc.compute(positions, self._spot_price, self._mark_price_const(put_mark),
                                    lambda u: history)
        # A LONG put GAINS on a down move -> the down-move scenario is the
        # BEST case here, not the worst; worst case is one of the flat (zero
        # P&L) scenarios.
        self.assertGreater(result.best_case_pnl, 0.0)
        self.assertAlmostEqual(result.worst_case_pnl, 0.0, places=6)

    def test_multiple_positions_same_underlying_share_the_scenario(self):
        """A short call + long call wing on the SAME underlying must be
        shocked TOGETHER per scenario (correlated), not independently —
        verified by checking the combined worst case is smaller in
        magnitude than the short leg's worst case alone (the wing hedges
        it), proving they were netted within one scenario rather than
        each contributing its own separately-worst scenario."""
        short_only = {"x": Position(instrument=_option("NIFTY24000CE", self.strike, OptionType.CE, self.expiry),
                                     quantity=-1, average_price=self.mark)}
        history = [0.0] * (MIN_VAR_OBSERVATIONS - 1) + [0.15]
        short_result = self.calc.compute(short_only, self._spot_price, self._mark_price_const(self.mark),
                                          lambda u: history)

        # Close enough to spot to avoid ImpliedVolatilityCalculator's known
        # Newton-Raphson convergence quirk on deep-OTM + short-DTE strikes
        # (confirmed 2026-08-06 while building the portfolio-Greeks tests —
        # it can floor out at sigma=0.001 instead of recovering the true
        # ~15% vol, which would make this leg get silently skipped).
        wing_strike = 24200.0
        wing_mark = black_scholes_price(self.spot, wing_strike, self.t_years, self.iv, OptionType.CE)

        def mark_for(pos):
            return self.mark if pos.instrument.strike == self.strike else wing_mark

        condor_like = {
            "short": Position(instrument=_option("NIFTY24000CE", self.strike, OptionType.CE, self.expiry),
                               quantity=-1, average_price=self.mark),
            "wing": Position(instrument=_option("NIFTY24200CE", wing_strike, OptionType.CE, self.expiry),
                              quantity=1, average_price=wing_mark),
        }
        hedged_result = self.calc.compute(condor_like, self._spot_price, mark_for, lambda u: history)

        self.assertLess(abs(hedged_result.worst_case_pnl), abs(short_result.worst_case_pnl))

    def test_multiple_underlyings_aligned_by_scenario_index(self):
        """A scenario where BOTH underlyings move against the same short
        positions simultaneously (correlated/aligned scenario) must produce
        a worse loss than either underlying's own worst case alone —
        proving scenarios are aligned across underlyings, not sampled
        independently per underlying."""
        bn_spot = 51000.0
        bn_strike = 51000.0
        bn_lot_size = 15
        bn_mark = black_scholes_price(bn_spot, bn_strike, self.t_years, self.iv, OptionType.CE)

        def spot_price(u):
            return self.spot if u == "NIFTY" else (bn_spot if u == "BANKNIFTY" else None)

        def mark_price(pos):
            return self.mark if pos.instrument.underlying == "NIFTY" else bn_mark

        positions = {
            "n": Position(instrument=_option("NIFTY24000CE", self.strike, OptionType.CE, self.expiry),
                           quantity=-1, average_price=self.mark),
            "bn": Position(instrument=_option("BANKNIFTY51000CE", bn_strike, OptionType.CE, self.expiry,
                                               underlying="BANKNIFTY", lot_size=bn_lot_size),
                            quantity=-1, average_price=bn_mark),
        }
        # Both underlyings spike hard on the SAME (last) trading day.
        history = [0.0] * (MIN_VAR_OBSERVATIONS - 1) + [0.15]
        result = self.calc.compute(positions, spot_price, mark_price, lambda u: history)

        nifty_shocked = black_scholes_price(self.spot * math.exp(0.15), self.strike, self.t_years, self.iv, OptionType.CE)
        bn_shocked = black_scholes_price(bn_spot * math.exp(0.15), bn_strike, self.t_years, self.iv, OptionType.CE)
        nifty_leg_pnl = (nifty_shocked - self.mark) * (-1 * self.lot_size)
        bn_leg_pnl = (bn_shocked - bn_mark) * (-1 * bn_lot_size)
        expected_combined = nifty_leg_pnl + bn_leg_pnl
        self.assertAlmostEqual(result.worst_case_pnl, expected_combined, places=1)
        # A single scenario combined both legs' losses -> only ONE non-zero
        # (worse) scenario exists, same shape as the single-underlying test.
        self.assertLess(result.worst_case_pnl, min(nifty_leg_pnl, bn_leg_pnl))

    def test_skipped_when_no_spot_available(self):
        inst = _option("BANKNIFTY51000CE", 51000.0, OptionType.CE, self.expiry, underlying="BANKNIFTY")
        positions = {"x": Position(instrument=inst, quantity=-1, average_price=200.0)}
        result = self.calc.compute(positions, self._spot_price, self._mark_price_const(200.0), lambda u: [0.0] * 30)
        self.assertIsNone(result)  # nothing priceable at all

    def test_var_99_is_at_least_var_95(self):
        positions = {"x": Position(instrument=_option("NIFTY24000CE", self.strike, OptionType.CE, self.expiry),
                                    quantity=-1, average_price=self.mark)}
        import random
        rng = random.Random(7)
        history = [rng.gauss(0, 0.01) for _ in range(100)]
        result = self.calc.compute(positions, self._spot_price, self._mark_price_const(self.mark),
                                    lambda u: history)
        self.assertGreaterEqual(result.var_99, result.var_95)

    def test_to_dict_shape(self):
        positions = {"x": Position(instrument=_option("NIFTY24000CE", self.strike, OptionType.CE, self.expiry),
                                    quantity=-1, average_price=self.mark)}
        result = self.calc.compute(positions, self._spot_price, self._mark_price_const(self.mark),
                                    lambda u: [0.0] * 30)
        d = result.to_dict()
        self.assertTrue(d["available"])
        for key in ("var_95", "var_99", "scenario_count", "worst_case_pnl", "best_case_pnl"):
            self.assertIn(key, d)


if __name__ == "__main__":
    unittest.main()
