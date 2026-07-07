from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable

from trading_platform.domain.enums import OrderPriority, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal
from trading_platform.exit.exit_plan import ExitPlan, ExitTrigger

logger = logging.getLogger(__name__)

ExitEnqueueFn = Callable[[OrderIntent], Awaitable[None]]

# How long an emitted exit may stay unfilled before the plan re-arms and the
# trigger fires again. Covers rejected/lost exit orders: the position never
# silently loses protection just because one exit attempt died downstream.
PENDING_EXIT_RETRY_SECONDS = 120.0


class ExitManager:
    """Background monitor that tracks active ExitPlans and emits exit intents.

    After every entry fill the runtime calls `register(plan)`. The monitor
    loop polls marks and enqueues exit OrderIntents via the provided enqueue
    function (pointing at ExecutionScheduler.enqueue).

    Lifecycle (audit fix H1): a plan is NOT removed when its trigger fires —
    emitting an exit order is not the same as being flat. The plan enters a
    "pending exit" state and is only deregistered when the exit FILL arrives
    (runtime._on_fill → on_exit_fill). If no fill arrives within
    PENDING_EXIT_RETRY_SECONDS (order rejected, queue full, broker down),
    the plan re-arms and the exit is retried.

    Persistence (audit fix H2): the runtime's database is the single source
    of truth (save on entry fill, delete on exit fill, restore on startup
    with instruments attached and filtered by execution mode). This class
    keeps no store of its own — an earlier private SQLite mirror restored
    plans without instruments and without mode scoping, which could silently
    drop protection or leak plans across PAPER/LIVE.

    Kill switch (audit fix C2): freeze semantics. The kill switch blocks new
    entries at the scheduler/risk layer; exit plans stay registered and exit
    orders continue to flow (scheduler and RiskEngine explicitly allow
    position-reducing orders during kill switch). Flattening everything is a
    separate, explicit action (EmergencySquareOff).

    Mark price priority:
      1. Live tick pushed via update_marks() (runtime._on_tick, per tick)
      2. Sticky last-known mark for the symbol
      3. Entry price (worst case — at least SL/target can still check on large moves)
    """

    PRIORITY_MAP: dict[ExitTrigger, OrderPriority] = {
        ExitTrigger.KILL_SWITCH: OrderPriority.KILL_SWITCH,
        ExitTrigger.STOP_LOSS: OrderPriority.STOP_LOSS,
        ExitTrigger.EXPIRY: OrderPriority.EXPIRY_EXIT,
        ExitTrigger.TARGET: OrderPriority.TARGET,
        ExitTrigger.PARTIAL_TARGET: OrderPriority.TARGET,
        ExitTrigger.TRAILING_STOP: OrderPriority.TRAILING_STOP,
        ExitTrigger.MANUAL: OrderPriority.EMERGENCY_EXIT,
    }

    def __init__(
        self,
        enqueue_fn: ExitEnqueueFn,
        poll_interval: float = 1.0,
        portfolio=None,
    ) -> None:
        self._enqueue = enqueue_fn
        self.poll_interval = poll_interval
        self._plans: dict[str, ExitPlan] = {}
        self._mark_prices: dict[str, float] = {}
        self._last_known_marks: dict[str, float] = {}   # sticky prices for fallback
        # plan_id → (monotonic-ish wall time when the exit intent was enqueued, trigger)
        self._pending_exits: dict[str, tuple[datetime, ExitTrigger]] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        # Optional portfolio reference — used to guard against phantom exits for
        # positions that are already flat (e.g. after restart with stale DB plans).
        self._portfolio = portfolio

    # ── Public API ────────────────────────────────────────────────────────────

    def register(self, plan: ExitPlan) -> None:
        self._plans[plan.plan_id] = plan
        # Seed last-known price from entry so the monitor has a fallback immediately
        if plan.entry_price > 0:
            self._last_known_marks.setdefault(plan.symbol, plan.entry_price)
        logger.debug("ExitPlan registered: %s for %s", plan.plan_id, plan.symbol)

    def deregister(self, plan_id: str) -> None:
        self._plans.pop(plan_id, None)
        self._pending_exits.pop(plan_id, None)

    def update_marks(self, prices: dict[str, float]) -> None:
        self._mark_prices.update(prices)
        # Keep sticky last-known for fallback when live feed goes stale
        self._last_known_marks.update({k: v for k, v in prices.items() if v > 0})

    def on_exit_fill(self, symbol: str, plan_id: str | None = None, trigger: str | None = None) -> None:
        """Confirmed exit fill — the ONLY event that removes a plan.

        Partial-target fills reduce the plan's remaining quantity and keep it
        active (stop already raised to breakeven by check_trigger); every other
        trigger closes out the plan.
        """
        now = datetime.now(timezone.utc)
        plans = (
            [self._plans[plan_id]] if plan_id and plan_id in self._plans
            else [p for p in self._plans.values() if p.symbol == symbol]
        )
        for plan in list(plans):
            if trigger == ExitTrigger.PARTIAL_TARGET.value:
                remaining = plan.quantity - plan.partial_exit_qty
                if remaining > 0:
                    plan.quantity = remaining
                    self._pending_exits.pop(plan.plan_id, None)
                    logger.info(
                        "ExitPlan %s: partial exit filled, %d lot(s) remain under trailing protection",
                        plan.plan_id, remaining,
                    )
                    continue
            plan.mark_triggered(
                ExitTrigger(trigger) if trigger in ExitTrigger._value2member_map_ else ExitTrigger.MANUAL,
                now,
            )
            self.deregister(plan.plan_id)
            logger.info("ExitPlan %s closed on confirmed exit fill (%s)", plan.plan_id, trigger)

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
            for plan_id, plan in list(self._plans.items()):
                if not plan.active:
                    continue
                pending = self._pending_exits.get(plan_id)
                if pending is not None:
                    emitted_at, prev_trigger = pending
                    if (now - emitted_at).total_seconds() < PENDING_EXIT_RETRY_SECONDS:
                        continue  # exit order in flight — wait for its fill
                    # No fill confirmation within the window: re-arm and retry.
                    self._pending_exits.pop(plan_id, None)
                    logger.warning(
                        "ExitPlan %s: exit (%s) unconfirmed after %.0fs — re-arming trigger",
                        plan_id, prev_trigger.value, PENDING_EXIT_RETRY_SECONDS,
                    )
                # Mark price resolution: live tick → sticky last-known → entry price
                mark = (
                    self._mark_prices.get(plan.symbol)
                    or self._last_known_marks.get(plan.symbol)
                    or plan.entry_price  # fallback to entry — at least SL/target can check
                )
                if not mark or mark <= 0:
                    continue
                trigger = plan.check_trigger(mark, now)
                if trigger:
                    try:
                        status = await self._emit_exit_intent(plan, trigger, mark, now)
                    except Exception as exc:
                        logger.exception("Failed to emit exit for plan %s: %s", plan_id, exc)
                        continue  # plan stays registered — retried next poll
                    if status == "emitted":
                        self._pending_exits[plan_id] = (now, trigger)
                    elif status == "flat":
                        # Position already closed elsewhere (square-off/manual) — plan is stale.
                        self.deregister(plan_id)
                    # status == "no_instrument": keep the plan and keep alarming;
                    # deleting it would silently strip protection (audit fix H1).

    async def _emit_exit_intent(
        self, plan: ExitPlan, trigger: ExitTrigger, price: float, now: datetime
    ) -> str:
        """Returns "emitted" | "flat" | "no_instrument"."""
        if plan.instrument is None:
            logger.critical(
                "ExitPlan %s has no instrument — cannot emit exit intent; position %s is UNPROTECTED",
                plan.plan_id, plan.symbol,
            )
            return "no_instrument"

        # Guard: suppress phantom exits for already-flat positions.
        # This prevents duplicate sells after restarts where stale DB exit plans
        # are re-loaded for positions that were closed in a previous session.
        if self._portfolio is not None:
            pos = self._portfolio.positions.get(plan.symbol)
            if pos is None or pos.quantity == 0:
                logger.warning(
                    "ExitPlan %s: position %s is already flat — suppressing phantom exit (trigger=%s)",
                    plan.plan_id, plan.symbol, trigger.value,
                )
                return "flat"

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
            metadata={
                "opens_position": False,
                "trace_id": plan.trace_id,
                "exit_trigger": trigger.value,
                "exit_plan_id": plan.plan_id,
            },
        )
        # For partial exits, use the partial quantity (50% of position).
        # For all other triggers, close the full position.
        exit_qty = (
            plan.partial_exit_qty
            if trigger == ExitTrigger.PARTIAL_TARGET and plan.partial_exit_qty and plan.partial_exit_qty > 0
            else plan.quantity
        )
        intent = OrderIntent(
            signal=signal,
            instrument=plan.instrument,
            quantity=exit_qty,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            priority=priority,
        )
        await self._enqueue(intent)
        logger.info(
            "Exit intent enqueued: %s side=%s trigger=%s price=%.2f",
            plan.symbol, exit_side.value, trigger.value, price,
        )
        return "emitted"

    def current_mark(self, symbol: str) -> float | None:
        """Return the best available mark price for `symbol`, or None if unknown."""
        mark = self._mark_prices.get(symbol) or self._last_known_marks.get(symbol)
        return float(mark) if mark and mark > 0 else None

    @property
    def active_plan_count(self) -> int:
        return sum(1 for p in self._plans.values() if p.active)

    def active_plans(self) -> list[dict]:
        return [p.to_dict() for p in self._plans.values() if p.active]
