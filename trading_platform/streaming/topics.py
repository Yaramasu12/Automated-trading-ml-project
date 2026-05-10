from __future__ import annotations

"""Typed streaming bus — formal topic schema over InMemoryEventBus.

Defines the canonical topic names from the canvas architecture and wraps the
existing InMemoryEventBus with:
  - topic validation (only known BusTopic values accepted)
  - structured TypedMessage envelope (source, schema_version, ts)
  - per-topic callback subscription
  - backward-compatible passthrough to underlying InMemoryEventBus

Canvas topics implemented
--------------------------
  tick.raw        - raw ticks from Angel One WebSocket
  bar.closed      - completed OHLCV candle (any timeframe)
  option.chain    - option chain snapshots (per underlying)
  features        - feature vectors from FeatureStore.append()
  agent.vote      - AgentCouncilDecision from AgentCouncilSupervisor
  model.prediction - NeuralPredictionBundle from NeuralPredictionService
  quantum.result  - QuantumOptimizationResult from QuantumOptimizationService
  risk.veto       - RiskEngine / ComplianceGuard rejection event
  order.event     - order lifecycle: submitted | filled | rejected | cancelled

Scale-out path
--------------
Replace InMemoryEventBus with a Redpanda/NATS adapter that implements
the same .publish() interface without touching any caller code.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ── Topic registry ────────────────────────────────────────────────────────────

class BusTopic(str, Enum):
    """Canonical topic names.  Values match the canvas wire-format strings."""

    TICK_RAW          = "tick.raw"
    BAR_CLOSED        = "bar.closed"
    OPTION_CHAIN      = "option.chain"
    FEATURES          = "features"
    AGENT_VOTE        = "agent.vote"
    MODEL_PREDICTION  = "model.prediction"
    QUANTUM_RESULT    = "quantum.result"
    RISK_VETO         = "risk.veto"
    ORDER_EVENT       = "order.event"

    # Internal control topics (not in canvas, used by runtime)
    RUNTIME_CONTROL   = "runtime.control"
    LABEL_OUTCOME     = "label.outcome"


# ── Message envelope ──────────────────────────────────────────────────────────

@dataclass
class TypedMessage:
    """Enriched event envelope published on the TypedTopicBus."""
    topic: BusTopic
    payload: dict[str, Any]
    source: str = ""                    # component that published this message
    schema_version: str = "1.0"
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "topic": self.topic.value,
            "source": self.source,
            "schema_version": self.schema_version,
            "ts": self.ts.isoformat(),
            "payload": self.payload,
        }


# Topic callback type
TopicCallback = Callable[[TypedMessage], None]


# ── Typed bus ─────────────────────────────────────────────────────────────────

class TypedTopicBus:
    """Topic-validated event bus wrapping InMemoryEventBus.

    Usage
    -----
    # Publish:
    bus.publish(BusTopic.TICK_RAW, {"symbol": "NIFTY", "last_price": 22500.0}, source="live_feed")

    # Subscribe:
    bus.subscribe(BusTopic.FEATURES, lambda msg: print(msg.payload))

    # Inspect:
    bus.recent(BusTopic.TICK_RAW, limit=10)
    """

    def __init__(self, underlying_bus: Any) -> None:
        """
        Parameters
        ----------
        underlying_bus : InMemoryEventBus
            The existing event bus to forward all messages to, so that the
            existing .recent() and .queue_depths() dashboards continue working.
        """
        self._bus = underlying_bus
        self._callbacks: dict[str, list[TopicCallback]] = {}
        self._lock = threading.RLock()
        self._message_counts: dict[str, int] = {t.value: 0 for t in BusTopic}

    # ── Publish ───────────────────────────────────────────────────────────────

    def publish(
        self,
        topic: BusTopic,
        payload: dict[str, Any],
        source: str = "",
        schema_version: str = "1.0",
    ) -> TypedMessage:
        """Publish a validated typed message to the bus.

        Also forwards to underlying InMemoryEventBus so existing subscribers
        and dashboard queries continue to work unchanged.
        """
        msg = TypedMessage(
            topic=topic,
            payload=payload,
            source=source,
            schema_version=schema_version,
        )

        # Forward to underlying bus (backward compat)
        try:
            self._bus.publish(
                topic.value,
                {"source": source, "schema_version": schema_version, **payload},
                stream=topic.value.replace(".", "_"),
            )
        except Exception as exc:
            logger.debug("TypedTopicBus: underlying bus error for %s: %s", topic.value, exc)

        # Count
        with self._lock:
            self._message_counts[topic.value] = self._message_counts.get(topic.value, 0) + 1

        # Dispatch callbacks
        self._dispatch(topic.value, msg)

        return msg

    # ── Subscribe ─────────────────────────────────────────────────────────────

    def subscribe(self, topic: BusTopic, callback: TopicCallback) -> None:
        """Register a callback for a specific topic.

        Callbacks are invoked synchronously in the publish thread.
        Keep them fast — offload heavy work to a queue/thread if needed.
        """
        with self._lock:
            self._callbacks.setdefault(topic.value, []).append(callback)

    def unsubscribe(self, topic: BusTopic, callback: TopicCallback) -> None:
        with self._lock:
            cbs = self._callbacks.get(topic.value, [])
            if callback in cbs:
                cbs.remove(callback)

    # ── Inspection ────────────────────────────────────────────────────────────

    def recent(self, topic: BusTopic | None = None, limit: int = 20) -> list[dict]:
        """Return recent messages from underlying bus (backward-compat view)."""
        stream = topic.value.replace(".", "_") if topic else None
        return self._bus.recent(limit=limit, stream=stream)

    def message_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._message_counts)

    def topic_status(self) -> dict[str, Any]:
        """Summary for the /monitoring/metrics and /health endpoints."""
        with self._lock:
            counts = dict(self._message_counts)
        return {
            "topics": [t.value for t in BusTopic],
            "message_counts": counts,
            "total_messages": sum(counts.values()),
            "backend": "in_memory_typed",
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _dispatch(self, topic_value: str, msg: TypedMessage) -> None:
        with self._lock:
            callbacks = list(self._callbacks.get(topic_value, []))
        for cb in callbacks:
            try:
                cb(msg)
            except Exception as exc:
                logger.warning("TypedTopicBus: callback error on topic %s: %s", topic_value, exc)


# ── Convenience publish helpers ───────────────────────────────────────────────

def publish_tick(bus: TypedTopicBus, symbol: str, last_price: float, **kwargs: Any) -> None:
    """Shorthand for TICK_RAW messages from the live feed."""
    bus.publish(BusTopic.TICK_RAW, {"symbol": symbol, "last_price": last_price, **kwargs},
                source="live_feed")


def publish_bar(bus: TypedTopicBus, symbol: str, timeframe: str, ohlcv: dict[str, Any]) -> None:
    """Shorthand for BAR_CLOSED messages."""
    bus.publish(BusTopic.BAR_CLOSED, {"symbol": symbol, "timeframe": timeframe, **ohlcv},
                source="bar_builder")


def publish_features(bus: TypedTopicBus, symbol: str, record: dict[str, Any]) -> None:
    """Shorthand for FEATURES messages from FeatureStore."""
    bus.publish(BusTopic.FEATURES, {"symbol": symbol, **record}, source="feature_store")


def publish_agent_vote(bus: TypedTopicBus, decision: dict[str, Any]) -> None:
    """Shorthand for AGENT_VOTE messages from AgentCouncilSupervisor."""
    bus.publish(BusTopic.AGENT_VOTE, decision, source="agent_council")


def publish_model_prediction(bus: TypedTopicBus, bundle: dict[str, Any]) -> None:
    """Shorthand for MODEL_PREDICTION messages from NeuralPredictionService."""
    bus.publish(BusTopic.MODEL_PREDICTION, bundle, source="neural_service")


def publish_quantum_result(bus: TypedTopicBus, result: dict[str, Any]) -> None:
    """Shorthand for QUANTUM_RESULT messages from QuantumOptimizationService."""
    bus.publish(BusTopic.QUANTUM_RESULT, result, source="quantum_service")


def publish_risk_veto(
    bus: TypedTopicBus,
    symbol: str,
    reason: str,
    component: str,
    intent_id: str = "",
) -> None:
    """Shorthand for RISK_VETO messages from the risk/compliance layer."""
    bus.publish(
        BusTopic.RISK_VETO,
        {"symbol": symbol, "reason": reason, "intent_id": intent_id},
        source=component,
    )


def publish_order_event(
    bus: TypedTopicBus,
    event: str,
    symbol: str,
    side: str,
    price: float,
    quantity: int,
    strategy: str = "",
    intent_id: str = "",
) -> None:
    """Shorthand for ORDER_EVENT messages (submitted | filled | rejected | cancelled)."""
    bus.publish(
        BusTopic.ORDER_EVENT,
        {
            "event": event,
            "symbol": symbol,
            "side": side,
            "price": price,
            "quantity": quantity,
            "strategy": strategy,
            "intent_id": intent_id,
        },
        source="execution_scheduler",
    )
