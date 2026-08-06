"""Unit tests for the near-expiry gamma exposure gate — REDESIGN_PROMPT.md
§6.1's "gamma-near-expiry" cap. RiskEngine already had a `gamma_exposure`
parameter and `max_gamma_near_expiry` threshold, but nothing anywhere
computed a real value for it before 2026-08-06, so the check could never
fire in production. This wires real net gamma (from PortfolioGreeksCalculator,
filtered to <=1 DTE positions) behind Settings.enable_gamma_exposure_gate
(default off, since the threshold predates any real gamma ever being
observed against it).

Two layers, tested separately:
  1. TradingRuntime._near_expiry_gamma_exposure — does the computation
     correctly filter to near-expiry option positions and sum net gamma.
  2. TradingRuntime._evaluate_final_execution_gate — does the flag
     correctly gate whether that computation's result reaches RiskEngine,
     with payload override always taking precedence either way.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from trading_platform.api.runtime import TradingRuntime
from trading_platform.domain.enums import (
    AssetClass, Exchange, InstrumentType, OptionType, OrderPriority, OrderType,
    ProductType, Segment, Side,
)
from trading_platform.domain.models import Instrument, OrderIntent, Position, Signal


def _option(symbol, strike, ot, expiry, underlying="NIFTY", lot_size=50):
    return Instrument(
        symbol=symbol, name=underlying, exchange=Exchange.NFO, segment=Segment.OPTIONS,
        asset_class=AssetClass.INDEX, instrument_type=InstrumentType.OPTION,
        token="1", lot_size=lot_size, strike=strike, option_type=ot, expiry=expiry,
        underlying=underlying,
    )


class NearExpiryGammaExposureTests(unittest.TestCase):
    def _runtime(self) -> TradingRuntime:
        rt = TradingRuntime()
        rt.portfolio.positions.clear()
        return rt

    def test_no_positions_is_zero(self):
        rt = self._runtime()
        self.assertEqual(rt._near_expiry_gamma_exposure(datetime.now(timezone.utc)), 0.0)

    def test_far_expiry_position_excluded(self):
        rt = self._runtime()
        now = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        far_expiry = date(2026, 2, 10)  # 31 days out
        rt.portfolio.positions["x"] = Position(
            instrument=_option("NIFTY24000CE", 24000.0, OptionType.CE, far_expiry),
            quantity=-1, average_price=150.0,
        )
        rt._position_spot_price = lambda u: 24000.0
        rt._position_mark_price = lambda pos: 150.0
        self.assertEqual(rt._near_expiry_gamma_exposure(now), 0.0)

    def test_zero_quantity_position_excluded(self):
        rt = self._runtime()
        now = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        rt.portfolio.positions["x"] = Position(
            instrument=_option("NIFTY24000CE", 24000.0, OptionType.CE, date(2026, 1, 10)),
            quantity=0, average_price=150.0,
        )
        rt._position_spot_price = lambda u: 24000.0
        rt._position_mark_price = lambda pos: 150.0
        self.assertEqual(rt._near_expiry_gamma_exposure(now), 0.0)

    def test_near_expiry_short_position_contributes_negative_net_gamma(self):
        rt = self._runtime()
        now = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        rt.portfolio.positions["x"] = Position(
            instrument=_option("NIFTY24000CE", 24000.0, OptionType.CE, date(2026, 1, 10)),  # DTE=0
            quantity=-1, average_price=150.0,
        )
        rt._position_spot_price = lambda u: 24000.0
        rt._position_mark_price = lambda pos: 150.0
        result = rt._near_expiry_gamma_exposure(now)
        self.assertLess(result, 0.0)  # short gamma

    def test_dte_one_still_counts_as_near_expiry(self):
        rt = self._runtime()
        now = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        rt.portfolio.positions["x"] = Position(
            instrument=_option("NIFTY24000CE", 24000.0, OptionType.CE, date(2026, 1, 11)),  # DTE=1
            quantity=-1, average_price=150.0,
        )
        rt._position_spot_price = lambda u: 24000.0
        rt._position_mark_price = lambda pos: 150.0
        self.assertNotEqual(rt._near_expiry_gamma_exposure(now), 0.0)

    def test_dte_two_excluded(self):
        rt = self._runtime()
        now = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        rt.portfolio.positions["x"] = Position(
            instrument=_option("NIFTY24000CE", 24000.0, OptionType.CE, date(2026, 1, 12)),  # DTE=2
            quantity=-1, average_price=150.0,
        )
        rt._position_spot_price = lambda u: 24000.0
        rt._position_mark_price = lambda pos: 150.0
        self.assertEqual(rt._near_expiry_gamma_exposure(now), 0.0)


class GammaGateWiringTests(unittest.TestCase):
    """Tests the flag/override plumbing in _evaluate_final_execution_gate in
    isolation from _near_expiry_gamma_exposure's own correctness (mocked to
    a fixed large value here) — see NearExpiryGammaExposureTests for that."""

    def _intent(self, now: datetime) -> OrderIntent:
        # DTE=1, not 0: DTE=0 also trips the separate expiry_day_open_cutoff
        # check (after 14:00 IST) — these tests target the gamma branch
        # specifically, which DTE<=1 already satisfies (see
        # NearExpiryGammaExposureTests.test_dte_one_still_counts_as_near_expiry).
        expiry = now.date() + timedelta(days=1)
        instrument = _option("NIFTY24000CE", 24000.0, OptionType.CE, expiry)
        signal = Signal(
            strategy_name="test", symbol=instrument.symbol, side=Side.SELL,
            confidence=1.0, price=150.0, reason="test", created_at=now,
            # hedged=True bypasses the (unrelated) naked-option-selling check
            # so these tests reach the near-expiry gamma branch specifically.
            metadata={"opens_position": True, "hedged": True},
        )
        return OrderIntent(
            signal=signal, instrument=instrument, quantity=1,
            order_type=OrderType.MARKET, product_type=ProductType.CARRYFORWARD,
            limit_price=150.0, priority=OrderPriority.ENTRY,
        )

    def _runtime(self) -> TradingRuntime:
        rt = TradingRuntime()
        rt.portfolio.positions.clear()
        rt.kill_switch_active = False
        # Guarantee a fixed, large gamma reading regardless of what real
        # positions exist — isolates the wiring test from the calculator.
        rt._near_expiry_gamma_exposure = mock.Mock(return_value=999.0)
        return rt

    def test_flag_off_by_default_ignores_real_gamma(self):
        rt = self._runtime()
        self.assertFalse(rt.settings.enable_gamma_exposure_gate)
        now = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        decision = rt._evaluate_final_execution_gate(
            self._intent(now), now=now, payload={"_trace_side_effects": False},
        )
        self.assertNotEqual(decision.reason, "near_expiry_gamma_exceeds_limit")
        rt._near_expiry_gamma_exposure.assert_not_called()

    def test_flag_on_uses_computed_gamma_and_rejects(self):
        rt = self._runtime()
        import dataclasses
        rt.settings = dataclasses.replace(rt.settings, enable_gamma_exposure_gate=True)
        now = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        decision = rt._evaluate_final_execution_gate(
            self._intent(now), now=now, payload={"_trace_side_effects": False},
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "near_expiry_gamma_exceeds_limit")
        rt._near_expiry_gamma_exposure.assert_called_once_with(now)

    def test_payload_override_takes_precedence_even_when_flag_off(self):
        rt = self._runtime()
        now = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        decision = rt._evaluate_final_execution_gate(
            self._intent(now), now=now,
            payload={"_trace_side_effects": False, "gamma_exposure": 999.0},
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "near_expiry_gamma_exceeds_limit")
        rt._near_expiry_gamma_exposure.assert_not_called()

    def test_payload_override_of_zero_is_respected_even_when_flag_on(self):
        rt = self._runtime()
        import dataclasses
        rt.settings = dataclasses.replace(rt.settings, enable_gamma_exposure_gate=True)
        now = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        decision = rt._evaluate_final_execution_gate(
            self._intent(now), now=now,
            payload={"_trace_side_effects": False, "gamma_exposure": 0.0},
        )
        self.assertNotEqual(decision.reason, "near_expiry_gamma_exceeds_limit")
        rt._near_expiry_gamma_exposure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
