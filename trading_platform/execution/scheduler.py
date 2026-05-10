from __future__ import annotations

import asyncio
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
        self._queue: asyncio.PriorityQueue[PrioritizedOrderIntent] = asyncio.PriorityQueue(
            maxsize=max_queue_size
        )
        self._running = False
        self._worker_task: asyncio.Task | None = None
        self._fill_callbacks: list[FillCallback] = []
        self.kill_switch_active = False
        self._processed = 0
        self._rejected = 0

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

        seq = next(_seq_counter)
        item = PrioritizedOrderIntent(priority=int(intent.priority), seq=seq, intent=intent)
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

        # Capital protection check (requires live portfolio snapshot)
        if self.capital_protection and self.portfolio and intent.priority >= OrderPriority.ENTRY:
            now = datetime.now(timezone.utc)
            snapshot = self.portfolio.mark_to_market(now, {intent.instrument.symbol: intent.signal.price})
            cap_result = self.capital_protection.check(intent, snapshot)
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

        if result.status == OrderStatus.FILLED and result.average_price is not None:
            order.filled_at = now
            charges = self.charges_model.estimate(intent, result.average_price) if self.charges_model else 0.0
            trade = self.fill_processor.process(order, result.average_price, intent.quantity, charges, now)
            self.oms.append(
                event_type="broker_filled",
                order_id=intent.idempotency_key,
                symbol=intent.instrument.symbol,
                fill_price=result.average_price,
                fill_qty=intent.quantity,
                broker_order_id=result.broker_order_id,
                metadata={
                    "trace_id": intent.signal.metadata.get("trace_id", ""),
                    "raw": getattr(result, "raw", None),
                },
            )
            self._publish(
                "order.filled.v1",
                {
                    "idempotency_key": intent.idempotency_key,
                    "symbol": intent.instrument.symbol,
                    "fill_price": result.average_price,
                    "fill_qty": intent.quantity,
                    "broker_order_id": result.broker_order_id,
                },
                "fills",
            )
            self._processed += 1
            for cb in self._fill_callbacks:
                try:
                    await cb(trade, intent)
                except Exception as exc:
                    logger.exception("Fill callback error: %s", exc)

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
            "kill_switch_active": self.kill_switch_active,
            "locked_symbols": self.lock_manager.locked_symbols,
        }

    def _publish(self, event_name: str, payload: dict, stream: str) -> None:
        if self.event_bus is not None:
            self.event_bus.publish(event_name, payload, stream)
