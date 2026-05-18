from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Awaitable

from trading_platform.domain.enums import OrderPriority, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal, Trade
from trading_platform.exit.exit_plan import ExitPlan, ExitTrigger

logger = logging.getLogger(__name__)

ExitEnqueueFn = Callable[[OrderIntent], Awaitable[None]]

_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "exit_plans.db"

_CREATE_EXIT_PLANS = """
CREATE TABLE IF NOT EXISTS exit_plans (
    plan_id         TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    quantity        INTEGER NOT NULL,
    strategy_name   TEXT,
    side            TEXT NOT NULL,
    trace_id        TEXT,
    stop_loss_price REAL,
    target_price    REAL,
    trailing_pct    REAL,
    expiry_date     TEXT,
    partial_exit_enabled INTEGER NOT NULL DEFAULT 0,
    partial_exit_done    INTEGER NOT NULL DEFAULT 0,
    partial_exit_qty     INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
)
"""


def _plan_to_row(plan: ExitPlan) -> tuple:
    return (
        plan.plan_id,
        plan.symbol,
        plan.entry_price,
        plan.quantity,
        plan.strategy_name,
        plan.side,
        plan.trace_id,
        plan.stop_loss_price,
        plan.target_price,
        plan.trailing_pct,
        plan.expiry_date.isoformat() if plan.expiry_date else None,
        int(plan.partial_exit_enabled),
        int(plan.partial_exit_done),
        plan.partial_exit_qty,
        int(plan.active),
        datetime.now(timezone.utc).isoformat(),
    )


def _row_to_plan(row: dict) -> ExitPlan:
    plan = ExitPlan(
        plan_id=row["plan_id"],
        symbol=row["symbol"],
        entry_price=row["entry_price"],
        quantity=row["quantity"],
        strategy_name=row["strategy_name"] or "",
        side=row["side"],
        trace_id=row["trace_id"] or "",
        stop_loss_price=row["stop_loss_price"],
        target_price=row["target_price"],
        trailing_pct=row["trailing_pct"],
        expiry_date=date.fromisoformat(row["expiry_date"]) if row["expiry_date"] else None,
        partial_exit_enabled=bool(row["partial_exit_enabled"]),
        partial_exit_done=bool(row["partial_exit_done"]),
        partial_exit_qty=row["partial_exit_qty"],
        active=bool(row["active"]),
    )
    plan._highest_price = plan.entry_price
    plan._lowest_price = plan.entry_price
    return plan


class ExitManager:
    """Background monitor that tracks active ExitPlans and emits exit intents.

    After every entry fill the scheduler calls `register(plan)`.
    The monitor loop polls live prices and enqueues exit OrderIntents
    via the provided enqueue function (pointing at ExecutionScheduler.enqueue).

    Active exit plans are persisted to SQLite on every add/remove and reloaded
    on startup so open positions retain protection across process restarts.

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
        ExitTrigger.PARTIAL_TARGET: OrderPriority.TARGET,
        ExitTrigger.TRAILING_STOP: OrderPriority.TRAILING_STOP,
        ExitTrigger.MANUAL: OrderPriority.EMERGENCY_EXIT,
    }

    def __init__(
        self,
        enqueue_fn: ExitEnqueueFn,
        poll_interval: float = 1.0,
        portfolio=None,
        db_path: Path | None = None,
    ) -> None:
        self._enqueue = enqueue_fn
        self.poll_interval = poll_interval
        self._plans: dict[str, ExitPlan] = {}
        self._mark_prices: dict[str, float] = {}
        self._last_known_marks: dict[str, float] = {}   # sticky prices for fallback
        self._running = False
        self._task: asyncio.Task | None = None
        # Optional portfolio reference — used to guard against phantom exits for
        # positions that are already flat (e.g. after restart with stale DB plans).
        self._portfolio = portfolio
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._load_from_db()

    # ── Persistence helpers ───────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            with self._conn() as conn:
                conn.execute(_CREATE_EXIT_PLANS)
        except Exception as exc:
            logger.error("ExitManager: failed to init DB: %s", exc)

    def _upsert_plan(self, plan: ExitPlan) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO exit_plans
                       (plan_id, symbol, entry_price, quantity, strategy_name, side,
                        trace_id, stop_loss_price, target_price, trailing_pct,
                        expiry_date, partial_exit_enabled, partial_exit_done,
                        partial_exit_qty, active, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    _plan_to_row(plan),
                )
        except Exception as exc:
            logger.error("ExitManager: failed to upsert plan %s: %s", plan.plan_id, exc)

    def _delete_plan(self, plan_id: str) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE exit_plans SET active=0 WHERE plan_id=?",
                    (plan_id,),
                )
        except Exception as exc:
            logger.error("ExitManager: failed to deactivate plan %s in DB: %s", plan_id, exc)

    def _load_from_db(self) -> None:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM exit_plans WHERE active=1"
                ).fetchall()
            for row in rows:
                try:
                    plan = _row_to_plan(dict(row))
                    self._plans[plan.plan_id] = plan
                    if plan.entry_price > 0:
                        self._last_known_marks.setdefault(plan.symbol, plan.entry_price)
                except Exception as exc:
                    logger.error("ExitManager: failed to restore plan row: %s", exc)
            if self._plans:
                logger.info("ExitManager: restored %d active exit plan(s) from DB", len(self._plans))
        except Exception as exc:
            logger.error("ExitManager: failed to load plans from DB: %s", exc)

    # ── Public API ────────────────────────────────────────────────────────────

    def register(self, plan: ExitPlan) -> None:
        self._plans[plan.plan_id] = plan
        # Seed last-known price from entry so the monitor has a fallback immediately
        if plan.entry_price > 0:
            self._last_known_marks.setdefault(plan.symbol, plan.entry_price)
        self._upsert_plan(plan)
        logger.debug("ExitPlan registered: %s for %s", plan.plan_id, plan.symbol)

    def deregister(self, plan_id: str) -> None:
        self._plans.pop(plan_id, None)
        self._delete_plan(plan_id)

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
                self._delete_plan(plan.plan_id)

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
                    try:
                        await self._emit_exit_intent(plan, trigger, mark, now)
                        # Only mark triggered and remove the plan after enqueue succeeds.
                        # If enqueue fails (queue full, broker offline), we leave the plan
                        # active so the next poll cycle retries — prevents positions from
                        # going unprotected after a transient failure.
                        plan.mark_triggered(trigger, now)
                        triggered.append(plan_id)
                    except Exception as exc:
                        logger.exception("Failed to emit exit for plan %s: %s", plan_id, exc)
            for plan_id in triggered:
                self._plans.pop(plan_id, None)
                self._delete_plan(plan_id)

    async def _emit_exit_intent(
        self, plan: ExitPlan, trigger: ExitTrigger, price: float, now: datetime
    ) -> None:
        if plan.instrument is None:
            logger.warning("ExitPlan %s has no instrument — cannot emit exit intent", plan.plan_id)
            return

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

    def current_mark(self, symbol: str) -> float | None:
        """Return the best available mark price for `symbol`, or None if unknown."""
        mark = self._mark_prices.get(symbol) or self._last_known_marks.get(symbol)
        return float(mark) if mark and mark > 0 else None

    @property
    def active_plan_count(self) -> int:
        return sum(1 for p in self._plans.values() if p.active)

    def active_plans(self) -> list[dict]:
        return [p.to_dict() for p in self._plans.values() if p.active]
