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

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel a previously-submitted, not-yet-terminal order.

        Returns True if the cancel was accepted, False otherwise. Required
        for chase-to-market: a resting LIMIT entry that hasn't filled within
        the chase window gets cancelled here before being resubmitted as
        MARKET. Abstract (not a soft default) because a no-op stub would let
        the scheduler double-book a limit AND a market order for the same
        intent without ever noticing the cancel silently did nothing.
        """
        raise NotImplementedError

