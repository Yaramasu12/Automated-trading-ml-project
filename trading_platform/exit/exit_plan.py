from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from uuid import uuid4

from trading_platform.domain.models import Instrument, Trade


class ExitTrigger(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TARGET = "TARGET"
    TRAILING_STOP = "TRAILING_STOP"
    EXPIRY = "EXPIRY"
    MANUAL = "MANUAL"
    KILL_SWITCH = "KILL_SWITCH"


@dataclass
class ExitPlan:
    """Created immediately after every entry fill.

    Holds SL, target, trailing-stop, and expiry thresholds.
    The ExitManager polls active plans and emits exit OrderIntents
    when any condition is triggered.
    """

    plan_id: str = field(default_factory=lambda: uuid4().hex)
    trade: Trade | None = None
    instrument: Instrument | None = None
    symbol: str = ""
    entry_price: float = 0.0
    quantity: int = 0
    strategy_name: str = ""
    side: str = "BUY"

    stop_loss_price: float | None = None
    target_price: float | None = None
    trailing_pct: float | None = None
    expiry_date: date | None = None

    _highest_price: float = field(default=0.0, init=False, repr=False)
    _lowest_price: float = field(default=float("inf"), init=False, repr=False)

    active: bool = True
    triggered_by: ExitTrigger | None = None
    triggered_at: datetime | None = None

    def __post_init__(self) -> None:
        self._highest_price = self.entry_price
        self._lowest_price = self.entry_price

    @classmethod
    def from_trade(
        cls,
        trade: Trade,
        instrument: Instrument,
        stop_loss_pct: float = 0.015,
        target_pct: float = 0.025,
        trailing_pct: float | None = None,
        expiry_date: date | None = None,
    ) -> ExitPlan:
        ep = trade.price
        sign = 1 if trade.side.value == "BUY" else -1
        sl = ep * (1 - sign * stop_loss_pct)
        tgt = ep * (1 + sign * target_pct)
        plan = cls(
            trade=trade,
            instrument=instrument,
            symbol=trade.symbol,
            entry_price=ep,
            quantity=trade.quantity,
            strategy_name=trade.strategy_name,
            side=trade.side.value,
            stop_loss_price=sl,
            target_price=tgt,
            trailing_pct=trailing_pct,
            expiry_date=expiry_date,
        )
        plan._highest_price = ep
        plan._lowest_price = ep
        return plan

    def update_trailing(self, current_price: float) -> None:
        if self.trailing_pct is None:
            return
        if self.side == "BUY":
            self._highest_price = max(self._highest_price, current_price)
            new_sl = self._highest_price * (1 - self.trailing_pct)
            if self.stop_loss_price is None or new_sl > self.stop_loss_price:
                self.stop_loss_price = new_sl
        else:
            self._lowest_price = min(self._lowest_price, current_price)
            new_sl = self._lowest_price * (1 + self.trailing_pct)
            if self.stop_loss_price is None or new_sl < self.stop_loss_price:
                self.stop_loss_price = new_sl

    def check_trigger(self, current_price: float, now: datetime) -> ExitTrigger | None:
        if not self.active:
            return None

        self.update_trailing(current_price)

        if self.expiry_date and now.date() >= self.expiry_date:
            return ExitTrigger.EXPIRY

        if self.side == "BUY":
            if self.stop_loss_price and current_price <= self.stop_loss_price:
                return ExitTrigger.STOP_LOSS
            if self.target_price and current_price >= self.target_price:
                return ExitTrigger.TARGET
        else:
            if self.stop_loss_price and current_price >= self.stop_loss_price:
                return ExitTrigger.STOP_LOSS
            if self.target_price and current_price <= self.target_price:
                return ExitTrigger.TARGET

        return None

    def mark_triggered(self, trigger: ExitTrigger, now: datetime) -> None:
        self.active = False
        self.triggered_by = trigger
        self.triggered_at = now

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "strategy_name": self.strategy_name,
            "side": self.side,
            "stop_loss_price": self.stop_loss_price,
            "target_price": self.target_price,
            "trailing_pct": self.trailing_pct,
            "expiry_date": self.expiry_date.isoformat() if self.expiry_date else None,
            "active": self.active,
            "triggered_by": self.triggered_by.value if self.triggered_by else None,
            "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
        }
