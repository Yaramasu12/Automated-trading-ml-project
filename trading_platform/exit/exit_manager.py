from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable

from trading_platform.domain.enums import OrderPriority, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal, Trade
from trading_platform.exit.exit_plan import ExitPlan, ExitTrigger

logger = logging.getLogger(__name__)

ExitEnqueueFn = Callable[[OrderIntent], Awaitable[None]]


class ExitManager:
    """Background monitor that tracks active ExitPlans and emits exit intents.

    After every entry fill the scheduler calls `register(plan)`.
    The monitor loop polls live prices and enqueues exit OrderIntents
    via the provided enqueue function (pointing at ExecutionScheduler.enqueue).

    Mark price priority:
      1. Live tick from live feed (most accurate)
      2. Externally supplied mark via update_marks()
      3. Last known mark price for the symbol (sticky — avoids silent skip)
      4. Entry price (worst case — at least SL/target can still check on large moves)

    Without this fallback, paper-mode positions where the live feed has no tick
    for the instrument are NEVER closed, causing unlimited unrealized losses.
    """

    PRIORITY_MAP: dict[ExitTrigger, OrderPriority] = {
        ExitTrigger.KILL_SWITCH: OrderPriority.KILL_SWITCH,
        ExitTrigger.STOP_LOSS: OrderPriority.STOP_LOSS,
        ExitTrigger.EXPIRY: OrderPriority.EXPIRY_EXIT,
        ExitTrigger.TARGET: OrderPriority.TARGET,
        ExitTrigger.TRAILING_STOP: OrderPriority.TRAILING_STOP,
        ExitTrigger.MANUAL: OrderPriority.EMERGENCY_EXIT,
    }

    def __init__(self, enqueue_fn: ExitEnqueueFn, poll_interval: float = 1.0) -> None:
        self._enqueue = enqueue_fn
        self.poll_interval = poll_interval
        self._plans: dict[str, ExitPlan] = {}
        self._mark_prices: dict[str, float] = {}
        self._last_known_marks: dict[str, float] = {}   # sticky prices for fallback
        self._running = False
        self._task: asyncio.Task | None = None

    def register(self, plan: ExitPlan) -> None:
        self._plans[plan.plan_id] = plan
        # Seed last-known price from entry so the monitor has a fallback immediately
        if plan.entry_price > 0:
            self._last_known_marks.setdefault(plan.symbol, plan.entry_price)
        logger.debug("ExitPlan registered: %s for %s", plan.plan_id, plan.symbol)

    def deregister(self, plan_id: str) -> None:
        self._plans.pop(plan_id, None)

    def update_marks(self, prices: dict[str, float]) -> None:
        self._mark_prices.update(prices)
        # Keep sticky last-known for fallback when live feed goes stale
        self._last_known_marks.update({k: v for k, v in prices.items() if v > 0})

    def kill_all(self) -> None:
        """Mark all active plans as kill-switch triggered (no broker call needed)."""
        now = datetime.now(timezone.utc)
        for plan in self._plans.values():
            if plan.active:
                plan.mark_triggered(ExitTrigger.KILL_SWITCH, now)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop(), name="exit_manager")
        logger.info("ExitManager started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ExitManager stopped")

    async def _monitor_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.poll_interval)
            now = datetime.now(timezone.utc)
            triggered: list[str] = []
            for plan_id, plan in list(self._plans.items()):
                if not plan.active:
                    continue
                # Mark price resolution: live tick → cached update → sticky last-known → entry price
                mark = (
                    self._mark_prices.get(plan.symbol)
                    or self._last_known_marks.get(plan.symbol)
                    or plan.entry_price  # fallback to entry — at least SL/target can check
                )
                if not mark or mark <= 0:
                    continue
                trigger = plan.check_trigger(mark, now)
                if trigger:
                    plan.mark_triggered(trigger, now)
                    triggered.append(plan_id)
                    try:
                        await self._emit_exit_intent(plan, trigger, mark, now)
                    except Exception as exc:
                        logger.exception("Failed to emit exit for plan %s: %s", plan_id, exc)
            for plan_id in triggered:
                self._plans.pop(plan_id, None)

    async def _emit_exit_intent(
        self, plan: ExitPlan, trigger: ExitTrigger, price: float, now: datetime
    ) -> None:
        if plan.instrument is None:
            logger.warning("ExitPlan %s has no instrument — cannot emit exit intent", plan.plan_id)
            return

        exit_side = Side.SELL if plan.side == "BUY" else Side.BUY
        priority = self.PRIORITY_MAP.get(trigger, OrderPriority.TARGET)

        signal = Signal(
            strategy_name=f"exit_manager:{trigger.value.lower()}",
            symbol=plan.symbol,
            side=exit_side,
            confidence=1.0,
            price=price,
            reason=f"ExitPlan triggered: {trigger.value}",
            created_at=now,
        )
        intent = OrderIntent(
            signal=signal,
            instrument=plan.instrument,
            quantity=plan.quantity,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            priority=priority,
        )
        await self._enqueue(intent)
        logger.info(
            "Exit intent enqueued: %s side=%s trigger=%s price=%.2f",
            plan.symbol, exit_side.value, trigger.value, price,
        )

    @property
    def active_plan_count(self) -> int:
        return sum(1 for p in self._plans.values() if p.active)

    def active_plans(self) -> list[dict]:
        return [p.to_dict() for p in self._plans.values() if p.active]
