"""
trading_platform/tenancy/portfolio_ledger.py — Per-tenant portfolio ledger + simulated broker

Per §16: Multi-tenant architecture. Each tenant has:
- Isolated PortfolioLedger (positions, P&L, equity curve)
- Independent kill switch (tenant-scoped)
- Order submission to broker via BrokerSession
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Protocol

from trading_platform.tenancy.broker_session import BrokerSession, BrokerSessionManager

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Order states
# ──────────────────────────────────────────────


class OrderState(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# ──────────────────────────────────────────────
# Order
# ──────────────────────────────────────────────


@dataclass
class Order:
    """Tenant order."""
    order_id: str
    tenant_id: str
    instrument_id: str
    side: str  # BUY / SELL
    quantity: Decimal
    price: Decimal
    order_type: str  # MARKET / LIMIT
    status: OrderState = OrderState.PENDING
    filled_quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    strategy_id: Optional[str] = None
    signal_hash: Optional[str] = None
    algo_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    rejected_reason: Optional[str] = None
    fill_events: List[Dict[str, Any]] = field(default_factory=list)


# ──────────────────────────────────────────────
# Position
# ──────────────────────────────────────────────


@dataclass
class Position:
    """Tenant position."""
    instrument_id: str
    tenant_id: str
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")
    open_count: int = 0
    close_count: int = 0


# ──────────────────────────────────────────────
# Daily P&L
# ──────────────────────────────────────────────


@dataclass
class DailyPnL:
    """Daily P&L for a tenant."""
    tenant_id: str
    date: str
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    total_pnl: Decimal = Decimal("0")
    gross_pnl: Decimal = Decimal("0")
    costs: Decimal = Decimal("0")  # brokerage, STT, GST, stamp
    trades_count: int = 0
    win_count: int = 0
    loss_count: int = 0


# ──────────────────────────────────────────────
# Kill switch
# ──────────────────────────────────────────────


class TenantKillSwitch:
    """Per-tenant kill switch."""

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        self.active = False
        self.reason: Optional[str] = None
        self.last_triggered: Optional[float] = None
        self.daily_loss_limit: Optional[Decimal] = None
        self.daily_loss_current: Decimal = Decimal("0")
        self.max_drawdown: Optional[Decimal] = None
        self.peak_equity: Decimal = Decimal("0")
        self.current_equity: Decimal = Decimal("0")

    def trigger(self, reason: str) -> None:
        """Trigger kill switch."""
        self.active = True
        self.reason = reason
        self.last_triggered = time.time()
        logger.warning(f"[KILLSWITCH tenant={self.tenant_id}] Triggered: {reason}")

    def reset(self) -> None:
        """Reset kill switch."""
        self.active = False
        self.reason = None
        self.last_triggered = None

    def check_daily_loss(self, daily_loss: Decimal) -> Optional[str]:
        """Check if daily loss limit exceeded. Returns reason or None."""
        if self.daily_loss_limit is None:
            return None
        if daily_loss >= self.daily_loss_limit:
            reason = f"Daily loss {daily_loss} >= limit {self.daily_loss_limit}"
            self.trigger(reason)
            return reason
        return None

    def check_drawdown(self, current_equity: Decimal, peak_equity: Decimal, max_drawdown: Decimal) -> Optional[str]:
        """Check if drawdown exceeds limit."""
        if peak_equity <= 0:
            return None
        drawdown = (peak_equity - current_equity) / peak_equity
        if drawdown >= max_drawdown:
            reason = f"Drawdown {drawdown:.4f} >= limit {max_drawdown}"
            self.trigger(reason)
            return reason
        return None


# ──────────────────────────────────────────────
# PortfolioLedger
# ──────────────────────────────────────────────


class PortfolioLedger:
    """
    Per-tenant portfolio ledger.

    Tracks:
    - Positions (quantity, avg price, realized/unrealized P&L)
    - Order lifecycle (pending → submitted → filled/rejected)
    - Daily P&L (realized, unrealized, costs)
    - Equity curve (peak, current)
    """

    def __init__(
        self,
        tenant_id: str,
        broker_session: Optional[BrokerSession] = None,
        initial_capital: Decimal = Decimal("1000000"),
        daily_loss_limit: Optional[Decimal] = None,
        max_drawdown: Optional[Decimal] = None,
        alert_callback=None,
    ):
        self.tenant_id = tenant_id
        self.broker_session = broker_session
        self.initial_capital = initial_capital
        self.daily_loss_limit = daily_loss_limit
        self.max_drawdown = max_drawdown
        self.alert_callback = alert_callback

        # Positions: instrument_id → Position
        self._positions: Dict[str, Position] = {}

        # Orders: order_id → Order
        self._orders: Dict[str, Order] = {}

        # Pending orders (awaiting fill)
        self._pending_orders: Deque[Order] = deque()

        # Current equity
        self._current_equity = initial_capital
        self._peak_equity = initial_capital

        # Daily P&L
        self._daily_pnl = Decimal("0")

        # Kill switch
        self.kill_switch = TenantKillSwitch(tenant_id)

        # Order ID counter
        self._order_counter = 0

    @property
    def current_equity(self) -> Decimal:
        """Current equity = initial_capital + realized_pnl + unrealized_pnl."""
        total_realized = sum(p.realized_pnl for p in self._positions.values())
        total_unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        return self.initial_capital + total_realized + total_unrealized

    @property
    def peak_equity(self) -> Decimal:
        return self._peak_equity

    @property
    def positions(self) -> Dict[str, Position]:
        return dict(self._positions)

    @property
    def pending_orders(self) -> List[Order]:
        return list(self._pending_orders)

    async def submit_order(
        self,
        signal: Any,
        risk_result: Any = None,
    ) -> Order:
        """Submit an order from a signal."""
        self._order_counter += 1
        order_id = f"ORD-{self.tenant_id[:4]}-{self._order_counter:06d}"

        order = Order(
            order_id=order_id,
            tenant_id=self.tenant_id,
            instrument_id=signal.instrument_id,
            side=signal.side,
            quantity=signal.quantity,
            price=signal.price,
            order_type=signal.order_type,
            strategy_id=signal.strategy_id,
            signal_hash=signal.signal_hash,
            algo_id=signal.algo_id if hasattr(signal, 'algo_id') else None,
        )

        self._orders[order_id] = order
        self._pending_orders.append(order)
        logger.info(f"[LEDGER tenant={self.tenant_id}] Order submitted: {order_id} {signal.side} {signal.quantity} {signal.instrument_id}")

        # Check kill switch before sending to broker
        if self.kill_switch.active:
            order.status = OrderState.REJECTED
            order.rejected_reason = f"Kill switch active: {self.kill_switch.reason}"
            self._pending_orders.remove(order)
            logger.warning(f"[LEDGER tenant={self.tenant_id}] Order rejected: kill switch active")
            if self.alert_callback:
                await self.alert_callback("CRITICAL", "Kill switch active", self.kill_switch.reason)
            return order

        # Send to broker
        if self.broker_session:
            try:
                from trading_platform.broker.angel_one_adapter import AngelOneAdapter
                adapter = AngelOneAdapter()
                broker_order = await adapter.place_order(
                    order=order,
                    algo_id=order.algo_id or self.broker_session.credentials.algo_id,
                )
                order.status = OrderState.SUBMITTED
                if broker_order:
                    order.filled_quantity = broker_order.get("filled_quantity", Decimal("0"))
                    order.average_price = broker_order.get("average_price", Decimal("0"))
            except Exception as exc:
                order.status = OrderState.REJECTED
                order.rejected_reason = str(exc)
                self._pending_orders.remove(order)
                logger.error(f"[LEDGER tenant={self.tenant_id}] Order rejected: {exc}")
                if self.alert_callback:
                    await self.alert_callback("WARN", "Order rejected", f"{order_id}: {exc}")

        return order

    async def process_fill(self, order_id: str, filled_quantity: Decimal, fill_price: Decimal) -> None:
        """Process a fill from broker."""
        order = self._orders.get(order_id)
        if not order:
            logger.warning(f"[LEDGER tenant={self.tenant_id}] Unknown order fill: {order_id}")
            return

        order.filled_quantity += filled_quantity
        order.average_price = fill_price
        if order.filled_quantity >= order.quantity:
            order.status = OrderState.FILLED
            if order in self._pending_orders:
                self._pending_orders.remove(order)

        # Update position
        position = self._positions.get(order.instrument_id)
        if not position:
            position = Position(
                instrument_id=order.instrument_id,
                tenant_id=self.tenant_id,
            )
            self._positions[order.instrument_id] = position

        if order.side == "BUY":
            cost = filled_quantity * fill_price
            position.quantity += filled_quantity
            # Recalculate avg price
            if position.quantity > 0:
                position.average_price = (
                    (position.quantity - filled_quantity) * position.average_price + filled_quantity * fill_price
                ) / position.quantity
        else:
            # SELL
            pnl = (fill_price - position.average_price) * filled_quantity
            position.realized_pnl += pnl
            position.quantity -= filled_quantity
            position.close_count += 1

        position.total_fees += self._calculate_costs(order, fill_price, filled_quantity)
        self._daily_pnl += pnl

        # Update equity
        self._current_equity = self.current_equity
        if self._current_equity > self._peak_equity:
            self._peak_equity = self._current_equity

        logger.info(f"[LEDGER tenant={self.tenant_id}] Fill: {order_id} {filled_quantity} @ {fill_price}")

        # Check kill switch conditions
        self._check_kill_switch()

    def _calculate_costs(self, order: Order, price: Decimal, quantity: Decimal) -> Decimal:
        """Calculate trading costs (brokerage, STT, GST, stamp, exchange)."""
        turnover = price * quantity
        # India cost model
        brokerage = max(Decimal("20"), turnover * Decimal("0.0001"))  # 0.01% or ₹20
        stt = Decimal("0")
        if order.side == "SELL":
            stt = turnover * Decimal("0.00025")  # 0.025% on sell side
        exchange_txn = turnover * Decimal("0.00001")
        stamp = turnover * Decimal("0.00003")  # Varies by state
        gst = (brokerage + exchange_txn) * Decimal("0.18")
        sebi = turnover * Decimal("0.0000001")
        stamp_duty = turnover * Decimal("0.00001")  # Stamp duty on buy
        total = brokerage + stt + exchange_txn + stamp + gst + sebi + stamp_duty
        return total

    def _check_kill_switch(self) -> None:
        """Check kill switch conditions."""
        # Daily loss check
        if self.daily_loss_limit:
            reason = self.kill_switch.check_daily_loss(self._daily_pnl)
            if reason:
                return

        # Drawdown check
        if self.max_drawdown:
            reason = self.kill_switch.check_drawdown(
                self._current_equity, self._peak_equity, self.max_drawdown
            )
            if reason:
                return

    def get_pnl(self) -> Dict[str, Any]:
        """Get current P&L summary."""
        total_realized = sum(p.realized_pnl for p in self._positions.values())
        total_unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        return {
            "tenant_id": self.tenant_id,
            "initial_capital": str(self.initial_capital),
            "realized_pnl": str(total_realized),
            "unrealized_pnl": str(total_unrealized),
            "current_equity": str(self._current_equity),
            "peak_equity": str(self._peak_equity),
            "daily_pnl": str(self._daily_pnl),
            "drawdown": str((self._peak_equity - self._current_equity) / self._peak_equity if self._peak_equity > 0 else 0),
            "kill_switch_active": self.kill_switch.active,
            "kill_switch_reason": self.kill_switch.reason,
        }

    async def square_off_all(self, reason: str) -> List[Order]:
        """Emergency square-off of all positions."""
        orders = []
        for instrument_id, position in list(self._positions.items()):
            if position.quantity != 0:
                side = "SELL" if position.quantity > 0 else "BUY"
                quantity = abs(position.quantity)
                # Market order to square off
                signal = type('Signal', (), {
                    'instrument_id': instrument_id,
                    'side': side,
                    'quantity': quantity,
                    'price': Decimal("0"),  # Market
                    'order_type': 'MARKET',
                    'strategy_id': 'EMERGENCY_SQUARE_OFF',
                    'signal_hash': None,
                })()
                order = await self.submit_order(signal)
                orders.append(order)
        logger.warning(f"[LEDGER tenant={self.tenant_id}] Emergency square-off: {reason}")
        if self.alert_callback:
            await self.alert_callback("CRITICAL", "Emergency square-off", reason)
        return orders


# ──────────────────────────────────────────────
# SimulatedBrokerClient (for paper trading)
# ──────────────────────────────────────────────


class SimulatedBrokerClient(PortfolioLedger):
    """
    Per-tenant simulated broker client for paper trading.

    Per §16.4: Paper-only display with synthetic data. Each paper user gets:
    - Independent simulated broker + portfolio ledger
    - Same slippage/cost model as live
    - Isolated equity curve
    """

    def __init__(
        self,
        tenant_id: str,
        initial_capital: Decimal = Decimal("1000000"),
        slippage_bps: Decimal = Decimal("5"),  # 5 bps slippage
        **kwargs,
    ):
        super().__init__(
            tenant_id=tenant_id,
            initial_capital=initial_capital,
            alert_callback=kwargs.get("alert_callback"),
        )
        self.slippage_bps = slippage_bps
        self._ltp_cache: Dict[str, Decimal] = {}  # instrument_id → last trade price

    def set_ltp(self, instrument_id: str, price: Decimal) -> None:
        """Set last trade price for an instrument (from market data)."""
        self._ltp_cache[instrument_id] = price

    async def submit_order(
        self,
        signal: Any,
        risk_result: Any = None,
    ) -> Order:
        """Submit a simulated order — instant fill at LTP + slippage."""
        # Use set LTP or signal price
        ltp = self._ltp_cache.get(signal.instrument_id)
        if ltp and signal.price <= 0:
            fill_price = ltp
        elif signal.price > 0:
            fill_price = signal.price
        else:
            fill_price = Decimal("100")  # Default fallback

        # Apply slippage
        if signal.side == "BUY":
            fill_price = fill_price * (Decimal("1") + self.slippage_bps / Decimal("10000"))
        else:
            fill_price = fill_price * (Decimal("1") - self.slippage_bps / Decimal("10000"))

        # Create order
        self._order_counter += 1
        order_id = f"SIM-{self.tenant_id[:4]}-{self._order_counter:06d}"
        order = Order(
            order_id=order_id,
            tenant_id=self.tenant_id,
            instrument_id=signal.instrument_id,
            side=signal.side,
            quantity=signal.quantity,
            price=signal.price,
            order_type=signal.order_type,
            status=OrderState.FILLED,
            filled_quantity=signal.quantity,
            average_price=fill_price,
            strategy_id=signal.strategy_id,
            signal_hash=signal.signal_hash,
        )

        self._orders[order_id] = order

        # Process fill
        await self.process_fill(order_id, signal.quantity, fill_price)

        logger.info(f"[SIM tenant={self.tenant_id}] Filled: {order_id} {signal.side} {signal.quantity} @ {fill_price}")
        return order

    def update_unrealized_pnl(self) -> None:
        """Update unrealized P&L based on LTP cache."""
        for instrument_id, position in self._positions.items():
            if position.quantity != 0 and instrument_id in self._ltp_cache:
                ltp = self._ltp_cache[instrument_id]
                if position.quantity > 0:
                    pnl = (ltp - position.average_price) * position.quantity
                else:
                    pnl = (position.average_price - ltp) * abs(position.quantity)
                position.unrealized_pnl = pnl