from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from trading_platform.domain.enums import OrderStatus
from trading_platform.domain.models import OrderIntent


@dataclass(frozen=True)
class BrokerResult:
    status: OrderStatus
    broker_order_id: str | None
    average_price: float | None
    submitted_at: datetime
    acknowledged_at: datetime
    message: str
    raw: dict | None = None


class BrokerClient(ABC):
    name: str

    @abstractmethod
    def is_ready(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, intent: OrderIntent) -> BrokerResult:
        raise NotImplementedError

    @abstractmethod
    def positions(self) -> list[dict]:
        raise NotImplementedError

