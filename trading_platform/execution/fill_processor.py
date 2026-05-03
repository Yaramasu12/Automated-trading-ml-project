from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from trading_platform.domain.models import Order, OrderIntent, Trade
from trading_platform.domain.enums import OrderStatus
from trading_platform.execution.oms_store import OMSEventStore
from trading_platform.portfolio.ledger import PortfolioLedger


class FillProcessor:
    """Processes broker fills: updates portfolio, records OMS event, returns Trade."""

    def __init__(self, portfolio: PortfolioLedger, oms: OMSEventStore) -> None:
        self.portfolio = portfolio
        self.oms = oms

    def process(
        self,
        order: Order,
        fill_price: float,
        fill_qty: int,
        charges: float,
        now: datetime | None = None,
    ) -> Trade:
        now = now or datetime.now(timezone.utc)
        trade = self.portfolio.apply_fill(order, fill_price, now, charges)
        self.oms.append(
            event_type="fill_processed",
            order_id=order.intent.idempotency_key,
            idempotency_key=order.intent.idempotency_key,
            symbol=order.intent.instrument.symbol,
            strategy_name=order.intent.signal.strategy_name,
            side=order.intent.signal.side.value,
            quantity=fill_qty,
            price=fill_price,
            fill_price=fill_price,
            fill_qty=fill_qty,
            metadata={"charges": charges, "trade_id": trade.trade_id},
        )
        return trade

    def process_partial(
        self,
        order: Order,
        fill_price: float,
        fill_qty: int,
        charges: float,
        now: datetime | None = None,
    ) -> Trade:
        now = now or datetime.now(timezone.utc)
        partial_intent = OrderIntent(
            signal=order.intent.signal,
            instrument=order.intent.instrument,
            quantity=fill_qty,
            order_type=order.intent.order_type,
            product_type=order.intent.product_type,
            limit_price=order.intent.limit_price,
            stop_loss=order.intent.stop_loss,
            target=order.intent.target,
            priority=order.intent.priority,
            idempotency_key=order.intent.idempotency_key,
        )
        partial_order = Order(intent=partial_intent, status=OrderStatus.PARTIALLY_FILLED)
        return self.process(partial_order, fill_price, fill_qty, charges, now)
