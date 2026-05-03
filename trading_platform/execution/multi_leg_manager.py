from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Awaitable
from uuid import uuid4

from trading_platform.domain.enums import LegStatus, OrderPriority
from trading_platform.domain.models import OrderIntent, Trade

logger = logging.getLogger(__name__)

EnqueueFn = Callable[[OrderIntent], Awaitable[str]]


@dataclass
class Leg:
    leg_id: str = field(default_factory=lambda: uuid4().hex)
    intent: OrderIntent | None = None
    status: LegStatus = LegStatus.PENDING
    broker_order_id: str | None = None
    fill_price: float | None = None
    filled_at: datetime | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "leg_id": self.leg_id,
            "status": self.status.value,
            "broker_order_id": self.broker_order_id,
            "fill_price": self.fill_price,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "error": self.error,
            "symbol": self.intent.instrument.symbol if self.intent else None,
            "side": self.intent.signal.side.value if self.intent else None,
            "quantity": self.intent.quantity if self.intent else None,
        }


@dataclass
class MultiLegOrder:
    """Represents a spread (e.g. bull-call spread, iron condor).

    Legs are submitted sequentially. If any leg fails the already-filled legs
    are reversed (partial-fill rollback) by enqueuing opposite-side intents.
    """
    order_id: str = field(default_factory=lambda: uuid4().hex)
    strategy_name: str = ""
    legs: list[Leg] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    rolled_back: bool = False
    active: bool = True

    @property
    def all_filled(self) -> bool:
        return all(leg.status == LegStatus.FILLED for leg in self.legs)

    @property
    def any_failed(self) -> bool:
        return any(leg.status == LegStatus.FAILED for leg in self.legs)

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "strategy_name": self.strategy_name,
            "leg_count": len(self.legs),
            "legs": [leg.to_dict() for leg in self.legs],
            "all_filled": self.all_filled,
            "any_failed": self.any_failed,
            "rolled_back": self.rolled_back,
            "active": self.active,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class MultiLegOrderManager:
    """Submits option spreads and other multi-leg strategies leg by leg.

    On any leg failure the filled legs are immediately reversed to avoid
    naked exposure. Fill callbacks notify the exit manager for each filled leg.
    """

    def __init__(self, enqueue_fn: EnqueueFn) -> None:
        self._enqueue = enqueue_fn
        self._orders: dict[str, MultiLegOrder] = {}
        self._fill_registry: dict[str, Trade] = {}

    async def submit(
        self,
        legs: list[OrderIntent],
        strategy_name: str = "",
    ) -> MultiLegOrder:
        ml_order = MultiLegOrder(
            strategy_name=strategy_name,
            legs=[Leg(intent=intent) for intent in legs],
        )
        self._orders[ml_order.order_id] = ml_order

        filled_legs: list[Leg] = []
        for leg in ml_order.legs:
            if leg.intent is None:
                continue
            try:
                leg.status = LegStatus.SUBMITTED
                await self._enqueue(leg.intent)
                leg.status = LegStatus.FILLED
                filled_legs.append(leg)
            except Exception as exc:
                leg.status = LegStatus.FAILED
                leg.error = str(exc)
                logger.error(
                    "MultiLeg %s: leg %s failed (%s) — rolling back %d filled legs",
                    ml_order.order_id, leg.leg_id, exc, len(filled_legs),
                )
                await self._rollback(ml_order, filled_legs)
                ml_order.rolled_back = True
                ml_order.active = False
                ml_order.completed_at = datetime.now(timezone.utc)
                return ml_order

        ml_order.completed_at = datetime.now(timezone.utc)
        ml_order.active = False
        logger.info("MultiLeg %s: all %d legs filled", ml_order.order_id, len(filled_legs))
        return ml_order

    async def _rollback(self, ml_order: MultiLegOrder, filled_legs: list[Leg]) -> None:
        import dataclasses
        from trading_platform.domain.enums import Side
        for leg in reversed(filled_legs):
            if leg.intent is None:
                continue
            original_side = leg.intent.signal.side
            reverse_side = Side.SELL if original_side == Side.BUY else Side.BUY
            reverse_signal = dataclasses.replace(
                leg.intent.signal,
                side=reverse_side,
                reason=f"rollback:{ml_order.order_id}",
            )
            reverse_intent = dataclasses.replace(
                leg.intent,
                signal=reverse_signal,
                priority=OrderPriority.EMERGENCY_EXIT,
            )
            try:
                await self._enqueue(reverse_intent)
                leg.status = LegStatus.CANCELLED
            except Exception as exc:
                logger.exception("MultiLeg rollback failed for leg %s: %s", leg.leg_id, exc)

    def register_fill(self, idempotency_key: str, trade: Trade) -> None:
        self._fill_registry[idempotency_key] = trade

    def active_orders(self) -> list[dict]:
        return [o.to_dict() for o in self._orders.values() if o.active]

    def all_orders(self) -> list[dict]:
        return [o.to_dict() for o in self._orders.values()]
