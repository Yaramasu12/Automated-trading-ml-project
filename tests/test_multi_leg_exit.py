"""Regression tests for the multi-leg entry/exit classification fix (2026-08-03).

Root cause: submit_multi_leg() assigned condor legs OrderPriority.PROTECTIVE_
MULTI_LEG/HEDGE, both below OrderPriority.ENTRY. Three unrelated systems use
priority-relative-to-ENTRY as an implicit entry/exit classifier (_on_fill's
exit-fill branch, the event-risk gate, the capital-protection gate), so every
multi-leg entry was silently misclassified as an exit: no ExitPlan was ever
registered, and two risk gates were bypassed.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

from trading_platform.api.runtime import TradingRuntime
from trading_platform.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentType,
    OptionType,
    OrderPriority,
    Segment,
)
from trading_platform.domain.models import Instrument


def _option(symbol, strike, ot, expiry, underlying="NIFTY"):
    return Instrument(
        symbol=symbol, name=underlying, exchange=Exchange.NFO,
        segment=Segment.OPTIONS, asset_class=AssetClass.INDEX,
        instrument_type=InstrumentType.OPTION, token=symbol, lot_size=50, tick_size=0.05,
        expiry=expiry, strike=strike, option_type=ot, underlying=underlying,
    )


class SubmitMultiLegPriorityTests(unittest.TestCase):
    """submit_multi_leg's legs must default to ENTRY priority, not
    PROTECTIVE_MULTI_LEG/HEDGE, so _on_fill and the scheduler-side risk gates
    correctly treat them as position-opening orders."""

    def _runtime_with_legs(self):
        rt = TradingRuntime()
        exp = date(2100, 1, 7)
        legs = [
            _option("NIFTY24000CE", 24000.0, OptionType.CE, exp),
            _option("NIFTY24300CE", 24300.0, OptionType.CE, exp),
            _option("NIFTY23000PE", 23000.0, OptionType.PE, exp),
            _option("NIFTY22700PE", 22700.0, OptionType.PE, exp),
        ]
        for inst in legs:
            rt.instrument_master.instruments[inst.symbol] = inst
        return rt

    def test_legs_default_to_entry_priority(self):
        rt = self._runtime_with_legs()
        captured = {}

        async def fake_submit(intents, strategy_name=""):
            captured["intents"] = intents
            return SimpleNamespace(rolled_back=False, order_id="test_order", to_dict=lambda: {})

        rt.multi_leg_manager.submit = fake_submit
        payload = {
            "strategy_name": "short_vol_condor",
            "legs": [
                {"symbol": "NIFTY24000CE", "side": "SELL", "price": 100.0, "quantity": 1},
                {"symbol": "NIFTY24300CE", "side": "BUY", "price": 40.0, "quantity": 1},
                {"symbol": "NIFTY23000PE", "side": "SELL", "price": 90.0, "quantity": 1},
                {"symbol": "NIFTY22700PE", "side": "BUY", "price": 35.0, "quantity": 1},
            ],
        }
        asyncio.run(rt.submit_multi_leg(payload))
        intents = captured["intents"]
        self.assertEqual(len(intents), 4)
        for intent in intents:
            self.assertEqual(intent.priority, OrderPriority.ENTRY)
            # opens_position also correctly defaults True (unset) for entries —
            # the capital-protection/compliance/event-risk gates key off this.
            self.assertTrue(intent.signal.metadata.get("opens_position", True))

    def test_explicit_priority_is_respected_for_closes(self):
        """A caller closing a structure (e.g. ShortVolExecutor.close_structure)
        must be able to override the ENTRY default with an exit-tier priority."""
        rt = self._runtime_with_legs()
        captured = {}

        async def fake_submit(intents, strategy_name=""):
            captured["intents"] = intents
            return SimpleNamespace(rolled_back=False, order_id="test_order", to_dict=lambda: {})

        rt.multi_leg_manager.submit = fake_submit
        payload = {
            "strategy_name": "short_vol_exit",
            "legs": [
                {"symbol": "NIFTY24000CE", "side": "BUY", "price": 10.0, "quantity": 1,
                 "priority": "TARGET", "metadata": {"opens_position": False}},
                {"symbol": "NIFTY24300CE", "side": "SELL", "price": 5.0, "quantity": 1,
                 "priority": "TARGET", "metadata": {"opens_position": False}},
            ],
        }
        asyncio.run(rt.submit_multi_leg(payload))
        intents = captured["intents"]
        for intent in intents:
            self.assertEqual(intent.priority, OrderPriority.TARGET)
            self.assertFalse(intent.signal.metadata.get("opens_position", True))


if __name__ == "__main__":
    unittest.main()
