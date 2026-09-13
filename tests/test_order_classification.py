"""Regression tests for audit_results/2026-09-13/AUDIT_REPORT.md finding #1
(critical): every enforcement point trusted the caller-supplied
`opens_position` signal metadata as-is. Reproduced live
(audit_results/2026-09-13/probes.py): on a FLAT account with the kill switch
active, a BUY of 1,000 RELIANCE tagged opens_position=False and TARGET
priority was queued and filled, growing the position from zero while the
kill switch stayed active.

Covers the shared derivation (domain/order_classification.py), RiskEngine's
use of it, and the scheduler-level end-to-end reproduction of the audit's
own exact scenario.
"""
from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path

from trading_platform.broker.base import BrokerClient, BrokerResult
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.domain.enums import ExecutionMode, OrderPriority, OrderStatus, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal
from trading_platform.domain.order_classification import derive_true_opens_position
from trading_platform.execution.fill_processor import FillProcessor
from trading_platform.execution.lock_manager import InstrumentLockManager
from trading_platform.execution.oms_store import OMSEventStore
from trading_platform.execution.scheduler import ExecutionScheduler
from trading_platform.portfolio.ledger import PortfolioLedger
from trading_platform.risk.engine import RiskEngine, RiskLimits

_MASTER = build_default_universe(date(2026, 1, 5))


def _intent(symbol: str = "RELIANCE", side: Side = Side.BUY, quantity: int = 1000,
            priority: OrderPriority = OrderPriority.TARGET,
            opens_position: bool = False) -> OrderIntent:
    signal = Signal(
        "audit_forged_close", symbol, side, 0.9, 1000.0, "test",
        datetime.now(timezone.utc), metadata={"opens_position": opens_position},
    )
    return OrderIntent(signal, _MASTER.get(symbol), quantity, OrderType.MARKET,
                       ProductType.INTRADAY, priority=priority)


class DeriveTrueOpensPositionTests(unittest.TestCase):
    """Pure unit tests for the shared derivation — no I/O."""

    def test_caller_claiming_opens_true_is_trusted_as_is(self):
        intent = _intent(opens_position=True)
        self.assertTrue(derive_true_opens_position(intent, held_quantity=0))
        self.assertTrue(derive_true_opens_position(intent, held_quantity=-1000))

    def test_flat_account_forged_close_is_reclassified_as_opening(self):
        # The audit's exact scenario: flat account, opens_position=False.
        intent = _intent(side=Side.BUY, quantity=1000, opens_position=False)
        self.assertTrue(derive_true_opens_position(intent, held_quantity=0))

    def test_same_direction_addition_is_opening_not_reducing(self):
        # Already long 500, BUY 1000 more "closing" -- this grows exposure.
        intent = _intent(side=Side.BUY, quantity=1000, opens_position=False)
        self.assertTrue(derive_true_opens_position(intent, held_quantity=500))

    def test_genuine_fully_covered_reduction_is_trusted(self):
        # Long 1000, SELL 1000 "closing" -- a real, fully-covered exit.
        intent = _intent(side=Side.SELL, quantity=1000, opens_position=False)
        self.assertFalse(derive_true_opens_position(intent, held_quantity=1000))

    def test_partial_reduction_within_held_quantity_is_trusted(self):
        # Long 1000, SELL 400 "closing" -- a partial, still fully-covered exit.
        intent = _intent(side=Side.SELL, quantity=400, opens_position=False)
        self.assertFalse(derive_true_opens_position(intent, held_quantity=1000))

    def test_reversal_beyond_held_quantity_is_opening(self):
        # Long only 500, SELL 1000 "closing" -- closes the 500 then opens a
        # fresh 500 short with no established risk basis. Treated as opening.
        intent = _intent(side=Side.SELL, quantity=1000, opens_position=False)
        self.assertTrue(derive_true_opens_position(intent, held_quantity=500))

    def test_short_side_symmetry(self):
        # Short 1000 (held_quantity negative), BUY 1000 "closing" -- covers it exactly.
        intent = _intent(side=Side.BUY, quantity=1000, opens_position=False)
        self.assertFalse(derive_true_opens_position(intent, held_quantity=-1000))
        # BUY 1000 while short only 400 more than covers -- reversal, opening.
        self.assertTrue(derive_true_opens_position(intent, held_quantity=-400))


class RiskEngineHeldQuantityTests(unittest.TestCase):
    def _snapshot(self):
        from trading_platform.portfolio.ledger import PortfolioSnapshot
        return PortfolioSnapshot(
            cash=1_000_000, equity=1_000_000, realized_pnl=0.0, unrealized_pnl=0.0,
            drawdown=0.0, peak_equity=1_000_000, open_positions=0,
        )

    def test_forged_close_on_flat_account_is_rejected_under_kill_switch(self):
        engine = RiskEngine(RiskLimits())
        intent = _intent(side=Side.BUY, quantity=1000, opens_position=False)
        decision = engine.evaluate(
            intent=intent, portfolio=self._snapshot(), now=datetime.now(timezone.utc),
            execution_mode=ExecutionMode.PAPER, kill_switch_active=True,
            held_quantity=0,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "kill_switch_active")

    def test_genuine_reduction_still_passes_under_kill_switch(self):
        engine = RiskEngine(RiskLimits())
        intent = _intent(side=Side.SELL, quantity=1000, opens_position=False)
        decision = engine.evaluate(
            intent=intent, portfolio=self._snapshot(), now=datetime.now(timezone.utc),
            execution_mode=ExecutionMode.PAPER, kill_switch_active=True,
            held_quantity=1000,
        )
        self.assertTrue(decision.approved, decision.reason)

    def test_missing_held_quantity_defaults_to_flat_fails_safe(self):
        # A caller that forgets to pass held_quantity gets the safe default
        # (0 / flat) rather than silently inheriting the old trust-the-flag
        # behavior.
        engine = RiskEngine(RiskLimits())
        intent = _intent(side=Side.BUY, quantity=1000, opens_position=False)
        decision = engine.evaluate(
            intent=intent, portfolio=self._snapshot(), now=datetime.now(timezone.utc),
            execution_mode=ExecutionMode.PAPER, kill_switch_active=True,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "kill_switch_active")


