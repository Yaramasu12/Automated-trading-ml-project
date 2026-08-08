"""
Event bus client — wraps Redpanda/Kafka for publishing and subscribing to market data events.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Generic, TypeVar, Optional

from .secrets import get_env
from .logging import get_logger

logger = get_logger("common.event_bus")


# ─── Canonical Event Types ───────────────────────────────────────────────

class EventType(str, Enum):
    """Types of events on the event bus."""
    # Market data
    TICK = "tick"
    BAR = "bar"
    ORDER_BOOK = "order_book"
    TRADE = "trade"
    # Strategy
    SIGNAL = "signal"
    ORDER_INTENT = "order_intent"
    # Execution
    ORDER = "order"
    FILL = "fill"
    REJECT = "reject"
    # System
    KILL_SWITCH = "kill_switch"
    HEARTBEAT = "heartbeat"
    # Research
    FEATURE_UPDATE = "feature_update"
    MODEL_PREDICTION = "model_prediction"


@dataclass
class Event:
    """Canonical event envelope published to the bus."""
    event_type: EventType
    timestamp: str  # ISO-8601 UTC
    source: str  # service/component name
    topic: str  # routing topic/channel
    data: dict[str, Any]
    seq: int = 0  # sequence number (per-source)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "Event":
        d = json.loads(raw)
        d["event_type"] = EventType(d["event_type"])
        return cls(**d)

    @classmethod
    def create(
        cls,
        event_type: EventType,
        topic: str,
        data: dict[str, Any],
        source: str = "unknown",
    ) -> "Event":
        return cls(
            event_type=event_type,
            timestamp=datetime.now(timezone.utc).isoformat(),
            source=source,
            topic=topic,
            data=data,
        )


# ─── Market Data Models ──────────────────────────────────────────────────

@dataclass
class Tick:
    """Canonical tick record."""
    symbol: str
    exchange: str
    price: float
    size: float
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    timestamp: str
    venue_raw: str = ""


@dataclass
class Bar:
    """Canonical OHLCV bar."""
    symbol: str
    exchange: str
    timeframe: str  # "1s", "1m", "5m", etc.
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: str
    trade_count: int = 0


@dataclass
class Signal:
    """Signal from strategy/ML model."""
    symbol: str
    direction: str  # "long", "short", "flat"
    strength: float  # -1.0 to 1.0
    target_qty: float
    model_name: str
    confidence: float = 0.0
    metadata: dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass
class OrderIntent:
    """Intent to place an order (goes through risk gate)."""
    symbol: str
    side: str  # "buy", "sell"
    order_type: str  # "market", "limit", "stop", "stop_limit"
    qty: float
    price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "GTC"
    strategy_id: str = ""
    tags: dict[str, str] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = {}


@dataclass
class Fill:
    """Order fill/execution."""
    order_id: str
    symbol: str
    side: str
    qty: float
    price: float
    commission: float = 0.0
    timestamp: str = ""
    venue: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


# ─── Event Bus Interface ─────────────────────────────────────────────────

T = TypeVar("T")


class EventBus(ABC):
    """Abstract event bus — both sync and async patterns supported."""

    @abstractmethod
    async def publish(self, event: Event) -> None:
        """Publish an event to the bus."""

    @abstractmethod
    async def subscribe(
        self,
        topic: str,
        callback: Callable[[Event], Coroutine[Any, Any, None]],
    ) -> None:
        """Subscribe to events on a topic."""

    @abstractmethod
    async def close(self) -> None:
        """Close the connection."""


class NoOpEventBus(EventBus):
    """No-op event bus for testing/CI without Redpanda."""

    async def publish(self, event: Event) -> None:
        logger.debug("NoOp publish: %s %s", event.topic, event.event_type)

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[Event], Coroutine[Any, Any, None]],
    ) -> None:
        logger.info("NoOp subscribe: %s", topic)

    async def close(self) -> None:
        pass


# ─── Redpanda/Kafka Client ───────────────────────────────────────────────

class RedpandaEventBus(EventBus):
    """Redpanda/Kafka-backed event bus."""

    def __init__(
        self,
        bootstrap_servers: str = "localhost:9092",
        client_id: str = "algo-trading",
    ):
        self.bootstrap_servers = bootstrap_servers
        self.client_id = client_id
        self._producer: Any = None
        self._consumer: Any = None
        self._subscribers: dict[str, list[Callable]] = {}
        self._running = False

    async def start(self) -> None:
        """Initialize producer and consumer connections."""
        try:
            from kafka import KafkaProducer, KafkaConsumer
        except ImportError:
            logger.warning(
                "kafka-python not installed. Falling back to NoOpEventBus."
            )
            raise ImportError("Install kafka-python: pip install kafka-python-ng")

        # Producer
        self._producer = KafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            value_serializer=lambda v: v.encode("utf-8") if isinstance(v, str) else v,
            key_serializer=lambda v: v.encode("utf-8") if v else None,
            acks="all",
            retries=3,
            max_in_flight_requests_per_connection=5,
        )

        logger.info("Redpanda producer connected: %s", self.bootstrap_servers)

    async def publish(self, event: Event) -> None:
        """Publish an event to Redpanda."""
        if self._producer is None:
            await self.start()

        topic = event.topic
        data = event.to_json()

        future = self._producer.send(topic, value=data, key=event.source)
        future.add_callback(lambda _: logger.debug("Published: %s → %s", topic, event.event_type))
        future.add_errback(lambda e: logger.error("Publish failed: %s", e))

        # Flush periodically (every 100 publishes handled by Kafka batching)
        try:
            self._producer.flush(timeout=5)
        except Exception:
            logger.exception("Failed to flush producer")

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[Event], Coroutine[Any, Any, None]],
    ) -> None:
        """Subscribe to a topic."""
        if topic not in self._subscribers:
            self._subscribers[topic] = []

        self._subscribers[topic].append(callback)
        logger.info("Subscribed to topic: %s (%d callbacks)", topic, len(self._subscribers[topic]))

    async def consume_loop(self, topics: list[str]) -> None:
        """Background loop to consume and dispatch events."""
        try:
            from kafka import KafkaConsumer
        except ImportError:
            logger.warning("kafka-python not available for consume_loop")
            return

        self._running = True
        consumer = KafkaConsumer(
            *topics,
            bootstrap_servers=self.bootstrap_servers,
            value_deserializer=lambda b: b.decode("utf-8"),
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            group_id=f"algo-trading-{self.client_id}",
            consumer_timeout_ms=10000,
        )

        logger.info("Consumer started for topics: %s", topics)

        while self._running:
            for message in consumer:
                if not self._running:
                    break
                try:
                    event = Event.from_json(message.value)
                    # Dispatch to subscribers
                    callbacks = self._subscribers.get(message.topic, [])
                    for cb in callbacks:
                        if callable(cb):
                            # For sync callbacks
                            result = cb(event)
                            if callable(result) and hasattr(result, "__await__"):
                                await result
                except Exception:
                    logger.exception("Error processing event on topic: %s", message.topic)

    async def close(self) -> None:
        """Close producer and consumer."""
        self._running = False
        if self._producer:
            try:
                self._producer.flush(timeout=5)
                self._producer.close(timeout=5)
            except Exception:
                logger.exception("Error closing producer")
        logger.info("Redpanda event bus closed")


# ─── Factory ──────────────────────────────────────────────────────────────

async def create_event_bus() -> EventBus:
    """Create the appropriate event bus based on environment."""
    use_redpanda = get_env("USE_REDPANDA", "false").lower() == "true"

    if not use_redpanda:
        logger.info("Using NoOpEventBus (USE_REDPANDA=false)")
        return NoOpEventBus()

    bootstrap = get_env("REDPANDA_BOOTSTRAP_SERVERS", "localhost:9092")
    bus = RedpandaEventBus(bootstrap_servers=bootstrap)
    await bus.start()
    logger.info("Using RedpandaEventBus: %s", bootstrap)
    return bus


__all__ = [
    "EventType",
    "Event",
    "Tick",
    "Bar",
    "Signal",
    "OrderIntent",
    "Fill",
    "EventBus",
    "NoOpEventBus",
    "RedpandaEventBus",
    "create_event_bus",
]