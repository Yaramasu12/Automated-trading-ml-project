from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class OperationalEvent:
    timestamp: datetime
    event_type: str
    severity: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp.isoformat()
        return payload


@dataclass(frozen=True)
class OperationalSnapshot:
    status: str
    started_at: datetime
    uptime_seconds: float
    execution_mode: str
    live_armed: bool
    kill_switch_active: bool
    stale_market_data: bool
    total_orders: int
    filled_orders: int
    rejected_orders: int
    rejection_rate: float
    average_latency_ms: float
    max_latency_ms: float
    event_count: int

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["started_at"] = self.started_at.isoformat()
        return payload


class OperationalMonitor:
    def __init__(self):
        self.started_at = datetime.now(timezone.utc)
        self.events: list[OperationalEvent] = []
        self.total_orders = 0
        self.filled_orders = 0
        self.rejected_orders = 0
        self.latencies_ms: list[float] = []

    def record_event(
        self,
        event_type: str,
        message: str,
        severity: str = "INFO",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            OperationalEvent(
                timestamp=datetime.now(timezone.utc),
                event_type=event_type,
                severity=severity,
                message=message,
                metadata=metadata or {},
            )
        )

    def record_order(self, order: dict) -> None:
        self.total_orders += 1
        status = str(order.get("status", "")).upper()
        if status == "FILLED":
            self.filled_orders += 1
        if status in {"REJECTED", "RISK_REJECTED", "CANCELLED"}:
            self.rejected_orders += 1
        latency = order.get("latency_ms")
        if latency is not None:
            self.latencies_ms.append(float(latency))

    def record_orders(self, orders: list[dict]) -> None:
        for order in orders:
            self.record_order(order)

    def snapshot(
        self,
        execution_mode: str,
        live_armed: bool,
        kill_switch_active: bool,
        stale_market_data: bool = False,
    ) -> OperationalSnapshot:
        now = datetime.now(timezone.utc)
        rejection_rate = self.rejected_orders / self.total_orders if self.total_orders else 0.0
        average_latency = sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else 0.0
        max_latency = max(self.latencies_ms) if self.latencies_ms else 0.0
        status = "HEALTHY"
        if kill_switch_active:
            status = "HALTED"
        elif stale_market_data or rejection_rate > 0.10 or average_latency > 200:
            status = "DEGRADED"
        return OperationalSnapshot(
            status=status,
            started_at=self.started_at,
            uptime_seconds=(now - self.started_at).total_seconds(),
            execution_mode=execution_mode,
            live_armed=live_armed,
            kill_switch_active=kill_switch_active,
            stale_market_data=stale_market_data,
            total_orders=self.total_orders,
            filled_orders=self.filled_orders,
            rejected_orders=self.rejected_orders,
            rejection_rate=rejection_rate,
            average_latency_ms=average_latency,
            max_latency_ms=max_latency,
            event_count=len(self.events),
        )

    def recent_events(self, limit: int = 20) -> list[dict]:
        return [event.to_dict() for event in self.events[-limit:]]
