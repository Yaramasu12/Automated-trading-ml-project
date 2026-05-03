from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from trading_platform.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentType,
    OptionType,
    OrderPriority,
    OrderStatus,
    OrderType,
    ProductType,
    Segment,
    Side,
)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    exchange: Exchange
    segment: Segment
    asset_class: AssetClass
    instrument_type: InstrumentType
    token: str
    lot_size: int = 1
    tick_size: float = 0.05
    expiry: date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    underlying: str | None = None

    @property
    def is_derivative(self) -> bool:
        return self.segment in {Segment.FUTURES, Segment.OPTIONS}

    @property
    def is_expiry_sensitive(self) -> bool:
        return self.expiry is not None


@dataclass(frozen=True)
class MarketBar:
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0


@dataclass(frozen=True)
class Signal:
    strategy_name: str
    symbol: str
    side: Side
    confidence: float
    price: float
    reason: str
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderIntent:
    signal: Signal
    instrument: Instrument
    quantity: int
    order_type: OrderType
    product_type: ProductType
    limit_price: float | None = None
    stop_loss: float | None = None
    target: float | None = None
    priority: OrderPriority = OrderPriority.ENTRY
    idempotency_key: str = field(default_factory=lambda: uuid4().hex)

    @property
    def notional_value(self) -> float:
        price = self.limit_price or self.signal.price
        return abs(self.quantity * price * self.instrument.lot_size)


@dataclass(order=True)
class PrioritizedOrderIntent:
    """Wrapper that makes OrderIntent sortable for asyncio.PriorityQueue."""
    priority: int
    seq: int
    intent: OrderIntent = field(compare=False)


@dataclass
class Order:
    intent: OrderIntent
    status: OrderStatus = OrderStatus.CREATED
    broker_order_id: str | None = None
    submitted_at: datetime | None = None
    acknowledged_at: datetime | None = None
    filled_at: datetime | None = None
    average_price: float | None = None
    rejection_reason: str | None = None

    @property
    def latency_ms(self) -> float | None:
        if self.submitted_at is None or self.acknowledged_at is None:
            return None
        delta = self.acknowledged_at - self.submitted_at
        return delta.total_seconds() * 1000


@dataclass(frozen=True)
class Trade:
    trade_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: int
    price: float
    charges: float
    timestamp: datetime
    strategy_name: str


@dataclass
class Position:
    instrument: Instrument
    quantity: int = 0
    average_price: float = 0.0
    realized_pnl: float = 0.0

    def market_value(self, mark_price: float) -> float:
        return self.quantity * mark_price * self.instrument.lot_size

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.quantity == 0:
            return 0.0
        return (mark_price - self.average_price) * self.quantity * self.instrument.lot_size

