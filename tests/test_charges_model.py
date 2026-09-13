"""Regression tests for audit_results/2026-09-13/AUDIT_REPORT.md finding #3
(high, first half): futures/options STT rates were understated even
against the pre-hike regime, and the Budget 2026 STT hike (effective
1 April 2026, NSE Circular Ref. No. 02/2026) raised them further:
  Futures STT (sell-side):         0.02%  -> 0.05%
  Options STT (sell-side premium): 0.10%  -> 0.15%
The old code used 0.0125% and 0.0625% respectively -- wrong for both eras.
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timezone

from trading_platform.backtesting.charges import ChargesModel
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.domain.enums import Exchange, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal

_MASTER = build_default_universe(date(2026, 1, 5))


def _intent(symbol: str, side: Side, quantity: int = 1, price: float = 100.0,
            product_type: ProductType = ProductType.INTRADAY) -> OrderIntent:
    signal = Signal("test", symbol, side, 0.9, price, "test",
                    datetime.now(timezone.utc), metadata={})
    return OrderIntent(signal, _MASTER.get(symbol), quantity, OrderType.MARKET, product_type)


class FuturesSttTests(unittest.TestCase):
    """Exercises the private rate-selection methods directly with known
    turnover, avoiding any cross-contamination from stamp duty (buy-side
    only) or GST when isolating just the STT component."""

    def test_current_sell_side_stt_is_five_bps(self):
        turnover = 24000.0 * 50
        no_stt_leg = ChargesModel._futures_charges(turnover, True, Exchange.NSE, False)   # buy: no STT
        with_stt_leg = ChargesModel._futures_charges(turnover, False, Exchange.NSE, False)  # sell: STT applies
        # GST is charged on (brokerage + exchange_txn + sebi) only, identical
        # for both sides; stamp duty is buy-only. Isolate STT by removing
        # both sides' shared brokerage/exchange/sebi/gst and the buy's stamp.
        stamp = turnover * 0.00002
        stt = (with_stt_leg) - (no_stt_leg - stamp)
        self.assertAlmostEqual(stt, turnover * 0.0005, places=2)

    def test_pre_hike_sell_side_stt_is_two_bps(self):
        turnover = 24000.0 * 50
        no_stt_leg = ChargesModel._futures_charges(turnover, True, Exchange.NSE, True)
        with_stt_leg = ChargesModel._futures_charges(turnover, False, Exchange.NSE, True)
        stamp = turnover * 0.00002
        stt = with_stt_leg - (no_stt_leg - stamp)
        self.assertAlmostEqual(stt, turnover * 0.0002, places=2)

    def test_buy_side_never_charged_stt(self):
        turnover = 24000.0 * 50
        current = ChargesModel._futures_charges(turnover, True, Exchange.NSE, False)
        pre_hike = ChargesModel._futures_charges(turnover, True, Exchange.NSE, True)
        self.assertEqual(current, pre_hike)

    def test_estimate_wires_as_of_through_to_the_rate_selection(self):
        fut = _MASTER.select_future("NIFTY", date(2026, 1, 5))
        intent = replace(_intent(fut.symbol, Side.SELL, 1, 24000.0), instrument=fut)
        current = ChargesModel().estimate(intent, 24000.0, as_of=date(2026, 9, 13))
        pre_hike = ChargesModel().estimate(intent, 24000.0, as_of=date(2026, 3, 31))
        no_date = ChargesModel().estimate(intent, 24000.0)
        self.assertGreater(current, pre_hike)
        self.assertEqual(no_date, current)  # None defaults to "now" = current rate


class OptionsSttTests(unittest.TestCase):
    def test_current_sell_side_premium_stt_is_fifteen_bps(self):
        premium_turnover = 120.0 * 50
        no_stt_leg = ChargesModel._options_charges(premium_turnover, True, False)
        with_stt_leg = ChargesModel._options_charges(premium_turnover, False, False)
        stamp = premium_turnover * 0.00003
        stt = with_stt_leg - (no_stt_leg - stamp)
        self.assertAlmostEqual(stt, premium_turnover * 0.0015, places=2)

    def test_pre_hike_sell_side_premium_stt_is_ten_bps(self):
        premium_turnover = 120.0 * 50
        no_stt_leg = ChargesModel._options_charges(premium_turnover, True, True)
        with_stt_leg = ChargesModel._options_charges(premium_turnover, False, True)
        stamp = premium_turnover * 0.00003
        stt = with_stt_leg - (no_stt_leg - stamp)
        self.assertAlmostEqual(stt, premium_turnover * 0.001, places=2)

    def test_estimate_wires_as_of_through_to_the_rate_selection(self):
        from trading_platform.domain.enums import OptionType
        option = _MASTER.select_option("NIFTY", date(2026, 1, 5), 22500, OptionType.CE)
        intent = replace(_intent(option.symbol, Side.SELL, 1, 120.0), instrument=option)
        current = ChargesModel().estimate(intent, 120.0, as_of=date(2026, 9, 13))
        pre_hike = ChargesModel().estimate(intent, 120.0, as_of=date(2026, 3, 31))
        no_date = ChargesModel().estimate(intent, 120.0)
        self.assertGreater(current, pre_hike)
        self.assertEqual(no_date, current)


if __name__ == "__main__":
    unittest.main()
