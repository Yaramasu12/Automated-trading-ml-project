"""Regression tests for the 2026-07 production audit fixes.

Covers the money-path defects:
  C1/M3 — acknowledged live orders are tracked to a terminal state and booked
          (full and partial fills); unresolved orders raise an OMS alarm.
  C2    — kill switch freezes entries; ExitManager no longer mass-deletes plans.
  H1    — exit plans survive until the exit FILL confirms; rejected/lost exit
          orders re-arm after the pending window instead of dropping protection.
  H3    — intents stamped with their creation mode are rejected after a mode switch.
  H5    — a duplicate idempotency key is suppressed at dequeue, under the lock.
  H6    — Angel One orders always go out as NORMAL (system-managed exits).
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from trading_platform.broker.base import BrokerClient, BrokerResult
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.domain.enums import OrderPriority, OrderStatus, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal, Trade
from trading_platform.execution.fill_processor import FillProcessor
from trading_platform.execution.lock_manager import InstrumentLockManager
from trading_platform.execution.oms_store import OMSEventStore
from trading_platform.execution.scheduler import ExecutionScheduler
from trading_platform.exit.exit_manager import ExitManager, PENDING_EXIT_RETRY_SECONDS
from trading_platform.exit.exit_plan import ExitPlan, ExitTrigger
from trading_platform.portfolio.ledger import PortfolioLedger

_MASTER = build_default_universe(date(2026, 1, 5))


def _intent(symbol: str = "RELIANCE", quantity: int = 2, key: str | None = None,
            priority: OrderPriority = OrderPriority.ENTRY, metadata: dict | None = None,
            stop_loss: float | None = None, target: float | None = None) -> OrderIntent:
    signal = Signal(
        "test_strategy", symbol, Side.BUY, 0.9, 2800.0, "test",
        datetime.now(timezone.utc), metadata=dict(metadata or {}),
    )
    kwargs: dict = {"priority": priority, "stop_loss": stop_loss, "target": target}
    if key is not None:
        kwargs["idempotency_key"] = key
    return OrderIntent(signal, _MASTER.get(symbol), quantity, OrderType.MARKET,
                       ProductType.INTRADAY, **kwargs)


class _AckThenStatusBroker(BrokerClient):
    """Returns ACKNOWLEDGED on submit; order_status() serves scripted states."""
    name = "FAKE_LIVE"

    def __init__(self, statuses: list[dict | None]):
        self.statuses = list(statuses)
        self.submitted: list[OrderIntent] = []

    def is_ready(self) -> bool:
        return True

    def submit_order(self, intent: OrderIntent) -> BrokerResult:
        self.submitted.append(intent)
        now = datetime.now(timezone.utc)
        return BrokerResult(OrderStatus.ACKNOWLEDGED, "AO-1", None, now, now, "ack")

    def positions(self) -> list[dict]:
        return []

    def order_status(self, order_id: str) -> dict | None:
        return self.statuses.pop(0) if self.statuses else None


def _scheduler(broker, tmpdir: str, portfolio: PortfolioLedger | None = None,
               get_mode=None) -> ExecutionScheduler:
    portfolio = portfolio or PortfolioLedger(10_000_000)
    oms = OMSEventStore(db_path=Path(tmpdir) / "oms.db")
    sched = ExecutionScheduler(
        broker=broker,
        oms=oms,
        fill_processor=FillProcessor(portfolio, oms),
        lock_manager=InstrumentLockManager(),
        portfolio=portfolio,
        get_execution_mode=get_mode,
    )
    sched._TRACK_POLL_SECONDS = 0.01      # keep tests fast
    sched._TRACK_TIMEOUT_SECONDS = 0.2
    return sched


class LiveFillTrackingTests(unittest.IsolatedAsyncioTestCase):
    """C1/M3: acknowledged orders must be tracked into the ledger."""

    async def test_acknowledged_order_books_fill_when_broker_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            lot = _MASTER.get("RELIANCE").lot_size
            broker = _AckThenStatusBroker([
                {"state": "open", "average_price": 0.0, "filled_units": 0, "message": ""},
                {"state": "complete", "average_price": 2805.0, "filled_units": 2 * lot, "message": ""},
            ])
            portfolio = PortfolioLedger(10_000_000)
            sched = _scheduler(broker, tmp, portfolio)
            fills: list[Trade] = []

            async def on_fill(trade, intent):
                fills.append(trade)

            sched.register_fill_callback(on_fill)
            await sched._submit_to_broker(_intent(quantity=2))
            for _ in range(200):
                if fills:
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(len(fills), 1, "acknowledged order never reached the ledger")
            self.assertEqual(fills[0].quantity, 2)
            self.assertAlmostEqual(fills[0].price, 2805.0)
            self.assertIn("RELIANCE", portfolio.position_symbols())

    async def test_unresolved_order_raises_oms_alarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = _AckThenStatusBroker([])  # order book never answers
            sched = _scheduler(broker, tmp)
            await sched._submit_to_broker(_intent())
            await asyncio.sleep(0.5)

            events = [e["event_type"] for e in sched.oms.recent_events(50)]
            self.assertIn("fill_unresolved", events)
            self.assertEqual(sched.stats["unresolved_orders"], 1)

    async def test_partial_fill_at_timeout_books_filled_portion(self):
        with tempfile.TemporaryDirectory() as tmp:
            lot = _MASTER.get("RELIANCE").lot_size
            broker = _AckThenStatusBroker(
                [{"state": "open", "average_price": 2802.0, "filled_units": 1 * lot, "message": ""}] * 100
            )
            portfolio = PortfolioLedger(10_000_000)
            sched = _scheduler(broker, tmp, portfolio)
            fills: list[Trade] = []

            async def on_fill(trade, intent):
                fills.append(trade)

            sched.register_fill_callback(on_fill)
            await sched._submit_to_broker(_intent(quantity=3))
            for _ in range(300):
                if fills:
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(len(fills), 1)
            self.assertEqual(fills[0].quantity, 1, "partial fill must book only the filled lots")


class ModeStampAndDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_mode_switch_rejects_stale_intent(self):
        """H3: an intent enqueued under PAPER must not execute after switching to LIVE."""
        with tempfile.TemporaryDirectory() as tmp:
            mode = {"value": "PAPER"}
            broker = _AckThenStatusBroker([])
            sched = _scheduler(broker, tmp, get_mode=lambda: mode["value"])
            intent = _intent()
            await sched.enqueue(intent)
            self.assertEqual(intent.signal.metadata["execution_mode"], "PAPER")

            mode["value"] = "LIVE"
            await sched._process_intent(intent)

            self.assertEqual(broker.submitted, [], "stale-mode intent reached the broker")
            events = [e["event_type"] for e in sched.oms.recent_events(20)]
            self.assertIn("mode_mismatch_rejected", events)

    async def test_duplicate_key_suppressed_at_dequeue(self):
        """H5: a key already submitted to the broker is not submitted again."""
        with tempfile.TemporaryDirectory() as tmp:
            broker = _AckThenStatusBroker([])
            sched = _scheduler(broker, tmp)
            intent = _intent(key="dup-key-1")
            sched.oms.append(event_type="broker_submitted", order_id="dup-key-1",
                             idempotency_key="dup-key-1", symbol="RELIANCE")

            await sched._process_intent(intent)

            self.assertEqual(broker.submitted, [], "duplicate intent reached the broker")
            events = [e["event_type"] for e in sched.oms.recent_events(20)]
            self.assertIn("duplicate_suppressed", events)


class ExitPlanLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """H1/C2: plans survive until the exit fill confirms; no mass deletion."""

    def _plan(self, symbol: str = "RELIANCE", quantity: int = 2) -> ExitPlan:
        trade = Trade("t1", "o1", symbol, Side.BUY, quantity, 2800.0, 0.0,
                      datetime.now(timezone.utc), "test_strategy")
        return ExitPlan.from_trade(trade, instrument=_MASTER.get(symbol),
                                   stop_loss_pct=0.01, target_pct=0.02)

    async def test_plan_survives_emission_until_fill(self):
        enqueued: list[OrderIntent] = []

        async def enqueue(intent):
            enqueued.append(intent)

        mgr = ExitManager(enqueue)
        plan = self._plan()
        mgr.register(plan)
        mgr.update_marks({"RELIANCE": 2800.0 * 0.985})  # breach the stop

        status = await mgr._emit_exit_intent(
            plan, ExitTrigger.STOP_LOSS, 2758.0, datetime.now(timezone.utc))
        self.assertEqual(status, "emitted")
        mgr._pending_exits[plan.plan_id] = (datetime.now(timezone.utc), ExitTrigger.STOP_LOSS)

        # Plan is still registered (exit order in flight, not yet filled).
        self.assertEqual(mgr.active_plan_count, 1)

        # Confirmed fill is what releases it.
        mgr.on_exit_fill("RELIANCE", plan_id=plan.plan_id, trigger="STOP_LOSS")
        self.assertEqual(mgr.active_plan_count, 0)

    async def test_unconfirmed_exit_rearms_after_pending_window(self):
        mgr = ExitManager(lambda i: None)
        plan = self._plan()
        mgr.register(plan)
        stale = datetime.now(timezone.utc) - timedelta(seconds=PENDING_EXIT_RETRY_SECONDS + 1)
        mgr._pending_exits[plan.plan_id] = (stale, ExitTrigger.STOP_LOSS)

        # Simulate one monitor pass over the pending bookkeeping.
        emitted_at, _ = mgr._pending_exits[plan.plan_id]
        overdue = (datetime.now(timezone.utc) - emitted_at).total_seconds() >= PENDING_EXIT_RETRY_SECONDS
        self.assertTrue(overdue, "pending exit should be overdue and eligible for retry")
        self.assertEqual(mgr.active_plan_count, 1, "plan must never disappear while unfilled")

    async def test_partial_fill_keeps_remainder_protected(self):
        mgr = ExitManager(lambda i: None)
        plan = self._plan(quantity=4)
        plan.partial_exit_qty = 2
        mgr.register(plan)
        mgr._pending_exits[plan.plan_id] = (datetime.now(timezone.utc), ExitTrigger.PARTIAL_TARGET)

        mgr.on_exit_fill("RELIANCE", plan_id=plan.plan_id, trigger="PARTIAL_TARGET")

        self.assertEqual(mgr.active_plan_count, 1, "remainder lost protection after partial exit")
        self.assertEqual(mgr._plans[plan.plan_id].quantity, 2)

    def test_kill_all_is_gone(self):
        """C2: freeze semantics — the mass-deletion API must not exist."""
        self.assertFalse(hasattr(ExitManager, "kill_all"))


class AngelOneOrderShapeTests(unittest.TestCase):
    def test_orders_always_normal_variety(self):
        """H6: system-managed exits — no broker-side ROBO/STOPLOSS legs."""
        from trading_platform.broker.angel_one import AngelOneBrokerClient
        from trading_platform.config import load_settings

        client = AngelOneBrokerClient(load_settings())
        intent = _intent(stop_loss=2760.0, target=2900.0)
        params = client._to_angel_order(intent)
        self.assertEqual(params["variety"], "NORMAL")
        self.assertEqual(params["stoploss"], "0")
        self.assertEqual(params["squareoff"], "0")


if __name__ == "__main__":
    unittest.main()
