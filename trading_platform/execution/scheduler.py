from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Awaitable, TYPE_CHECKING

from trading_platform.broker.base import BrokerClient
from trading_platform.domain.enums import OrderPriority, OrderStatus
from trading_platform.domain.models import Order, OrderIntent, PrioritizedOrderIntent, Trade
from trading_platform.event_bus import InMemoryEventBus
from trading_platform.execution.fill_processor import FillProcessor
from trading_platform.execution.final_gate import FinalGateDecision, FinalGateFn
from trading_platform.execution.lock_manager import InstrumentLockManager
from trading_platform.execution.oms_store import OMSEventStore
from trading_platform.execution.rate_limiter import TokenBucketRateLimiter

if TYPE_CHECKING:
    from trading_platform.risk.compliance import ComplianceGuard
    from trading_platform.risk.capital_protection import CapitalProtection
    from trading_platform.risk.event_risk import EventRiskGuard
    from trading_platform.portfolio.ledger import PortfolioLedger
    from trading_platform.backtesting.charges import ChargesModel

logger = logging.getLogger(__name__)

_seq_counter = itertools.count()


@dataclass
class SchedulerResult:
    order: Order
    trade: Trade | None
    queued_at: str
    executed_at: str | None


FillCallback = Callable[[Trade, OrderIntent], Awaitable[None]]


