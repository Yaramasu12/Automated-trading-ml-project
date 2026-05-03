from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable

from trading_platform.domain.enums import OrderPriority, OrderType, ProductType, Side, SquareOffScope
from trading_platform.domain.models import OrderIntent, Signal
from trading_platform.portfolio.ledger import PortfolioLedger

logger = logging.getLogger(__name__)

EnqueueFn = Callable[[OrderIntent], Awaitable[str]]


class EmergencySquareOff:
    """Close all, strategy-level, or symbol-level positions immediately.

    Enqueues EMERGENCY_EXIT priority intents for every net-long or net-short
    position matching the requested scope.
    """

    def __init__(self, portfolio: PortfolioLedger, enqueue_fn: EnqueueFn) -> None:
        self._portfolio = portfolio
        self._enqueue = enqueue_fn

    async def square_off(
        self,
        scope: SquareOffScope = SquareOffScope.GLOBAL,
        strategy_name: str | None = None,
        symbol: str | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        positions = self._portfolio.positions

        targets: list = []
        for pos in positions.values():
            if pos.quantity == 0:
                continue
            if scope == SquareOffScope.SYMBOL and pos.instrument.symbol != symbol:
                continue
            targets.append(pos)

        intents_enqueued: list[str] = []
        errors: list[str] = []

        for pos in targets:
            close_side = Side.SELL if pos.quantity > 0 else Side.BUY
            mark_price = pos.average_price
            signal = Signal(
                strategy_name=strategy_name or f"emergency_square_off:{scope.value.lower()}",
                symbol=pos.instrument.symbol,
                side=close_side,
                confidence=1.0,
                price=mark_price,
                reason=f"EmergencySquareOff scope={scope.value}",
                created_at=now,
            )
            intent = OrderIntent(
                signal=signal,
                instrument=pos.instrument,
                quantity=abs(pos.quantity),
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                priority=OrderPriority.EMERGENCY_EXIT,
            )
            try:
                event_id = await self._enqueue(intent)
                intents_enqueued.append(event_id)
                logger.warning(
                    "EmergencySquareOff: %s %s qty=%d",
                    close_side.value, pos.instrument.symbol, abs(pos.quantity),
                )
            except Exception as exc:
                err = f"{pos.instrument.symbol}: {exc}"
                errors.append(err)
                logger.exception("EmergencySquareOff enqueue failed for %s: %s", pos.instrument.symbol, exc)

        return {
            "scope": scope.value,
            "positions_targeted": len(targets),
            "intents_enqueued": len(intents_enqueued),
            "errors": errors,
            "timestamp": now.isoformat(),
        }
