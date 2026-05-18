from __future__ import annotations

import collections
import threading
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from trading_platform.domain.enums import Side
from trading_platform.domain.models import Instrument, Order, Position, Trade


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    drawdown: float
    peak_equity: float
    open_positions: int


class PortfolioLedger:
    def __init__(self, initial_capital: float):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: dict[str, Position] = {}
        self.trades: collections.deque[Trade] = collections.deque(maxlen=10_000)
        self.equity_curve: list[tuple[datetime, float]] = []
        self.peak_equity = initial_capital
        # Protects trades list, positions dict, cash, and peak_equity against
        # concurrent mutation from broker-callback threads and the asyncio loop.
        self._lock = threading.Lock()

    def mark_to_market(self, timestamp: datetime, mark_prices: dict[str, float]) -> PortfolioSnapshot:
        with self._lock:
            unrealized = 0.0
            for symbol, position in self.positions.items():
                mark = mark_prices.get(symbol, position.average_price)
                unrealized += position.unrealized_pnl(mark)
            equity = self.cash + sum(
                position.market_value(mark_prices.get(symbol, position.average_price))
                for symbol, position in self.positions.items()
            )
            self.peak_equity = max(self.peak_equity, equity)
            drawdown = 0.0 if self.peak_equity <= 0 else max(0.0, (self.peak_equity - equity) / self.peak_equity)
            self.equity_curve.append((timestamp, equity))
            if len(self.equity_curve) > 5000:
                self.equity_curve = self.equity_curve[-5000:]
            return PortfolioSnapshot(
                cash=self.cash,
                equity=equity,
                realized_pnl=sum(position.realized_pnl for position in self.positions.values()),
                unrealized_pnl=unrealized,
                drawdown=drawdown,
                peak_equity=self.peak_equity,
                open_positions=sum(1 for position in self.positions.values() if position.quantity != 0),
            )

    def apply_fill(self, order: Order, fill_price: float, timestamp: datetime, charges: float = 0.0) -> Trade:
        with self._lock:
            intent = order.intent
            symbol = intent.instrument.symbol
            signed_quantity = intent.signal.side.sign * intent.quantity
            position = self.positions.get(symbol) or Position(intent.instrument)
            existing_quantity = position.quantity
            lot_size = intent.instrument.lot_size
            fill_value = fill_price * signed_quantity * lot_size

            if intent.signal.side == Side.BUY:
                self.cash -= abs(fill_value) + charges
            else:
                self.cash += abs(fill_value) - charges

            if existing_quantity == 0 or (existing_quantity > 0) == (signed_quantity > 0):
                total_quantity = existing_quantity + signed_quantity
                weighted_cost = (
                    position.average_price * abs(existing_quantity)
                    + fill_price * abs(signed_quantity)
                )
                if abs(total_quantity) == 0:
                    raise ValueError(f"apply_fill: zero total_quantity for {symbol} — check compliance filter")
                position.average_price = weighted_cost / abs(total_quantity)
                position.quantity = total_quantity
            else:
                closing_quantity = min(abs(existing_quantity), abs(signed_quantity))
                realized = closing_quantity * (fill_price - position.average_price) * (1 if existing_quantity > 0 else -1) * lot_size
                position.realized_pnl += realized  # charges already deducted from cash
                remaining = existing_quantity + signed_quantity
                position.quantity = remaining
                if remaining == 0:
                    position.average_price = 0.0
                elif abs(signed_quantity) > abs(existing_quantity):
                    position.average_price = fill_price

            self.positions[symbol] = position
            trade = Trade(
                trade_id=uuid4().hex,
                order_id=order.broker_order_id or order.intent.idempotency_key,
                symbol=symbol,
                side=intent.signal.side,
                quantity=intent.quantity,
                price=fill_price,
                charges=charges,
                timestamp=timestamp,
                strategy_name=intent.signal.strategy_name,
            )
            self.trades.append(trade)
            return trade

    @property
    def equity(self) -> float:
        """Best-effort equity estimate: cash + cost basis of open positions.

        Uses average entry price as the mark since no live price is available here.
        Callers that need a live mark-to-market should use mark_to_market() instead.
        Cost-basis equity is appropriate for session-start baselines because the
        daily P&L circuit breaker measures today's change, not absolute unrealized P&L.
        """
        with self._lock:
            cost_basis = sum(
                abs(pos.quantity) * pos.average_price * pos.instrument.lot_size
                for pos in self.positions.values()
                if pos.quantity != 0
            )
            return self.cash + cost_basis

    def position_symbols(self) -> list[str]:
        with self._lock:
            return [symbol for symbol, position in self.positions.items() if position.quantity != 0]

