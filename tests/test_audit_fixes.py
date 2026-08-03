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
import contextlib
import tempfile
import time
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


@contextlib.contextmanager
def _scheduler_in_tmpdir(broker, portfolio: PortfolioLedger | None = None, get_mode=None):
    """Scheduler on a throwaway OMS db, closed before the directory is removed.

    Windows refuses to unlink a file that still has an open handle, so leaving the
    SQLite connection open makes TemporaryDirectory cleanup raise PermissionError
    and masks whatever the test actually asserted.  POSIX allows the unlink, which
    is why CI never sees it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        sched = _scheduler(broker, tmp, portfolio, get_mode)
        try:
            yield sched
        finally:
            sched.oms.close()


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    """Poll `predicate` on a wall-clock budget until it is true or `timeout` passes.

    Counting iterations of asyncio.sleep(0.01) is not a time budget: on Windows the
    loop returns early enough that a nominal "3 second" wait of 300 iterations can
    elapse in a fraction of that, racing the scheduler's own _TRACK_TIMEOUT_SECONDS
    deadline.  time.monotonic() is the clock the deadline is really measured against.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)


class LiveFillTrackingTests(unittest.IsolatedAsyncioTestCase):
    """C1/M3: acknowledged orders must be tracked into the ledger."""

    async def test_acknowledged_order_books_fill_when_broker_completes(self):
        lot = _MASTER.get("RELIANCE").lot_size
        broker = _AckThenStatusBroker([
            {"state": "open", "average_price": 0.0, "filled_units": 0, "message": ""},
            {"state": "complete", "average_price": 2805.0, "filled_units": 2 * lot, "message": ""},
        ])
        portfolio = PortfolioLedger(10_000_000)
        with _scheduler_in_tmpdir(broker, portfolio) as sched:
            fills: list[Trade] = []

            async def on_fill(trade, intent):
                fills.append(trade)

            sched.register_fill_callback(on_fill)
            await sched._submit_to_broker(_intent(quantity=2))
            await _wait_for(lambda: bool(fills))

            self.assertEqual(len(fills), 1, "acknowledged order never reached the ledger")
            self.assertEqual(fills[0].quantity, 2)
            self.assertAlmostEqual(fills[0].price, 2805.0)
            self.assertIn("RELIANCE", portfolio.position_symbols())

    async def test_unresolved_order_raises_oms_alarm(self):
        broker = _AckThenStatusBroker([])  # order book never answers
        with _scheduler_in_tmpdir(broker) as sched:
            await sched._submit_to_broker(_intent())
            await _wait_for(
                lambda: any(e["event_type"] == "fill_unresolved"
                            for e in sched.oms.recent_events(50))
            )

            events = [e["event_type"] for e in sched.oms.recent_events(50)]
            self.assertIn("fill_unresolved", events)
            self.assertEqual(sched.stats["unresolved_orders"], 1)

    async def test_partial_fill_at_timeout_books_filled_portion(self):
        lot = _MASTER.get("RELIANCE").lot_size
        broker = _AckThenStatusBroker(
            [{"state": "open", "average_price": 2802.0, "filled_units": 1 * lot, "message": ""}] * 100
        )
        portfolio = PortfolioLedger(10_000_000)
        with _scheduler_in_tmpdir(broker, portfolio) as sched:
            fills: list[Trade] = []

            async def on_fill(trade, intent):
                fills.append(trade)

            sched.register_fill_callback(on_fill)
            await sched._submit_to_broker(_intent(quantity=3))
            await _wait_for(lambda: bool(fills))

            self.assertEqual(len(fills), 1)
            self.assertEqual(fills[0].quantity, 1, "partial fill must book only the filled lots")


class ModeStampAndDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_mode_switch_rejects_stale_intent(self):
        """H3: an intent enqueued under PAPER must not execute after switching to LIVE."""
        mode = {"value": "PAPER"}
        broker = _AckThenStatusBroker([])
        with _scheduler_in_tmpdir(broker, get_mode=lambda: mode["value"]) as sched:
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
        broker = _AckThenStatusBroker([])
        with _scheduler_in_tmpdir(broker) as sched:
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

    async def test_exit_intent_product_type_matches_position(self):
        """Regression 2026-08-03: closing a CARRYFORWARD (options) position
        with an INTRADAY exit does not net out at the broker — the exit must
        submit with the same product type the position was held under."""
        from trading_platform.domain.enums import Segment

        enqueued: list[OrderIntent] = []

        async def enqueue(intent):
            enqueued.append(intent)

        # RELIANCE (equity) — closes must stay INTRADAY.
        equity_plan = self._plan()
        mgr = ExitManager(enqueue)
        mgr.register(equity_plan)
        status = await mgr._emit_exit_intent(
            equity_plan, ExitTrigger.STOP_LOSS, 2758.0, datetime.now(timezone.utc))
        self.assertEqual(status, "emitted")
        self.assertEqual(enqueued[-1].product_type, ProductType.INTRADAY)

        # A NIFTY option — closes must be CARRYFORWARD.
        opt_symbol = next(i.symbol for i in _MASTER.by_underlying("NIFTY", Segment.OPTIONS))
        trade = Trade("t2", "o2", opt_symbol, Side.SELL, 1, 100.0, 0.0,
                      datetime.now(timezone.utc), "short_vol_condor")
        opt_plan = ExitPlan.from_trade(trade, instrument=_MASTER.get(opt_symbol),
                                       expiry_date=date(2100, 1, 7))
        mgr.register(opt_plan)
        status = await mgr._emit_exit_intent(
            opt_plan, ExitTrigger.EXPIRY, 5.0, datetime.now(timezone.utc))
        self.assertEqual(status, "emitted")
        self.assertEqual(enqueued[-1].product_type, ProductType.CARRYFORWARD)

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


class SqliteJournalModeTests(unittest.TestCase):
    """Regression 2026-07-29: OMSEventStore and PaperLearningJournal both use
    journal_mode=WAL, which requires a shared-memory-mapped -shm sidecar file
    that doesn't reliably open over a Docker Desktop Windows bind mount when
    written concurrently from two containers (trading-api + scheduler, both
    mounting ./data). Every real write 500'd with "sqlite3.OperationalError:
    unable to open database file" raised from the PRAGMA journal_mode=WAL
    line itself — not from sqlite3.connect(), which succeeded. Switched to
    DELETE, which needs no shared memory mapping."""

    def test_oms_event_store_uses_delete_journal_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OMSEventStore(Path(tmp) / "oms_events.db")
            try:
                mode = store._conn().execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(mode.lower(), "delete")
            finally:
                store.close()

    def test_paper_learning_journal_uses_delete_journal_mode(self):
        from trading_platform.trace.learning_journal import PaperLearningJournal

        with tempfile.TemporaryDirectory() as tmp:
            journal = PaperLearningJournal(Path(tmp) / "paper_learning_journal.db")
            try:
                mode = journal._conn().execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(mode.lower(), "delete")
            finally:
                journal.close()

    def test_oms_checkpoint_is_a_safe_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OMSEventStore(Path(tmp) / "oms_events.db")
            try:
                store.checkpoint()  # must not raise under DELETE journal mode
            finally:
                store.close()

    def test_trading_database_sqlite_uses_delete_journal_mode(self):
        from trading_platform.data.persistence import TradingDatabase

        with tempfile.TemporaryDirectory() as tmp:
            db = TradingDatabase(db_path=Path(tmp) / "trading.db")
            try:
                mode = db._sqlite_conn().execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(mode.lower(), "delete")
                db.checkpoint()  # must not raise under DELETE journal mode
            finally:
                db.close()

    def test_trace_store_and_trading_database_agree_on_journal_mode(self):
        # Regression 2026-07-29: TraceStore and TradingDatabase both default
        # to (and, in TradingRuntime.__init__, are BOTH constructed against)
        # the same physical file, data/trading.db. TraceStore was missed in
        # the WAL->DELETE fix, so each one fought to set a different journal
        # mode on its own connection to the same file — which requires
        # exclusive access — and every single TradingRuntime() construction
        # 500'd with "sqlite3.OperationalError: database is locked" as a
        # result. They must always agree.
        from trading_platform.data.persistence import TradingDatabase
        from trading_platform.trace.store import TraceStore

        with tempfile.TemporaryDirectory() as tmp:
            shared_path = Path(tmp) / "trading.db"
            db = TradingDatabase(db_path=shared_path)
            trace_store = TraceStore(base_dir=Path(tmp) / "traces", db_path=shared_path)
            try:
                db_mode = db._sqlite_conn().execute("PRAGMA journal_mode").fetchone()[0]
                trace_mode = trace_store._db_conn().execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(db_mode.lower(), trace_mode.lower())
                # The actual failure mode: a write through either store must
                # not deadlock against the other's open connection.
                db.save_risk_event(event_type="test", reason="regression check")
            finally:
                db.close()
                # TraceStore has no close() — release its connection directly
                # so Windows can clean up the TemporaryDirectory afterwards
                # (an open sqlite handle inside the tempdir otherwise makes
                # shutil.rmtree fail with a confusing NotADirectoryError).
                trace_store._db_conn().close()


if __name__ == "__main__":
    unittest.main()
