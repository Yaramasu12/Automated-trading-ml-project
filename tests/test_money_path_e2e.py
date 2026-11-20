"""TEST-1: golden end-to-end test of the REAL money path.

The one path that actually trades is the short-vol options executor placing a
defined-risk condor. End to end that is:

    build() -> submit_multi_leg -> scheduler final gate (no-price -> naked-short
    defense -> EdgeGate -> RiskEngine) -> SimulatedBrokerClient fill -> ledger
    -> _on_fill -> ExitPlan armed -> close -> flat + realized P&L.

Every stage is unit-tested in isolation elsewhere — build() in test_short_vol,
leg priority and hedge-first sequencing in test_multi_leg_exit, fill handling
in the scheduler tests, exit logic in test_exit_* — but nothing wired the whole
chain together in ONE flow on a real PAPER TradingRuntime with a simulated
broker. A regression in the seam BETWEEN any two stages (a fill that doesn't
register an ExitPlan, a close that doesn't flatten, a gate that stops approving
defined-risk shorts) therefore passed every existing test while silently
breaking the only path that trades. This is the test that fails loudly instead.

It drives the legs through the real scheduler directly rather than through the
multi-leg manager's sequential-await submit (that rollback/sequencing logic has
its own tests in test_multi_leg_exit and would make this test race a background
rollback). The legs still pass through the SAME final gate, broker, ledger,
fill callback and exit accounting the live path uses — including the
multi_leg_group metadata the real submit_multi_leg path sets so the gate
recognises each short leg as a covered, defined-risk structure rather than a
naked sale.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date

from trading_platform.api.runtime import TradingRuntime
from trading_platform.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentType,
    OptionType,
    Segment,
    Side,
)
from trading_platform.domain.models import Instrument

_EXPIRY = date(2100, 1, 7)  # far-dated so expiry/DTE gates never interfere
_GROUP = "condor-golden-e2e"

# A NIFTY iron condor: long wings protect short bodies (defined risk).
_LEGS = {
    "NIFTY24000CE": (24000.0, OptionType.CE),  # short call body
    "NIFTY24300CE": (24300.0, OptionType.CE),  # long call wing
    "NIFTY23000PE": (23000.0, OptionType.PE),  # short put body
    "NIFTY22700PE": (22700.0, OptionType.PE),  # long put wing
}


def _option(symbol: str, strike: float, option_type: OptionType) -> Instrument:
    return Instrument(
        symbol=symbol, name="NIFTY", exchange=Exchange.NFO, segment=Segment.OPTIONS,
        asset_class=AssetClass.INDEX, instrument_type=InstrumentType.OPTION,
        token=symbol, lot_size=50, tick_size=0.05, expiry=_EXPIRY, strike=strike,
        option_type=option_type, underlying="NIFTY",
    )


class MoneyPathGoldenE2ETests(unittest.TestCase):
    def _runtime(self) -> TradingRuntime:
        rt = TradingRuntime()  # defaults to PAPER, no network, SimulatedBrokerClient
        self.assertEqual(rt.execution_mode.value, "PAPER")
        for symbol, (strike, ot) in _LEGS.items():
            rt.instrument_master.instruments[symbol] = _option(symbol, strike, ot)
        return rt

    async def _enqueue(self, rt: TradingRuntime, legs: list[dict]) -> None:
        for leg in legs:
            await rt.scheduler.enqueue(rt._intent_from_payload(leg))

    async def _drain(self, rt: TradingRuntime, expected_terminal: int) -> None:
        """Wait until the queue is empty, nothing is still tracking, and at
        least `expected_terminal` intents have reached a terminal state
        (filled or rejected). Bounded so a hang fails the test instead of
        blocking forever."""
        for _ in range(100):  # ~20s ceiling
            await asyncio.sleep(0.2)
            st = rt.scheduler.stats
            if (
                st["queue_depth"] == 0
                and st["tracking_orders"] == 0
                and st["processed"] + st["rejected"] >= expected_terminal
            ):
                return
        self.fail(f"scheduler did not settle: {rt.scheduler.stats}")

    def test_condor_round_trip_through_real_gate_fill_and_exit(self):
        async def run() -> None:
            rt = self._runtime()
            await rt.scheduler.start()
            try:
                # --- OPEN (hedge-first: buy the protective wings before the
                # short bodies, exactly as MultiLegManager sequences a condor).
                open_legs = [
                    {"symbol": "NIFTY24300CE", "side": "BUY", "price": 40.0, "quantity": 1,
                     "metadata": {"multi_leg_group": _GROUP}},
                    {"symbol": "NIFTY22700PE", "side": "BUY", "price": 35.0, "quantity": 1,
                     "metadata": {"multi_leg_group": _GROUP}},
                    {"symbol": "NIFTY24000CE", "side": "SELL", "price": 100.0, "quantity": 1,
                     "metadata": {"multi_leg_group": _GROUP}},
                    {"symbol": "NIFTY23000PE", "side": "SELL", "price": 90.0, "quantity": 1,
                     "metadata": {"multi_leg_group": _GROUP}},
                ]
                await self._enqueue(rt, open_legs)
                await self._drain(rt, expected_terminal=4)

                # Every leg cleared the real gate and filled — no silent reject.
                self.assertEqual(rt.scheduler.stats["rejected"], 0, rt.scheduler.stats)
                positions = {k: v.quantity for k, v in rt.portfolio.positions.items() if v.quantity != 0}
                self.assertEqual(
                    positions,
                    {"NIFTY24300CE": 1, "NIFTY22700PE": 1, "NIFTY24000CE": -1, "NIFTY23000PE": -1},
                    "a full four-leg iron condor must be open: two long wings, two short bodies",
                )
                # The integration seam with no prior single-flow coverage: a
                # fill must arm an ExitPlan so the position can never sit
                # unmanaged.
                self.assertGreaterEqual(
                    rt.exit_manager.active_plan_count, 1,
                    "opening fills must register/arm at least one ExitPlan",
                )
                self.assertEqual(rt.portfolio.realized_pnl, 0.0, "nothing realised while still open")

                # --- CLOSE (hedge-first again: buy back the shorts before
                # selling the wings so the book is never momentarily naked).
                # These are exits (opens_position False), so selling a long
                # wing to close is correctly NOT treated as a naked sale.
                close_legs = [
                    {"symbol": "NIFTY24000CE", "side": "BUY", "price": 20.0, "quantity": 1,
                     "priority": "TARGET", "metadata": {"opens_position": False, "multi_leg_group": _GROUP}},
                    {"symbol": "NIFTY23000PE", "side": "BUY", "price": 18.0, "quantity": 1,
                     "priority": "TARGET", "metadata": {"opens_position": False, "multi_leg_group": _GROUP}},
                    {"symbol": "NIFTY24300CE", "side": "SELL", "price": 8.0, "quantity": 1,
                     "priority": "TARGET", "metadata": {"opens_position": False, "multi_leg_group": _GROUP}},
                    {"symbol": "NIFTY22700PE", "side": "SELL", "price": 7.0, "quantity": 1,
                     "priority": "TARGET", "metadata": {"opens_position": False, "multi_leg_group": _GROUP}},
                ]
                await self._enqueue(rt, close_legs)
                await self._drain(rt, expected_terminal=8)

                self.assertEqual(rt.scheduler.stats["rejected"], 0, rt.scheduler.stats)
                remaining = {k: v.quantity for k, v in rt.portfolio.positions.items() if v.quantity != 0}
                self.assertEqual(remaining, {}, "the condor must be fully flat after the close")
                self.assertEqual(rt.monitor.filled_orders, 8, "all four opens and four closes filled")
                # Bought the shorts back cheaper than sold and sold the wings:
                # a profitable round-trip by construction, after charges.
                self.assertIsInstance(rt.portfolio.realized_pnl, float)
                self.assertGreater(
                    rt.portfolio.realized_pnl, 0.0,
                    "closing the condor for a net gain must realise positive P&L",
                )
            finally:
                await rt.scheduler.stop()

        asyncio.run(run())

    def test_naked_short_without_group_is_rejected_by_the_real_gate(self):
        """Proves the safety spine is LIVE in this harness, so the round-trip
        test above cannot be passing merely because the gate was disabled: an
        opening short option with neither multi_leg_group nor hedged metadata
        must be rejected as a naked sale by the same final gate."""
        rt = self._runtime()
        naked = rt._intent_from_payload({"symbol": "NIFTY24000CE", "side": "SELL", "price": 100.0, "quantity": 1})
        self.assertEqual(naked.signal.side, Side.SELL)
        decision = rt.scheduler._run_final_gate(naked)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "naked_option_selling_blocked")

        # ...and the SAME short, tagged as a leg of a defined-risk structure
        # (what submit_multi_leg sets), is approved — the distinction the
        # round-trip relies on.
        covered = rt._intent_from_payload({
            "symbol": "NIFTY24000CE", "side": "SELL", "price": 100.0, "quantity": 1,
            "metadata": {"multi_leg_group": _GROUP},
        })
        self.assertTrue(rt.scheduler._run_final_gate(covered).approved)


if __name__ == "__main__":
    unittest.main()
