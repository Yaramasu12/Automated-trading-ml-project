"""Unit tests for PortfolioGreeksCalculator (REDESIGN_PROMPT.md §6.1's
"portfolio Greeks caps" gap — the aggregation half of it)."""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from trading_platform.domain.enums import (
    AssetClass, Exchange, InstrumentType, OptionType, Segment,
)
from trading_platform.domain.models import Instrument, Position
from trading_platform.risk.portfolio_greeks import PortfolioGreeksCalculator


def _option(symbol, strike, ot, expiry, underlying="NIFTY", lot_size=50):
    return Instrument(
        symbol=symbol, name=underlying, exchange=Exchange.NFO, segment=Segment.OPTIONS,
        asset_class=AssetClass.INDEX, instrument_type=InstrumentType.OPTION,
        token="1", lot_size=lot_size, strike=strike, option_type=ot, expiry=expiry,
        underlying=underlying,
    )


def _equity(symbol="RELIANCE"):
    return Instrument(
        symbol=symbol, name=symbol, exchange=Exchange.NSE, segment=Segment.CASH,
        asset_class=AssetClass.EQUITY, instrument_type=InstrumentType.EQUITY, token="1",
    )


class PortfolioGreeksCalculatorTests(unittest.TestCase):
    def setUp(self):
        self.calc = PortfolioGreeksCalculator()
        self.expiry = date.today() + timedelta(days=7)
        self.spot = 24000.0

    def _spot_price(self, underlying):
        return self.spot if underlying == "NIFTY" else None

    def _mark_price_const(self, price):
        return lambda pos: price

    def test_empty_portfolio_is_all_zero(self):
        snap = self.calc.compute({}, self._spot_price, self._mark_price_const(150.0))
        self.assertEqual(snap.net_delta, 0.0)
        self.assertEqual(snap.net_gamma, 0.0)
        self.assertEqual(snap.net_theta, 0.0)
        self.assertEqual(snap.net_vega, 0.0)
        self.assertEqual(snap.positions, ())
        self.assertEqual(snap.skipped, ())

    def test_zero_quantity_position_ignored(self):
        inst = _option("NIFTY24000CE", 24000.0, OptionType.CE, self.expiry)
        positions = {"x": Position(instrument=inst, quantity=0, average_price=150.0)}
        snap = self.calc.compute(positions, self._spot_price, self._mark_price_const(150.0))
        self.assertEqual(snap.positions, ())

    def test_non_option_position_ignored_not_skipped(self):
        positions = {"x": Position(instrument=_equity(), quantity=10, average_price=2800.0)}
        snap = self.calc.compute(positions, self._spot_price, self._mark_price_const(2800.0))
        self.assertEqual(snap.positions, ())
        self.assertEqual(snap.skipped, ())  # not an option -> not even attempted

    def test_short_call_has_negative_net_delta_gamma_vega_positive_theta(self):
        inst = _option("NIFTY24000CE", 24000.0, OptionType.CE, self.expiry)
        positions = {"x": Position(instrument=inst, quantity=-50, average_price=150.0)}  # 1 lot short
        snap = self.calc.compute(positions, self._spot_price, self._mark_price_const(150.0))
        self.assertEqual(len(snap.positions), 1)
        p = snap.positions[0]
        self.assertLess(p.net_delta, 0.0)   # short a positive-delta call -> negative net delta
        self.assertLess(p.net_gamma, 0.0)   # short gamma
        self.assertLess(p.net_vega, 0.0)    # short vega (the whole point of selling premium)
        self.assertGreater(p.net_theta, 0.0)  # short options collect theta
        self.assertGreater(p.implied_vol, 0.0)
        self.assertEqual(p.days_to_expiry, 7)

    def test_long_put_has_negative_net_delta_positive_gamma_vega(self):
        inst = _option("NIFTY24000PE", 24000.0, OptionType.PE, self.expiry)
        positions = {"x": Position(instrument=inst, quantity=50, average_price=150.0)}  # 1 lot long
        snap = self.calc.compute(positions, self._spot_price, self._mark_price_const(150.0))
        p = snap.positions[0]
        self.assertLess(p.net_delta, 0.0)    # put delta is negative; long -> stays negative
        self.assertGreater(p.net_gamma, 0.0)  # long gamma
        self.assertGreater(p.net_vega, 0.0)   # long vega
        self.assertLess(p.net_theta, 0.0)     # long options bleed theta

    def test_short_put_has_positive_net_delta(self):
        inst = _option("NIFTY23800PE", 23800.0, OptionType.PE, self.expiry)
        positions = {"x": Position(instrument=inst, quantity=-50, average_price=60.0)}
        snap = self.calc.compute(positions, self._spot_price, self._mark_price_const(60.0))
        p = snap.positions[0]
        self.assertGreater(p.net_delta, 0.0)  # negative delta * negative qty -> positive

    def test_skipped_when_no_spot_available(self):
        inst = _option("BANKNIFTY51000CE", 51000.0, OptionType.CE, self.expiry, underlying="BANKNIFTY")
        positions = {"x": Position(instrument=inst, quantity=-15, average_price=200.0)}
        snap = self.calc.compute(positions, self._spot_price, self._mark_price_const(200.0))
        self.assertEqual(snap.positions, ())
        self.assertEqual(snap.skipped, ("BANKNIFTY51000CE",))

    def test_skipped_when_no_mark_price(self):
        inst = _option("NIFTY24000CE", 24000.0, OptionType.CE, self.expiry)
        positions = {"x": Position(instrument=inst, quantity=-50, average_price=150.0)}
        snap = self.calc.compute(positions, self._spot_price, lambda pos: None)
        self.assertEqual(snap.skipped, ("NIFTY24000CE",))

    def test_skipped_when_iv_inversion_out_of_sane_range(self):
        # An absurdly low mark for a 7-DTE ATM option inverts to near-zero IV,
        # outside compute_iv_rank/short_vol's own 0.01-3.0 sanity band.
        inst = _option("NIFTY24000CE", 24000.0, OptionType.CE, self.expiry)
        positions = {"x": Position(instrument=inst, quantity=-50, average_price=150.0)}
        snap = self.calc.compute(positions, self._spot_price, self._mark_price_const(0.0001))
        self.assertEqual(snap.skipped, ("NIFTY24000CE",))

    def test_full_condor_nets_close_to_delta_neutral_and_net_short_vega(self):
        # Symmetric iron condor: short call + long call wing, short put + long
        # put wing, roughly equidistant from spot -> approximately delta
        # neutral overall, but unambiguously net short vega/gamma (the
        # strategy's whole point) since all four legs contribute the same
        # sign of vega/gamma exposure scaled by direction.
        legs = [
            ("NIFTY24100CE", 24100.0, OptionType.CE, -50, 100.0),  # short call
            ("NIFTY24200CE", 24200.0, OptionType.CE, 50, 60.0),    # long call wing
            ("NIFTY23900PE", 23900.0, OptionType.PE, -50, 100.0),  # short put
            ("NIFTY23800PE", 23800.0, OptionType.PE, 50, 60.0),    # long put wing
        ]
        positions = {
            sym: Position(instrument=_option(sym, k, ot, self.expiry), quantity=qty, average_price=px)
            for sym, k, ot, qty, px in legs
        }
        mark_by_symbol = {sym: px for sym, _, _, _, px in legs}
        snap = self.calc.compute(positions, self._spot_price, lambda pos: mark_by_symbol[pos.instrument.symbol])
        self.assertEqual(len(snap.positions), 4)
        self.assertEqual(snap.skipped, ())
        # Real offsetting, not a hardcoded delta number: a directional bet
        # would have net_delta close to the sum of the legs' magnitudes; a
        # roughly balanced condor's net is well under that.
        gross_delta = sum(abs(p.net_delta) for p in snap.positions)
        self.assertLess(abs(snap.net_delta), gross_delta / 2)
        self.assertLess(snap.net_vega, 0.0)              # net short vega — the strategy's edge
        self.assertLess(snap.net_gamma, 0.0)             # net short gamma — the strategy's risk

    def test_to_dict_shape(self):
        inst = _option("NIFTY24000CE", 24000.0, OptionType.CE, self.expiry)
        positions = {"x": Position(instrument=inst, quantity=-50, average_price=150.0)}
        snap = self.calc.compute(positions, self._spot_price, self._mark_price_const(150.0))
        d = snap.to_dict()
        self.assertIn("net_delta", d)
        self.assertIn("net_gamma", d)
        self.assertIn("net_theta", d)
        self.assertIn("net_vega", d)
        self.assertEqual(d["position_count"], 1)
        self.assertEqual(d["skipped_count"], 0)
        self.assertEqual(len(d["positions"]), 1)
        self.assertEqual(d["positions"][0]["symbol"], "NIFTY24000CE")


if __name__ == "__main__":
    unittest.main()
