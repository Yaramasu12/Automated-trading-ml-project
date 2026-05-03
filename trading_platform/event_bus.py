from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class EventEnvelope:
    """Versioned event payload used by the local async architecture MVP."""

    event_name: str
    payload: dict[str, Any]
    stream: str = "default"
    event_id: str = field(default_factory=lambda: uuid4().hex)
    published_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_name": self.event_name,
            "stream": self.stream,
            "published_at": self.published_at.isoformat(),
            "payload": self.payload,
        }


class InMemoryEventBus:
    """Small durable-enough event backbone for local/paper mode.

    This deliberately keeps the same shape as a Redis Streams or NATS-backed
    bus would expose: producers publish versioned event names to streams, and
    consumers can inspect recent events and queue depths. It is process-local,
    so it is an MVP control-plane primitive rather than a production broker.
    """

    def __init__(self, max_events: int = 2_000) -> None:
        self.max_events = max_events
        self._events: deque[EventEnvelope] = deque(maxlen=max_events)
        self._streams: dict[str, deque[EventEnvelope]] = defaultdict(lambda: deque(maxlen=max_events))
        self._lock = RLock()

    def publish(
        self,
        event_name: str,
        payload: dict[str, Any] | None = None,
        stream: str = "default",
    ) -> EventEnvelope:
        envelope = EventEnvelope(event_name=event_name, payload=payload or {}, stream=stream)
        with self._lock:
            self._events.append(envelope)
            self._streams[stream].append(envelope)
        return envelope

    def recent(self, limit: int = 100, stream: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            source = self._streams[stream] if stream else self._events
            return [event.to_dict() for event in list(source)[-limit:]][::-1]

    def queue_depths(self) -> dict[str, int]:
        with self._lock:
            return {name: len(events) for name, events in self._streams.items()}

    def summary(self) -> dict[str, Any]:
        with self._lock:
            latest = self._events[-1].to_dict() if self._events else None
            return {
                "event_count": len(self._events),
                "streams": self.queue_depths(),
                "latest_event": latest,
                "backend": "in_memory",
            }