def _scheduler(portfolio: PortfolioLedger, tmpdir: str) -> ExecutionScheduler:
    class _FillingBroker(BrokerClient):
        name = "FAKE_PAPER"

        def is_ready(self) -> bool:
            return True

        def submit_order(self, intent: OrderIntent) -> BrokerResult:
            now = datetime.now(timezone.utc)
            return BrokerResult(OrderStatus.FILLED, "SIM-1", intent.signal.price, now, now, "filled")

        def positions(self) -> list[dict]:
            return []

        def order_status(self, order_id: str) -> dict | None:
            return None

        def cancel_order(self, broker_order_id: str) -> bool:
            return True

    oms = OMSEventStore(db_path=Path(tmpdir) / "oms.db")
    sched = ExecutionScheduler(
        broker=_FillingBroker(),
        oms=oms,
        fill_processor=FillProcessor(portfolio, oms),
        lock_manager=InstrumentLockManager(),
        portfolio=portfolio,
    )
    return sched


@contextlib.contextmanager
def _scheduler_in_tmpdir(portfolio: PortfolioLedger):
    with tempfile.TemporaryDirectory() as tmp:
        sched = _scheduler(portfolio, tmp)
        try:
            yield sched
        finally:
            sched.oms.close()


class SchedulerKillSwitchBypassRegressionTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end reproduction of the audit's own probes.py scenario at the
    scheduler layer (the actual enqueue -> kill-switch-check -> fill path)."""

    async def test_forged_close_on_flat_account_cannot_open_exposure_during_kill_switch(self):
        portfolio = PortfolioLedger(1_000_000)
        with _scheduler_in_tmpdir(portfolio) as sched:
            sched.kill_switch_active = True
            await sched.start()
            intent = _intent(side=Side.BUY, quantity=1000, priority=OrderPriority.TARGET,
                             opens_position=False)
            await sched.enqueue(intent)
            await asyncio.wait_for(sched._queue.join(), timeout=5)
            await sched.stop()

            # The audit's own failure mode: this used to fill and grow the
            # position to +1000 while the kill switch stayed active.
            position = portfolio.positions.get("RELIANCE")
            self.assertEqual(position.quantity if position else 0, 0)
            self.assertEqual(len(portfolio.trades), 0)
            self.assertTrue(sched.kill_switch_active)

    async def test_genuine_reduction_still_executes_during_kill_switch(self):
        # The kill switch must still let REAL reductions through -- this is
        # the behavior the forged-metadata path was trying to imitate.
        portfolio = PortfolioLedger(1_000_000)
        instrument = _MASTER.get("RELIANCE")
        signal = Signal("seed", "RELIANCE", Side.BUY, 0.9, 1000.0, "seed",
                        datetime.now(timezone.utc), metadata={})
        seed_intent = OrderIntent(signal, instrument, 1000, OrderType.MARKET, ProductType.INTRADAY)
        from trading_platform.domain.models import Order
        portfolio.apply_fill(Order(seed_intent, status=OrderStatus.FILLED), 1000.0,
                             datetime.now(timezone.utc), 0.0)
        self.assertEqual(portfolio.positions["RELIANCE"].quantity, 1000)

        with _scheduler_in_tmpdir(portfolio) as sched:
            sched.kill_switch_active = True
            await sched.start()
            intent = _intent(side=Side.SELL, quantity=1000, priority=OrderPriority.TARGET,
                             opens_position=False)
            await sched.enqueue(intent)
            await asyncio.wait_for(sched._queue.join(), timeout=5)
            await sched.stop()

            position = portfolio.positions.get("RELIANCE")
            self.assertEqual(position.quantity if position else 0, 0)

    async def test_reversal_sized_close_during_kill_switch_is_blocked(self):
        # Long only 500; a SELL 1000 "closing" order would close the 500 and
        # open a fresh 500 short -- the reversal remainder is opening and
        # must still be blocked by the kill switch.
        portfolio = PortfolioLedger(1_000_000)
        instrument = _MASTER.get("RELIANCE")
        signal = Signal("seed", "RELIANCE", Side.BUY, 0.9, 1000.0, "seed",
                        datetime.now(timezone.utc), metadata={})
        seed_intent = OrderIntent(signal, instrument, 500, OrderType.MARKET, ProductType.INTRADAY)
        from trading_platform.domain.models import Order
        portfolio.apply_fill(Order(seed_intent, status=OrderStatus.FILLED), 1000.0,
                             datetime.now(timezone.utc), 0.0)

        with _scheduler_in_tmpdir(portfolio) as sched:
            sched.kill_switch_active = True
            await sched.start()
            intent = _intent(side=Side.SELL, quantity=1000, priority=OrderPriority.TARGET,
                             opens_position=False)
            await sched.enqueue(intent)
            await asyncio.wait_for(sched._queue.join(), timeout=5)
            await sched.stop()

            # Position must be unchanged -- the whole order was blocked, not
            # partially filled down to a -500 short.
            self.assertEqual(portfolio.positions["RELIANCE"].quantity, 500)


if __name__ == "__main__":
    unittest.main()
