from __future__ import annotations

import json
import logging
import os
import urllib.request
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
        symbols: list[str] | None = None,
        reason: str = "emergency_square_off",
    ) -> dict:
        """Square off positions matching the given scope.

        ``symbols`` is an optional allow-list: only positions whose symbol is in
        this list will be closed.  Use it to restrict GLOBAL scope to equity-only
        (pass ``symbols=equity_positions``) or commodity-only without touching the
        other session's positions.
        """
        now = datetime.now(timezone.utc)
        positions = self._portfolio.positions
        _symbols_filter: set[str] | None = set(symbols) if symbols else None

        targets: list = []
        for pos in positions.values():
            if pos.quantity == 0:
                continue
            if scope == SquareOffScope.SYMBOL and pos.instrument.symbol != symbol:
                continue
            if _symbols_filter is not None and pos.instrument.symbol not in _symbols_filter:
                continue
            targets.append(pos)

        intents_enqueued: list[str] = []
        errors: list[str] = []
        self._send_alert(scope=scope, targets=len(targets), reason=reason, symbol=symbol)

        for pos in targets:
            close_side = Side.SELL if pos.quantity > 0 else Side.BUY
            mark_price = pos.average_price
            signal = Signal(
                strategy_name=strategy_name or f"emergency_square_off:{scope.value.lower()}",
                symbol=pos.instrument.symbol,
                side=close_side,
                confidence=1.0,
                price=mark_price,
                reason=f"EmergencySquareOff scope={scope.value}: {reason}",
                created_at=now,
                metadata={
                    "opens_position": False,
                    "square_off_scope": scope.value,
                    "square_off_reason": reason,
                },
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
            "reason": reason,
            "positions_targeted": len(targets),
            "intents_enqueued": len(intents_enqueued),
            "errors": errors,
            "timestamp": now.isoformat(),
        }

    def _send_alert(
        self,
        *,
        scope: SquareOffScope,
        targets: int,
        reason: str,
        symbol: str | None,
    ) -> None:
        webhook = os.environ.get("EMERGENCY_SQUARE_OFF_WEBHOOK_URL", "").strip()
        if not webhook:
            return
        payload = {
            "event": "emergency_square_off",
            "scope": scope.value,
            "symbol": symbol,
            "positions_targeted": targets,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            req = urllib.request.Request(
                webhook,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3).close()
        except Exception as exc:
            logger.warning("EmergencySquareOff alert webhook failed: %s", exc)
