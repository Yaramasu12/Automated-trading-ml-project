"""
trading_platform/execution/smart_router.py — Smart order router + multi-leg hedge-first routing

Per §13 Phase 2: Multi-leg options routing: hedge-first leg sequencing (buy protective legs 
before selling), basket margin preview before commit.
Per §6.4: Execution alpha — limit-at-touch with 2-tick chase then market on urgency.
Per §6.2: Algo-ID plumbing for SEBI retail-algo compliance.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Order types and states
# ──────────────────────────────────────────────


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    LIMIT_AT_TOUCH = "LIMIT_AT_TOUCH"
    CHASE = "CHASE"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class LegType(str, Enum):
    """Type of options leg."""
    BUY_PROTECTIVE = "BUY_PROTECTIVE"      # Buy OTM put/call (hedge first)
    SELL_PREMIUM = "SELL_PREMIUM"           # Sell OTM put/call (premium)
    BUY_EXTRA = "BUY_EXTRA"                 # Buy extra leg (e.g., calendar)
    SELL_EXTRA = "SELL_EXTRA"               # Sell extra leg (e.g., condor outer)


@dataclass
class OrderRequest:
    """Represents an order request to the router."""
    instrument_id: str
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    price: Optional[Decimal] = None
    strategy_id: str = ""
    strategy_name: str = ""
    algo_id: str = ""  # SEBI retail-algo compliance
    tenant_id: str = "tenant_default"
    signal_hash: str = ""
    correlation_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Leg:
    """A single leg in a multi-leg order."""
    order: OrderRequest
    leg_type: LegType
    margin_contribution: Decimal = Decimal("0")
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: Optional[Decimal] = None
    fill_timestamp: Optional[float] = None
    algo_id: str = ""


@dataclass
class MultiLegOrder:
    """A basket of legs (e.g., iron condor = 4 legs)."""
    order_id: str
    legs: List[Leg]
    status: OrderStatus = OrderStatus.PENDING
    strategy_id: str = ""
    strategy_name: str = ""
    tenant_id: str = "tenant_default"
    margin_preview: Decimal = Decimal("0")
    submitted_at: Optional[float] = None
    filled_at: Optional[float] = None
    rejection_reason: Optional[str] = None
    signal_hash: str = ""
    algo_id: str = ""  # SEBI compliance — every order carries this


@dataclass
class Fill:
    """A fill/execution record."""
    order_id: str
    leg_index: int
    instrument_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: Decimal
    fill_time: float
    algo_id: str = ""
    strategy_id: str = ""
    correlation_id: str = ""


# ──────────────────────────────────────────────
# Smart order router
# ──────────────────────────────────────────────


class SmartOrderRouter:
    """
    Smart order router with:
    - Multi-leg options routing with hedge-first sequencing
    - Limit-at-touch with 2-tick chase
    - Slicing for depth-aware execution
    - Basket margin preview before commit
    - Algo-ID tagging for SEBI compliance
    - Transaction cost analysis (TCA) integration
    """

    def __init__(
        self,
        broker_adapter,
        margin_service=None,
        tca_service=None,
        risk_service=None,
        alert_callback=None,
        max_chase_ticks: int = 2,
        tick_size_map: Optional[Dict[str, Decimal]] = None,
    ):
        self.broker_adapter = broker_adapter
        self.margin_service = margin_service
        self.tca_service = tca_service
        self.risk_service = risk_service
        self.alert_callback = alert_callback
        self.max_chase_ticks = max_chase_ticks
        self.tick_size_map = tick_size_map or {}

        # Order tracking
        self._pending_orders: Dict[str, MultiLegOrder] = {}
        self._active_orders: Dict[str, MultiLegOrder] = {}
        self._fill_history: List[Fill] = []

        # Rate limiting
        self._order_timestamps: List[float] = []
        self._max_orders_per_second: float = 5.0

    async def submit(self, order: OrderRequest) -> str:
        """
        Submit a single order. Returns the order ID.
        """
        # Generate correlation ID
        correlation_id = self._generate_correlation_id(order)
        order.correlation_id = correlation_id

        # Validate order
        await self._validate_order(order)

        # Create order record
        order_id = self._generate_order_id(order)
        order.instrument_id = order.instrument_id or order.symbol

        # Check rate limit
        await self._check_rate_limit()

        # Submit to broker
        try:
            result = await self.broker_adapter.submit_order(order)
            order_id = result.get("order_id", order_id)
            order.status = OrderStatus.SUBMITTED

            self._active_orders[order_id] = MultiLegOrder(
                order_id=order_id,
                legs=[Leg(order=order, leg_type=LegType.BUY_EXTRA if order.side == OrderSide.BUY else LegType.SELL_EXTRA)],
                strategy_id=order.strategy_id,
                strategy_name=order.strategy_name,
                tenant_id=order.tenant_id,
                signal_hash=order.signal_hash,
                algo_id=order.algo_id,
            )

            logger.info(f"[ORDER] Order submitted: {order_id} {order.side} {order.symbol} "
                       f"qty={order.quantity} type={order.order_type}")

            # TCA tracking
            if self.tca_service:
                self.tca_service.track_order(order_id, order)

            return order_id

        except Exception as exc:
            logger.error(f"[ORDER] Order submission failed for {order.symbol}: {exc}", exc_info=True)
            await self._on_order_failure(order, str(exc))
            raise

    async def submit_multileg(self, multi_leg: MultiLegOrder) -> str:
        """
        Submit a multi-leg order with hedge-first sequencing.

        Hedge-first: buy protective legs BEFORE selling premium legs.
        This ensures the position is always hedged before taking on short risk.
        """
        order_id = multi_leg.order_id
        multi_leg.submitted_at = time.time()

        # Pre-flight: margin preview
        if self.margin_service:
            try:
                margin_req = await self.margin_service.preview_margin(multi_leg)
                multi_leg.margin_preview = margin_req
                logger.info(f"[MARGIN] Preview for {order_id}: ₹{margin_req}")
            except Exception as exc:
                logger.error(f"[MARGIN] Margin preview failed for {order_id}: {exc}")
                multi_leg.rejection_reason = f"Margin preview failed: {exc}"
                multi_leg.status = OrderStatus.REJECTED
                return order_id

        # Hedge-first sequencing: sort legs so BUY_PROTECTIVE comes first
        sorted_legs = self._sort_legs_by_priority(multi_leg.legs)

        # Execute legs sequentially (hedge-first)
        filled_legs: List[Leg] = []
        failed_leg: Optional[Leg] = None

        for i, leg in enumerate(sorted_legs):
            try:
                fill = await self._execute_leg(leg, i)
                filled_legs.append(leg)
                leg.status = OrderStatus.FILLED
                leg.filled_qty = leg.order.quantity
                leg.avg_fill_price = fill.price
                leg.fill_timestamp = fill.fill_time

                multi_leg.legs[i] = leg

                logger.info(f"[LEG] Leg {i} filled: {leg.order.side} {leg.order.symbol} "
                           f"qty={fill.quantity} @ ₹{fill.price}")

            except Exception as exc:
                logger.error(f"[LEG] Leg {i} failed: {exc}")
                failed_leg = leg
                leg.status = OrderStatus.REJECTED
                multi_leg.legs[i] = leg
                break

        # Post-flight: update status
        if failed_leg:
            # Partial fill — cancel remaining legs or square off
            logger.warning(f"[MULTILEG] Partial fill for {order_id}: {len(filled_legs)}/{len(sorted_legs)} legs filled")
            multi_leg.status = OrderStatus.PARTIAL
            multi_leg.rejection_reason = f"Leg {sorted_legs.index(failed_leg)} failed: {exc}"

            # Cancel unfilled legs
            for leg in multi_leg.legs:
                if leg.status == OrderStatus.PENDING:
                    await self._cancel_leg(leg, "partial_fill_cancellation")

            # Alert on partial fill (critical for options — always hedged)
            if self.alert_callback:
                await self.alert_callback(
                    "WARN",
                    "Partial multi-leg fill",
                    f"{order_id}: {len(filled_legs)}/{len(sorted_legs)} legs filled. "
                    f"Remaining legs cancelled.",
                )
        elif len(filled_legs) == len(sorted_legs):
            multi_leg.status = OrderStatus.FILLED
            multi_leg.filled_at = time.time()
            logger.info(f"[MULTILEG] All legs filled for {order_id}")
        else:
            multi_leg.status = OrderStatus.REJECTED
            multi_leg.rejection_reason = "No legs filled"

        self._active_orders[order_id] = multi_leg
        return order_id

    async def _execute_leg(self, leg: Leg, leg_index: int) -> Fill:
        """Execute a single leg with smart routing logic."""
        order = leg.order

        # Determine execution strategy based on order type
        if order.order_type == OrderType.MARKET:
            fill = await self._execute_market(order, leg)
        elif order.order_type == OrderType.LIMIT_AT_TOUCH:
            fill = await self._execute_limit_at_touch(order, leg)
        elif order.order_type == OrderType.CHASE:
            fill = await self._execute_chase(order, leg)
        else:
            # Default to limit
            fill = await self._execute_limit(order, leg)

        # Record fill
        fill_record = Fill(
            order_id=leg.order.correlation_id or leg.order.instrument_id,
            leg_index=leg_index,
            instrument_id=order.instrument_id,
            symbol=order.symbol,
            side=order.side,
            quantity=fill.quantity,
            price=fill.price,
            fill_time=time.time(),
            algo_id=leg.algo_id or order.algo_id,
            strategy_id=order.strategy_id,
            correlation_id=order.correlation_id,
        )
        self._fill_history.append(fill_record)

        # TCA: record fill for transaction cost analysis
        if self.tca_service:
            self.tca_service.record_fill(fill_record)

        return fill

    async def _execute_limit_at_touch(self, order: OrderRequest, leg: Leg) -> Fill:
        """
        Limit-at-touch with 2-tick chase then market on urgency.

        Per §6.4: This is a measurable, controllable edge — 10-20 bps/trade.
        """
        # Get best bid/ask from market data
        depth = await self.broker_adapter.get_depth(order.symbol)
        if not depth or not depth.get("best_bid") or not depth.get("best_ask"):
            # Fallback to limit price
            return await self._execute_limit(order, leg)

        best_bid = Decimal(str(depth["best_bid"]))
        best_ask = Decimal(str(depth["best_ask"]))
        tick_size = self.tick_size_map.get(order.symbol, Decimal("0.05"))

        # Initial limit: best bid (for BUY) or best ask (for SELL)
        if order.side == OrderSide.BUY:
            limit_price = best_ask  # Buy at ask
        else:
            limit_price = best_bid  # Sell at bid

        # Chase up to 2 ticks
        for chase in range(self.max_chase_ticks + 1):
            if order.side == OrderSide.BUY:
                test_price = limit_price + tick_size * chase
                if test_price >= best_ask:
                    # Price at or above ask — submit as market
                    return await self._execute_market(order, leg)
            else:
                test_price = limit_price - tick_size * chase
                if test_price <= best_bid:
                    return await self._execute_market(order, leg)

            # Try limit order at test_price
            order.price = test_price
            try:
                result = await self.broker_adapter.submit_order(order)
                if result.get("filled"):
                    return Fill(
                        order_id=order.correlation_id or order.symbol,
                        leg_index=0,
                        instrument_id=order.instrument_id,
                        symbol=order.symbol,
                        side=order.side,
                        quantity=result.get("filled_qty", order.quantity),
                        price=test_price,
                        fill_time=time.time(),
                    )
            except Exception:
                pass

            # Brief pause before next chase
            await asyncio.sleep(0.05)  # 50ms between chase attempts

        # All chases exhausted — submit as market
        order.order_type = OrderType.MARKET
        return await self._execute_market(order, leg)

    async def _execute_market(self, order: OrderRequest, leg: Leg) -> Fill:
        """Execute a market order."""
        result = await self.broker_adapter.submit_order(order)
        return Fill(
            order_id=order.correlation_id or order.symbol,
            leg_index=0,
            instrument_id=order.instrument_id,
            symbol=order.symbol,
            side=order.side,
            quantity=result.get("filled_qty", order.quantity),
            price=result.get("avg_price", Decimal("0")),
            fill_time=time.time(),
        )

    async def _execute_limit(self, order: OrderRequest, leg: Leg) -> Fill:
        """Execute a limit order."""
        order.order_type = OrderType.LIMIT
        result = await self.broker_adapter.submit_order(order)
        return Fill(
            order_id=order.correlation_id or order.symbol,
            leg_index=0,
            instrument_id=order.instrument_id,
            symbol=order.symbol,
            side=order.side,
            quantity=result.get("filled_qty", order.quantity),
            price=result.get("avg_price", order.price or Decimal("0")),
            fill_time=time.time(),
        )

    async def _execute_chase(self, order: OrderRequest, leg: Leg) -> Fill:
        """Execute with chase logic (limit → chase → market)."""
        # Use limit-at-touch logic for chase
        return await self._execute_limit_at_touch(order, leg)

    def _sort_legs_by_priority(self, legs: List[Leg]) -> List[Leg]:
        """
        Sort legs by priority: BUY_PROTECTIVE first, then SELL_PREMIUM, then extras.
        This implements hedge-first sequencing.
        """
        priority = {
            LegType.BUY_PROTECTIVE: 0,
            LegType.SELL_PREMIUM: 1,
            LegType.BUY_EXTRA: 2,
            LegType.SELL_EXTRA: 3,
        }
        return sorted(legs, key=lambda l: priority.get(l.leg_type, 99))

    async def _validate_order(self, order: OrderRequest) -> None:
        """Validate order before submission."""
        # Risk check
        if self.risk_service:
            await self.risk_service.pre_trade_check(order)

        # Algo-ID compliance (SEBI §6.2)
        if not order.algo_id:
            raise ValueError(
                f"Algo-ID required for all automated orders. "
                f"Strategy: {order.strategy_id}, Tenant: {order.tenant_id}"
            )

    async def _check_rate_limit(self) -> None:
        """Check order rate limit (sliding window)."""
        now = time.time()
        # Clean old timestamps
        self._order_timestamps = [
            t for t in self._order_timestamps
            if now - t < 1.0
        ]
        if len(self._order_timestamps) >= self._max_orders_per_second:
            raise RuntimeError(
                f"Order rate limit exceeded: {self._max_orders_per_second}/sec"
            )
        self._order_timestamps.append(now)

    def _generate_order_id(self, order: OrderRequest) -> str:
        """Generate a unique order ID."""
        raw = f"{order.strategy_id}:{order.symbol}:{order.side}:{time.time()}"
        return f"ORD-{hashlib.sha256(raw.encode()).hexdigest()[:12].upper()}"

    def _generate_correlation_id(self, order: OrderRequest) -> str:
        """Generate a correlation ID for order tracking."""
        raw = f"{order.tenant_id}:{order.strategy_id}:{order.symbol}:{order.side}:{time.time()}"
        return f"corr-{hashlib.md5(raw.encode()).hexdigest()[:16]}"

    async def _cancel_leg(self, leg: Leg, reason: str) -> None:
        """Cancel a single leg."""
        try:
            await self.broker_adapter.cancel_order(leg.order)
            leg.status = OrderStatus.CANCELLED
            logger.info(f"[LEG] Leg cancelled: {reason}")
        except Exception as exc:
            logger.error(f"[LEG] Cancel failed: {exc}")

    async def _on_order_failure(self, order: OrderRequest, reason: str) -> None:
        """Handle order failure."""
        if self.alert_callback:
            await self.alert_callback(
                "WARN",
                "Order failed",
                f"{order.side} {order.symbol}: {reason}",
            )

    def get_order_status(self, order_id: str) -> Optional[MultiLegOrder]:
        """Get order status."""
        return self._active_orders.get(order_id) or self._pending_orders.get(order_id)

    def get_fill_history(self, strategy_id: Optional[str] = None) -> List[Fill]:
        """Get fill history, optionally filtered by strategy."""
        if strategy_id:
            return [f for f in self._fill_history if f.strategy_id == strategy_id]
        return list(self._fill_history)

    def get_tca_summary(self) -> Dict[str, Any]:
        """Get TCA (Transaction Cost Analysis) summary."""
        if not self.tca_service:
            return {}
        return self.tca_service.get_summary()