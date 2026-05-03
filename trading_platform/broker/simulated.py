from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading_platform.broker.base import BrokerClient, BrokerResult
from trading_platform.domain.enums import OrderStatus
from trading_platform.domain.models import OrderIntent


class SimulatedBrokerClient(BrokerClient):
    name = "SIMULATED"

    def __init__(self, latency_ms: int = 12):
        self.latency_ms = latency_ms
        self.submitted: list[OrderIntent] = []

    def is_ready(self) -> bool:
        return True

    def submit_order(self, intent: OrderIntent) -> BrokerResult:
        submitted_at = datetime.now(timezone.utc)
        acknowledged_at = submitted_at + timedelta(milliseconds=self.latency_ms)
        self.submitted.append(intent)
        return BrokerResult(
            status=OrderStatus.FILLED,
            broker_order_id=f"SIM-{len(self.submitted):06d}",
            average_price=intent.limit_price or intent.signal.price,
            submitted_at=submitted_at,
            acknowledged_at=acknowledged_at,
            message="simulated_fill",
            raw={"mode": "simulated"},
        )

    def positions(self) -> list[dict]:
        return []
