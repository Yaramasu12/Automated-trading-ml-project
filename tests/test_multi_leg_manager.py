"""Direct tests for MultiLegOrderManager's leg-timeout/rollback path.

Section 18 audit (2026-09-16): MultiLegOrderManager has real leg-timeout/
rollback logic (submit()'s asyncio.wait_for + _rollback()), but every
existing multi-leg test (test_multi_leg_exit.py) stubs out
rt.multi_leg_manager.submit wholesale — the "missing options leg" fault
mode (one leg of a spread never fills at the broker) was never actually
exercised against the real manager. These tests drive the real
MultiLegOrderManager directly, with a fake enqueue_fn that intentionally
never confirms one leg's fill, so the timeout/rollback code genuinely runs.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from trading_platform.data.instrument_master import build_default_universe
from trading_platform.domain.enums import OrderPriority, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal, Trade
from trading_platform.execution.multi_leg_manager import MultiLegOrderManager

_MASTER = build_default_universe(date(2026, 1, 5))


def _leg_intent(symbol: str, side: Side, key: str) -> OrderIntent:
    signal = Signal(
        "short_vol_condor", symbol, side, 0.9, 100.0, "test_leg",
        datetime.now(timezone.utc), metadata={},
    )
    return OrderIntent(
        signal, _MASTER.get(symbol), 1, OrderType.MARKET, ProductType.CARRYFORWARD,
        priority=OrderPriority.ENTRY, idempotency_key=key,
    )


class MissingLegFaultInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_leg_that_never_fills_is_rolled_back_after_timeout(self):
        """The exact fault mode: leg 1 fills normally, leg 2 (e.g. the
        broker silently drops the second leg of a condor) never confirms.
        The manager must time out on leg 2, mark it FAILED, and reverse
        leg 1 rather than leave a naked, half-open position."""
        enqueued: list[OrderIntent] = []

        async def fake_enqueue(intent: OrderIntent) -> str:
            enqueued.append(intent)
            if intent.idempotency_key == "leg1":
                # Simulate a normal, prompt broker fill for leg 1 only.
                trade = Trade(
                    "t1", "leg1", intent.instrument.symbol, intent.signal.side,
                    intent.quantity, 100.0, 0.0, datetime.now(timezone.utc), "short_vol_condor",
                )
                manager.notify_fill("leg1", trade)
            # leg2 (and any rollback intent) is deliberately never confirmed —
            # this is the fault being injected.
            return "broker-ack"

        manager = MultiLegOrderManager(enqueue_fn=fake_enqueue)

        legs = [
            _leg_intent("RELIANCE", Side.SELL, "leg1"),
            _leg_intent("TCS", Side.SELL, "leg2"),
        ]

        with patch("trading_platform.execution.multi_leg_manager._LEG_FILL_TIMEOUT", 0.05):
            ml_order = await manager.submit(legs, strategy_name="short_vol_condor")

        self.assertTrue(ml_order.rolled_back)
        self.assertFalse(ml_order.active)

        leg1, leg2 = ml_order.legs
        self.assertEqual(leg1.status.value, "CANCELLED")  # rolled back
        self.assertEqual(leg2.status.value, "FAILED")
        self.assertIn("timed out", leg2.error)

        # A reverse (opposite-side) intent for leg 1 must have been enqueued.
        reverse_intents = [
            i for i in enqueued
            if i.instrument.symbol == "RELIANCE" and i is not legs[0]
        ]
        self.assertEqual(len(reverse_intents), 1)
        self.assertEqual(reverse_intents[0].signal.side, Side.BUY)  # opposite of the original SELL
        self.assertEqual(reverse_intents[0].priority, OrderPriority.EMERGENCY_EXIT)

    async def test_all_legs_filling_normally_does_not_roll_back(self):
        """Sanity check: the fault-injection test above isn't just always
        rolling back — a fully-confirmed spread must complete normally."""
        manager = MultiLegOrderManager(enqueue_fn=None)

        async def fake_enqueue(intent: OrderIntent) -> str:
            trade = Trade(
                "t", intent.idempotency_key, intent.instrument.symbol, intent.signal.side,
                intent.quantity, 100.0, 0.0, datetime.now(timezone.utc), "short_vol_condor",
            )
            manager.notify_fill(intent.idempotency_key, trade)
            return "broker-ack"

        manager._enqueue = fake_enqueue

        legs = [
            _leg_intent("RELIANCE", Side.SELL, "leg1"),
            _leg_intent("TCS", Side.SELL, "leg2"),
        ]

        ml_order = await manager.submit(legs, strategy_name="short_vol_condor")

        self.assertFalse(ml_order.rolled_back)
        self.assertTrue(ml_order.all_filled)


if __name__ == "__main__":
    unittest.main()