class ExecutionScheduler:
    """Priority-based async execution engine.

    Strategies enqueue OrderIntents. The worker dequeues in priority order
    (kill-switch first, entry last), runs compliance → capital → event-risk
    checks, acquires the per-instrument lock, rate-limits, and submits to
    the broker.

    Fill callbacks (e.g. ExitManager.register) are called after every fill.
    """

    def __init__(
        self,
        broker: BrokerClient,
        oms: OMSEventStore,
        fill_processor: FillProcessor,
        lock_manager: InstrumentLockManager,
        rate_limiter: TokenBucketRateLimiter | None = None,
        compliance: ComplianceGuard | None = None,
        capital_protection: CapitalProtection | None = None,
        event_risk: EventRiskGuard | None = None,
        portfolio: PortfolioLedger | None = None,
        event_bus: InMemoryEventBus | None = None,
        charges_model: ChargesModel | None = None,
        final_gate: FinalGateFn | None = None,
        max_queue_size: int = 500,
        get_execution_mode: Callable[[], str] | None = None,
    ) -> None:
        self.broker = broker
        self.oms = oms
        self.fill_processor = fill_processor
        self.lock_manager = lock_manager
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter()
        self.compliance = compliance
        self.capital_protection = capital_protection
        self.event_risk = event_risk
        self.portfolio = portfolio
        self.event_bus = event_bus
        self.charges_model = charges_model
        self.final_gate = final_gate
        self.get_execution_mode = get_execution_mode
        self._queue: asyncio.PriorityQueue[PrioritizedOrderIntent] = asyncio.PriorityQueue(
            maxsize=max_queue_size
        )
        self._running = False
        self._worker_task: asyncio.Task | None = None
        self._tracking_tasks: set[asyncio.Task] = set()
        self._fill_callbacks: list[FillCallback] = []
        self.kill_switch_active = False
        self._processed = 0
        self._rejected = 0
        self._unresolved_orders = 0
        # Equity at session open — used to compute intraday P&L for the daily-loss circuit breaker.
        # Set once by the runtime at startup; resets each morning via set_session_start_equity().
        self._session_start_equity: float = 0.0

    def register_fill_callback(self, cb: FillCallback) -> None:
        self._fill_callbacks.append(cb)

    async def enqueue(self, intent: OrderIntent) -> str:
        """Place an intent on the priority queue. Returns event_id."""
        if self.kill_switch_active and not self._allowed_during_kill_switch(intent):
            self.oms.append(
                event_type="kill_switch_cancelled",
                order_id=intent.idempotency_key,
                idempotency_key=intent.idempotency_key,
                symbol=intent.instrument.symbol,
                strategy_name=intent.signal.strategy_name,
                rejection_reason="kill_switch_active",
                metadata={"trace_id": intent.signal.metadata.get("trace_id", "")},
            )
            self._publish(
                "kill_switch.triggered.v1",
                {
                    "idempotency_key": intent.idempotency_key,
                    "symbol": intent.instrument.symbol,
                    "strategy_name": intent.signal.strategy_name,
                    "reason": "kill_switch_active",
                },
                "control",
            )
            return intent.idempotency_key

        if self.oms.is_duplicate(intent.idempotency_key):
            return intent.idempotency_key

        # Run compliance check synchronously before queuing
        if self.compliance:
            result = self.compliance.check(intent)
            if not result.approved:
                self.oms.append(
                    event_type="compliance_rejected",
                    order_id=intent.idempotency_key,
                    idempotency_key=intent.idempotency_key,
                    symbol=intent.instrument.symbol,
                    strategy_name=intent.signal.strategy_name,
                    rejection_reason=result.reason,
                    metadata={"trace_id": intent.signal.metadata.get("trace_id", "")},
                )
                self._rejected += 1
                return intent.idempotency_key
            self.oms.append(
                event_type="compliance_approved",
                order_id=intent.idempotency_key,
                symbol=intent.instrument.symbol,
                metadata={"trace_id": intent.signal.metadata.get("trace_id", "")},
            )
            self._publish(
                "order.risk_approved.v1",
                {
                    "idempotency_key": intent.idempotency_key,
                    "symbol": intent.instrument.symbol,
                    "stage": "compliance",
                },
                "risk",
            )

        # Event risk: only block ENTRY-priority orders (exits always go through)
        if self.event_risk and intent.priority >= OrderPriority.ENTRY:
            er = self.event_risk.check()
            if er.blocked:
                self.oms.append(
                    event_type="compliance_rejected",
                    order_id=intent.idempotency_key,
                    idempotency_key=intent.idempotency_key,
                    symbol=intent.instrument.symbol,
                    rejection_reason=er.reason,
                    metadata={"trace_id": intent.signal.metadata.get("trace_id", "")},
                )
                self._rejected += 1
                return intent.idempotency_key

        # Stamp the execution mode the intent was created under (audit fix H3):
        # a hot mode switch (PAPER↔LIVE) swaps the broker while intents may
        # still sit in the queue; the worker rejects any intent whose stamped
        # mode no longer matches so a paper decision can never hit the real broker.
        if self.get_execution_mode is not None:
            intent.signal.metadata.setdefault("execution_mode", self.get_execution_mode())

        seq = next(_seq_counter)
        item = PrioritizedOrderIntent(priority=int(intent.priority), seq=seq, intent=intent)
        if intent.priority < OrderPriority.ENTRY:
            # Exit/protective orders must never block the ExitManager's monitor
            # loop behind a full queue (audit fix M4). Fail fast — the caller
            # keeps the plan armed and retries next poll cycle.
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                self.oms.append(
                    event_type="queue_full_exit_deferred",
                    order_id=intent.idempotency_key,
                    idempotency_key=intent.idempotency_key,
                    symbol=intent.instrument.symbol,
                    rejection_reason="queue_full",
                )
                raise RuntimeError(
                    f"execution queue full ({self._queue.maxsize}) — exit intent deferred"
                )
        else:
            await self._queue.put(item)

        event_id = self.oms.append(
            event_type="intent_queued",
            order_id=intent.idempotency_key,
            idempotency_key=intent.idempotency_key,
            symbol=intent.instrument.symbol,
            strategy_name=intent.signal.strategy_name,
            side=intent.signal.side.value,
            quantity=intent.quantity,
            price=intent.limit_price or intent.signal.price,
            priority=int(intent.priority),
            metadata={"trace_id": intent.signal.metadata.get("trace_id", "")},
        )
        self._publish(
            "order.intent_created.v1",
            {
                "event_id": event_id,
                "idempotency_key": intent.idempotency_key,
                "symbol": intent.instrument.symbol,
                "strategy_name": intent.signal.strategy_name,
                "side": intent.signal.side.value,
                "quantity": intent.quantity,
                "priority": int(intent.priority),
            },
            "order_intents",
        )
        return event_id

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker(), name="execution_scheduler")
        logger.info("ExecutionScheduler started")

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        for task in list(self._tracking_tasks):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("ExecutionScheduler stopped")

    async def _worker(self) -> None:
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            intent = item.intent
            try:
                await self._process_intent(intent)
            except Exception as exc:
                logger.exception("Scheduler error processing %s: %s", intent.idempotency_key, exc)
                self._rejected += 1
            finally:
                self._queue.task_done()

    async def _process_intent(self, intent: OrderIntent) -> None:
        symbol = intent.instrument.symbol

        # Audit fix H3: refuse intents created under a different execution mode.
        stamped_mode = intent.signal.metadata.get("execution_mode")
        current_mode = self.get_execution_mode() if self.get_execution_mode is not None else None
        if stamped_mode is not None and current_mode is not None and stamped_mode != current_mode:
            self.oms.append(
                event_type="mode_mismatch_rejected",
                order_id=intent.idempotency_key,
                idempotency_key=intent.idempotency_key,
                symbol=symbol,
                strategy_name=intent.signal.strategy_name,
                rejection_reason=f"intent created in {stamped_mode}, runtime now in {current_mode}",
            )
            self._publish(
                "order.mode_mismatch_rejected.v1",
                {"idempotency_key": intent.idempotency_key, "symbol": symbol,
                 "stamped_mode": stamped_mode, "current_mode": current_mode},
                "risk",
            )
            logger.error(
                "Mode-mismatch rejection: intent %s (%s) stamped %s but runtime is %s",
                intent.idempotency_key, symbol, stamped_mode, current_mode,
            )
            self._rejected += 1
            return

        # Capital protection check (requires live portfolio snapshot)
        opens_position = bool(intent.signal.metadata.get("opens_position", True))
        if self.capital_protection and self.portfolio and opens_position and intent.priority >= OrderPriority.ENTRY:
            now = datetime.now(timezone.utc)
            snapshot = self.portfolio.mark_to_market(now, {intent.instrument.symbol: intent.signal.price})
            # Compute real intraday P&L so the daily-loss circuit breaker fires correctly.
            # session_start_equity is set at market open; negative delta = today's loss.
            daily_pnl = (snapshot.equity - self._session_start_equity) if self._session_start_equity > 0 else 0.0
            cap_result = self.capital_protection.check(intent, snapshot, daily_pnl=daily_pnl)
            if not cap_result.approved:
                self.oms.append(
                    event_type="capital_check_failed",
                    order_id=intent.idempotency_key,
                    symbol=symbol,
                    rejection_reason=cap_result.reason,
                )
                self._rejected += 1
                return
            self.oms.append(
                event_type="capital_check_passed",
                order_id=intent.idempotency_key,
                symbol=symbol,
            )
            self._publish(
                "order.approved.v1",
                {
                    "idempotency_key": intent.idempotency_key,
                    "symbol": symbol,
                    "stage": "capital_protection",
                },
                "approved_orders",
            )

        final_decision = self._run_final_gate(intent)
        if not final_decision.approved:
            self.oms.append(
                event_type="risk_rejected",
                order_id=intent.idempotency_key,
                idempotency_key=intent.idempotency_key,
                symbol=symbol,
                strategy_name=intent.signal.strategy_name,
                side=intent.signal.side.value,
                quantity=intent.quantity,
                price=intent.limit_price or intent.signal.price,
                priority=int(intent.priority),
                rejection_reason=final_decision.reason,
                metadata=final_decision.to_dict(),
            )
            self._publish(
                "order.risk_rejected.v1",
                {
                    "idempotency_key": intent.idempotency_key,
                    "symbol": symbol,
                    "reason": final_decision.reason,
                    "stage": final_decision.stage,
                },
                "risk",
            )
            self._rejected += 1
            return
        if self.final_gate:
            self.oms.append(
                event_type="risk_approved",
                order_id=intent.idempotency_key,
                idempotency_key=intent.idempotency_key,
                symbol=symbol,
                strategy_name=intent.signal.strategy_name,
                metadata=final_decision.to_dict(),
            )

        await self.lock_manager.acquire(symbol)
        self.oms.append(
            event_type="lock_acquired",
            order_id=intent.idempotency_key,
            symbol=symbol,
        )
        try:
            # Audit fix H5: re-check idempotency under the instrument lock.
            # The enqueue-time check races: two intents with the same key can
            # both pass it before either reaches the broker. This check runs
            # serialized per instrument, immediately before submission.
            if self.oms.is_duplicate(intent.idempotency_key):
                self.oms.append(
                    event_type="duplicate_suppressed",
                    order_id=intent.idempotency_key,
                    idempotency_key=intent.idempotency_key,
                    symbol=symbol,
                    rejection_reason="already_submitted_to_broker",
                )
                return
            await self.rate_limiter.acquire()
            await self._submit_to_broker(intent)
        finally:
            self.lock_manager.release(symbol)
            self.oms.append(
                event_type="lock_released",
                order_id=intent.idempotency_key,
                symbol=symbol,
            )

    async def _submit_to_broker(self, intent: OrderIntent) -> None:
        now = datetime.now(timezone.utc)
        loop = asyncio.get_running_loop()

        result = await loop.run_in_executor(None, self.broker.submit_order, intent)

        order = Order(intent=intent)
        order.status = result.status
        order.broker_order_id = result.broker_order_id
        order.submitted_at = result.submitted_at
        order.acknowledged_at = result.acknowledged_at
        order.average_price = result.average_price

        self.oms.append(
            event_type="broker_submitted",
            order_id=intent.idempotency_key,
            idempotency_key=intent.idempotency_key,
            symbol=intent.instrument.symbol,
            strategy_name=intent.signal.strategy_name,
            broker_order_id=result.broker_order_id,
            metadata={
                "trace_id": intent.signal.metadata.get("trace_id", ""),
                "status": result.status.value,
                "message": getattr(result, "message", ""),
                "raw": getattr(result, "raw", None),
            },
        )
        self._publish(
            "order.submitted.v1",
            {
                "idempotency_key": intent.idempotency_key,
                "symbol": intent.instrument.symbol,
                "broker_order_id": result.broker_order_id,
                "status": result.status.value,
            },
            "execution",
        )

        if result.status == OrderStatus.FILLED and result.average_price is None:
            # Audit fix M2: a "filled" order with no price cannot be booked —
            # surfacing it beats silently dropping the fill from the ledger.
            logger.critical(
                "Broker reported FILLED with no average price for %s (%s) — fill NOT booked; reconcile manually",
                intent.idempotency_key, intent.instrument.symbol,
            )
            self._unresolved_orders += 1
            self.oms.append(
                event_type="fill_price_missing",
                order_id=intent.idempotency_key,
                idempotency_key=intent.idempotency_key,
                symbol=intent.instrument.symbol,
                broker_order_id=result.broker_order_id,
                rejection_reason="filled_without_average_price",
                metadata={"raw": getattr(result, "raw", None)},
            )
            self._publish(
                "order.fill_unresolved.v1",
                {"idempotency_key": intent.idempotency_key, "symbol": intent.instrument.symbol,
                 "broker_order_id": result.broker_order_id, "reason": "filled_without_average_price"},
                "execution",
            )
            return

        if result.status == OrderStatus.FILLED:
            await self._handle_fill(intent, order, result.average_price, intent.quantity, now)

        elif result.status in {OrderStatus.ACKNOWLEDGED, OrderStatus.SUBMITTED} and result.broker_order_id:
            # Audit fix C1: a live order acknowledged by the broker is NOT done.
            # Track it until a terminal state so the fill reaches the ledger,
            # exit plans get registered, and risk accounting stays truthful.
            task = asyncio.create_task(
                self._track_order_until_terminal(intent, order),
                name=f"track-order-{result.broker_order_id}",
            )
            self._tracking_tasks.add(task)
            task.add_done_callback(self._tracking_tasks.discard)

        elif result.status == OrderStatus.REJECTED:
            msg = result.message if hasattr(result, "message") else "broker_rejected"
            self.oms.append(
                event_type="broker_rejected",
                order_id=intent.idempotency_key,
                symbol=intent.instrument.symbol,
                rejection_reason=msg,
                metadata={
                    "trace_id": intent.signal.metadata.get("trace_id", ""),
                    "raw": getattr(result, "raw", None),
                },
            )
            self._publish(
                "order.rejected.v1",
                {
                    "idempotency_key": intent.idempotency_key,
                    "symbol": intent.instrument.symbol,
                    "reason": msg,
                },
                "execution",
            )
            self._rejected += 1

    async def _handle_fill(
        self,
        intent: OrderIntent,
        order: Order,
        fill_price: float,
        fill_qty: int,
        now: datetime,
        partial: bool = False,
    ) -> None:
        """Book a confirmed fill: ledger, OMS, event bus, fill callbacks."""
        order.filled_at = now
        charges = self.charges_model.estimate(intent, fill_price) if self.charges_model else 0.0
        if partial and fill_qty < intent.quantity:
            trade = self.fill_processor.process_partial(order, fill_price, fill_qty, charges, now)
        else:
            trade = self.fill_processor.process(order, fill_price, fill_qty, charges, now)
        self.oms.append(
            event_type="broker_filled",
            order_id=intent.idempotency_key,
            symbol=intent.instrument.symbol,
            fill_price=fill_price,
            fill_qty=fill_qty,
            broker_order_id=order.broker_order_id,
            metadata={
                "trace_id": intent.signal.metadata.get("trace_id", ""),
                "partial": partial,
            },
        )
        self._publish(
            "order.filled.v1",
            {
                "idempotency_key": intent.idempotency_key,
                "symbol": intent.instrument.symbol,
                "fill_price": fill_price,
                "fill_qty": fill_qty,
                "broker_order_id": order.broker_order_id,
                "partial": partial,
            },
            "fills",
        )
        self._processed += 1
        for cb in self._fill_callbacks:
            try:
                # Callbacks may be sync or async. Only await a coroutine —
                # awaiting a sync callback's None result threw on every fill
                # ("object NoneType can't be used in 'await' expression").
                result = cb(trade, intent)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.exception("Fill callback error: %s", exc)

    # Polling cadence for acknowledged (live) orders.
    _TRACK_POLL_SECONDS = 2.0
    _TRACK_TIMEOUT_SECONDS = 120.0

    async def _track_order_until_terminal(self, intent: OrderIntent, order: Order) -> None:
        """Poll the broker until an acknowledged order reaches a terminal state.

        Audit fix C1+M3. Runs on the event loop (broker calls via executor), so
        all bookkeeping happens exactly like an immediate fill. Outcomes:
          - complete  → book full fill (avg price, full quantity)
          - rejected/cancelled → OMS broker_rejected
          - partial fill at timeout → book the filled portion (fix M3)
          - unknown at timeout → CRITICAL alarm + fill_unresolved OMS event;
            the position may exist at the broker — reconcile before trading on.
        """
        broker_order_id = order.broker_order_id or ""
        symbol = intent.instrument.symbol
        lot_size = max(1, int(getattr(intent.instrument, "lot_size", 1) or 1))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._TRACK_TIMEOUT_SECONDS
        last_status: dict | None = None

        while loop.time() < deadline:
            await asyncio.sleep(self._TRACK_POLL_SECONDS)
            status_fn = getattr(self.broker, "order_status", None)
            if status_fn is None:
                break
            try:
                last_status = await loop.run_in_executor(None, status_fn, broker_order_id)
            except Exception as exc:
                logger.warning("Order-status poll error for %s: %s", broker_order_id, exc)
                continue
            if not last_status:
                continue
            state = str(last_status.get("state", "")).lower()
            if state == "complete":
                avg_price = float(last_status.get("average_price") or 0.0)
                filled_units = int(last_status.get("filled_units") or 0)
                fill_lots = (filled_units // lot_size) if filled_units else intent.quantity
                if avg_price <= 0:
                    break  # falls through to unresolved alarm below
                await self._handle_fill(
                    intent, order, avg_price, max(fill_lots, 1),
                    datetime.now(timezone.utc),
                    partial=fill_lots < intent.quantity,
                )
                return
            if state in {"rejected", "cancelled"}:
                self.oms.append(
                    event_type="broker_rejected",
                    order_id=intent.idempotency_key,
                    symbol=symbol,
                    broker_order_id=broker_order_id,
                    rejection_reason=str(last_status.get("message", state)),
                )
                self._publish(
                    "order.rejected.v1",
                    {"idempotency_key": intent.idempotency_key, "symbol": symbol,
                     "broker_order_id": broker_order_id, "reason": state},
                    "execution",
                )
                self._rejected += 1
                return
            # still open/pending — keep polling

        # Timeout (or no order_status support / bad data): book whatever filled.
        filled_units = int((last_status or {}).get("filled_units") or 0)
        fill_lots = filled_units // lot_size
        avg_price = float((last_status or {}).get("average_price") or 0.0)
        if fill_lots >= intent.quantity and avg_price > 0:
            # Fully filled — we just learned it late. Book normally, no alarm.
            await self._handle_fill(
                intent, order, avg_price, intent.quantity, datetime.now(timezone.utc)
            )
            return
        if fill_lots > 0 and avg_price > 0:
            logger.critical(
                "Order %s (%s) PARTIALLY filled %d/%d lot(s) at timeout — booking partial, remainder unresolved",
                broker_order_id, symbol, fill_lots, intent.quantity,
            )
            await self._handle_fill(
                intent, order, avg_price, fill_lots,
                datetime.now(timezone.utc), partial=True,
            )
        logger.critical(
            "Order %s (%s) fill status UNRESOLVED after %.0fs — position may exist at broker "
            "without ledger/exit tracking. Run POST /execution/reconcile before further trading.",
            broker_order_id, symbol, self._TRACK_TIMEOUT_SECONDS,
        )
        self._unresolved_orders += 1
        self.oms.append(
            event_type="fill_unresolved",
            order_id=intent.idempotency_key,
            idempotency_key=intent.idempotency_key,
            symbol=symbol,
            broker_order_id=broker_order_id,
            rejection_reason="no_terminal_state_within_timeout",
            metadata={"last_status": last_status},
        )
        self._publish(
            "order.fill_unresolved.v1",
            {"idempotency_key": intent.idempotency_key, "symbol": symbol,
             "broker_order_id": broker_order_id},
            "execution",
        )

    def set_session_start_equity(self, equity: float) -> None:
        """Record equity at market open so intraday P&L can be computed for the daily-loss circuit breaker."""
        self._session_start_equity = equity

    def update_broker(self, broker: BrokerClient) -> None:
        """Hot-swap broker when execution mode changes PAPER ↔ LIVE."""
        self.broker = broker

    def _run_final_gate(self, intent: OrderIntent) -> FinalGateDecision:
        if self.final_gate is None:
            return FinalGateDecision.approve(details={"configured": False})
        now = datetime.now(timezone.utc)
        return self.final_gate(intent, now, None)

    @staticmethod
    def _allowed_during_kill_switch(intent: OrderIntent) -> bool:
        if intent.priority == OrderPriority.KILL_SWITCH:
            return True
        opens_position = bool(intent.signal.metadata.get("opens_position", True))
        return not opens_position

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def stats(self) -> dict:
        return {
            "running": self._running,
            "queue_depth": self.queue_depth,
            "processed": self._processed,
            "rejected": self._rejected,
            "unresolved_orders": self._unresolved_orders,
            "tracking_orders": len(self._tracking_tasks),
            "kill_switch_active": self.kill_switch_active,
            "locked_symbols": self.lock_manager.locked_symbols,
        }

    def _publish(self, event_name: str, payload: dict, stream: str) -> None:
        if self.event_bus is not None:
            self.event_bus.publish(event_name, payload, stream)
