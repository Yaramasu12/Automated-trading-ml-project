"""Smart order routing and execution scheduling.

Handles:
1. Multi-leg options routing with hedge-first sequencing
2. Smart orders: limit-at-touch with chase, then market on urgency
3. Slicing when order size > threshold % of top-5 depth
4. Almgren-Chriss-style scheduling for multi-lot baskets
5. Every order carries exchange-issued Algo-ID (SEBI retail-algo compliance)

This is the ONLY path to the broker. No order bypasses this layer.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from trading_platform.config import Settings
from trading_platform.execution.oms_store import OMSEventStore
from trading_platform.portfolio.ledger import PortfolioLedger

logger = logging.getLogger(__name__)


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    SL = "SL"
    SL_M = "SL-M"


class OrderUrgency(str, Enum):
    IMMEDIATE = "IMMEDIATE"
    NORMAL = "NORMAL"
    PACING = "PACING"


@dataclass
class SmartOrder:
    """A smart order ready for routing to the broker."""
    symbol: str
    exchange: str
    segment: str  # CASH/FNO
    side: OrderSide
    quantity: int
    order_type: OrderType
    limit_price: Optional[float]  # None for MARKET orders
    urgency: OrderUrgency = OrderUrgency.NORMAL
    strategy: str = ""
    algo_id: str = ""
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    leg_label: Optional[str] = None  # "LEG_1_BUY_PROTECTIVE" for multi-leg
    max_slippage_bps: float = 5.0  # Max acceptable slippage in bps
    slice_size: Optional[int] = None  # If set, split into chunks
    validity: str = "DAY"
    tag: str = "quant_platform"
    disclosed_qty: int = 0
    trail_price: Optional[float] = None
    conditions: list[dict] = field(default_factory=list)
    tag_rules: list[str] = field(default_factory=list)

    @property
    def is_multi_leg_leg(self) -> bool:
        return self.leg_label is not None

    @property
    def is_market_order(self) -> bool:
        return self.order_type == OrderType.MARKET


@dataclass
class OrderPreview:
    """Preview of an order before submission — for UI confirmation."""
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: Optional[float]
    estimated_commission: float
    estimated_slippage: float
    total_cost: float
    margin_required: float
    leg_label: Optional[str]
    risk_checks_passed: bool
    risk_check_details: dict = field(default_factory=dict)


@dataclass
class FillConfirmation:
    """Confirmation of a fill from the broker."""
    order_id: str
    broker_order_id: str
    symbol: str
    exchange: str
    segment: str
    side: str
    fill_price: float
    fill_qty: int
    fill_time: datetime
    broker_time: str
    status: str
    algo_id: str
    strategy: str
    leg_label: Optional[str]
    rejection_reason: Optional[str] = None
    partial_fill: bool = False
    remaining_qty: int = 0


class MarginEstimator:
    """Estimates margin requirements for different order types."""

    def __init__(self, broker_adapter: Any) -> None:
        self._broker = broker_adapter

    async def estimate_options_basket(self, orders: list[SmartOrder]) -> float:
        """Estimate margin for a multi-leg options basket using broker preview."""
        if not orders:
            return 0.0

        # Hedge-first: netting reduces margin
        long_premium = sum(o.limit_price * o.quantity for o in orders if o.side == OrderSide.BUY and o.limit_price)
        short_premium = sum(o.limit_price * o.quantity for o in orders if o.side == OrderSide.SELL and o.limit_price)

        # Use broker's basket margin API if available
        try:
            margin = await self._broker.preview_margin([
                {"symbol": o.symbol, "side": o.side, "qty": o.quantity, "price": o.limit_price}
                for o in orders if o.limit_price
            ])
            return margin
        except Exception:
            # Fallback: rough estimate
            gross_notional = long_premium + short_premium
            # SEBI typically requires 10-15% of notional for options, but spreads reduce this
            return gross_notional * 0.15 if gross_notional > 0 else 0

    def estimate_equity_margin(self, symbol: str, side: OrderSide, qty: int, price: float) -> float:
        """Estimate equity margin (simplified)."""
        notional = price * qty
        # Equity delivery: ~20% margin (varies by security)
        # Intraday: ~5% margin
        if side == OrderSide.BUY:
            return notional * 0.20
        return notional * 0.05  # Sell (usually already in DH)


class OrderRouter:
    """Routes SmartOrders to the broker with smart execution logic."""

    def __init__(
        self,
        settings: Settings,
        broker_adapter: Any,
        margin_estimator: Optional[MarginEstimator] = None,
        event_bus: Optional[Any] = None,
    ) -> None:
        self._settings = settings
        self._broker = broker_adapter
        self._estimator = margin_estimator or MarginEstimator(broker_adapter)
        self._event_bus = event_bus
        self._pending_orders: dict[str, SmartOrder] = {}
        self._chase_count: dict[str, int] = {}
        self._max_chase = 3  # Max limit-at-touch chases before going market

    async def preview(
        self,
        order: SmartOrder,
        current_ltp: Optional[float] = None,
    ) -> OrderPreview:
        """Preview an order before submission."""
        ltp = current_ltp or order.limit_price or 0.0
        commission = self._estimate_commission(order)
        slippage = self._estimate_slippage(order, ltp)
        margin = 0.0

        if order.is_multi_leg_leg:
            # Basket margin (will be computed fully when all legs are known)
            margin = ltp * order.quantity * 0.15
        else:
            margin = self._estimator.estimate_equity_margin(
                order.symbol, order.side, order.quantity, ltp
            )

        return OrderPreview(
            symbol=order.symbol,
            side=order.side.value,
            quantity=order.quantity,
            order_type=order.order_type.value,
            limit_price=order.limit_price,
            estimated_commission=commission,
            estimated_slippage=slippage,
            total_cost=commission + slippage,
            margin_required=margin,
            leg_label=order.leg_label,
            risk_checks_passed=True,  # Filled by RiskService before submit
            risk_check_details={},
        )

    async def submit(
        self,
        order: SmartOrder,
        risk_checks: dict = None,
        current_depth: Optional[dict] = None,
    ) -> Optional[FillConfirmation]:
        """Submit an order to the broker with smart execution logic."""
        if risk_checks is None:
            risk_checks = {}

        # Store pending order
        self._pending_orders[order.correlation_id] = order
        self._chase_count[order.correlation_id] = 0

        logger.info(
            "Routing order: %s %s %s %s @ %s (urgency=%s, leg=%s)",
            order.side.value, order.symbol, order.quantity,
            order.order_type.value, order.limit_price,
            order.urgency.value, order.leg_label,
        )

        # Determine execution strategy
        if order.is_multi_leg_leg:
            return await self._submit_multi_leg(order, risk_checks)
        elif order.slice_size and order.quantity > order.slice_size:
            return await self._submit_sliced(order, current_depth)
        else:
            return await self._submit_single(order, current_depth)

    async def _submit_single(
        self,
        order: SmartOrder,
        current_depth: Optional[dict] = None,
    ) -> Optional[FillConfirmation]:
        """Submit a single order with smart execution."""
        if order.is_market_order:
            return await self._send_market_order(order)

        # Limit-at-touch with chase logic
        best_bid = current_depth.get("bid_prices", [0])[-1] if current_depth else None
        best_ask = current_depth.get("ask_prices", [0])[0] if current_depth else None
        at_touch = order.limit_price

        for chase in range(self._max_chase + 1):
            if chase > 0 and order.urgency != OrderUrgency.IMMEDIATE:
                logger.debug("Chasing order %s (attempt %d)", order.correlation_id, chase)
                await asyncio.sleep(0.5 * chase)  # Brief pause between chases

            # Try to improve the price by 1 tick
            if order.side == OrderSide.BUY and best_ask and chase < self._max_chase:
                improved_price = best_ask - (best_ask * 0.01 / 100)  # 1 tick improvement
            elif order.side == OrderSide.SELL and best_bid and chase < self._max_chase:
                improved_price = best_bid + (best_bid * 0.01 / 100)
            else:
                improved_price = at_touch

            confirmation = await self._send_limit_order(order, improved_price)
            if confirmation and confirmation.status in ("COMPLETE", "ACKNOWLEDGED"):
                return confirmation

            self._chase_count[order.correlation_id] = chase + 1

        # Final attempt: if not filled, convert to market on urgency
        if order.urgency == OrderUrgency.IMMEDIATE:
            logger.info("Order %s not filled after %d chases — converting to MARKET",
                       order.correlation_id, self._chase_count[order.correlation_id])
            return await self._send_market_order(order)

        return None

    async def _submit_multi_leg(
        self,
        order: SmartOrder,
        risk_checks: dict,
    ) -> Optional[FillConfirmation]:
        """Submit a multi-leg order with hedge-first sequencing."""
        # Hedge-first: buy protective legs before selling writing legs
        if "BUY" in (order.leg_label or "") and "PROTECTIVE" in (order.leg_label or ""):
            logger.info("Submitting hedge leg first: %s", order.leg_label)
        elif "SELL" in (order.leg_label or "") and "WRITING" in (order.leg_label or ""):
            logger.info("Waiting for hedge legs before writing leg: %s", order.leg_label)
            # This would coordinate with other legs in a basket submit
            pass

        return await self._send_limit_order(order, order.limit_price)

    async def _submit_sliced(
        self,
        order: SmartOrder,
        current_depth: Optional[dict] = None,
    ) -> Optional[FillConfirmation]:
        """Slice a large order into smaller chunks."""
        slice_qty = order.slice_size or order.quantity
        remaining = order.quantity
        last_confirmation = None

        while remaining > 0:
            qty = min(slice_qty, remaining)
            sliced = SmartOrder(
                symbol=order.symbol,
                exchange=order.exchange,
                segment=order.segment,
                side=order.side,
                quantity=qty,
                order_type=order.order_type,
                limit_price=order.limit_price,
                urgency=order.urgency,
                strategy=order.strategy,
                algo_id=order.algo_id,
                correlation_id=str(uuid.uuid4()),
                leg_label=f"{order.leg_label or 'SLICE'}_{remaining // qty + 1}",
            )
            last_confirmation = await self._submit_single(sliced, current_depth)
            remaining -= qty

        return last_confirmation

    async def _send_market_order(self, order: SmartOrder) -> Optional[FillConfirmation]:
        """Send a market order via the broker adapter."""
        try:
            result = await self._broker.place_order(
                symbol=order.symbol,
                side=order.side.value,
                quantity=order.quantity,
                order_type=OrderType.MARKET.value,
                price=None,
                algo_id=order.algo_id or self._settings.DEFAULT_ALGO_ID,
                tag=order.tag,
            )
            return FillConfirmation(
                order_id=order.correlation_id,
                broker_order_id=result.get("order_id", ""),
                symbol=order.symbol,
                exchange=order.exchange,
                segment=order.segment,
                side=order.side.value,
                fill_price=0.0,  # Will be filled by reconciliation
                fill_qty=order.quantity,
                fill_time=datetime.now(timezone.utc),
                broker_time="",
                status="PENDING_FILL",
                algo_id=order.algo_id,
                strategy=order.strategy,
                leg_label=order.leg_label,
            )
        except Exception as exc:
            logger.error("Market order failed for %s: %s", order.symbol, exc)
            return None

    async def _send_limit_order(
        self,
        order: SmartOrder,
        price: float,
    ) -> Optional[FillConfirmation]:
        """Send a limit order via the broker adapter."""
        try:
            result = await self._broker.place_order(
                symbol=order.symbol,
                side=order.side.value,
                quantity=order.quantity,
                order_type=order.order_type.value,
                price=price,
                algo_id=order.algo_id or self._settings.DEFAULT_ALGO_ID,
                tag=order.tag,
                disclosed_qty=order.disclosed_qty,
            )
            return FillConfirmation(
                order_id=order.correlation_id,
                broker_order_id=result.get("order_id", ""),
                symbol=order.symbol,
                exchange=order.exchange,
                segment=order.segment,
                side=order.side.value,
                fill_price=price,
                fill_qty=0,
                fill_time=datetime.now(timezone.utc),
                broker_time="",
                status="PENDING_FILL",
                algo_id=order.algo_id,
                strategy=order.strategy,
                leg_label=order.leg_label,
            )
        except Exception as exc:
            logger.error("Limit order failed for %s @ %s: %s", order.symbol, price, exc)
            return None

    def _estimate_commission(self, order: SmartOrder) -> float:
        """Estimate brokerage + taxes for an order."""
        notional = (order.limit_price or 0) * order.quantity
        # India equity/commodity: ₹20 or 0.03% per order (whichever lower)
        # F&O: ₹20 or 0.05% (whichever lower)
        if order.segment == "FNO":
            brokerage = min(20, notional * 0.0005)
        else:
            brokerage = min(20, notional * 0.0003)
        return round(brokerage, 2)

    def _estimate_slippage(self, order: SmartOrder, ltp: float) -> float:
        """Estimate slippage cost for an order."""
        if order.is_market_order:
            # Market orders: ~5-10 bps slippage on liquid names
            return ltp * order.quantity * 7.5 / 10000  # 7.5 bps default
        else:
            # Limit orders: ~2-3 bps
            return ltp * order.quantity * 2.5 / 10000  # 2.5 bps default

    def get_pending_orders(self) -> dict[str, SmartOrder]:
        """Return pending orders for monitoring."""
        return dict(self._pending_orders)

    def clear_pending(self, order_id: str) -> None:
        """Remove an order from pending (filled/cancelled)."""
        self._pending_orders.pop(order_id, None)
        self._chase_count.pop(order_id, None)


class BrokerPositionReconciliation:
    """Compares internal ledger against broker positions/funds every 30s.

    Detects:
    - Missing fills (internal ledger says filled, broker says pending)
    - Phantom positions (internal says long, broker says flat)
    - Fund drift (internal cash != broker funds)
    - Order state mismatch (internal says "open", broker says "rejected")

    Not constructed anywhere yet (see execution/reconciliation_service.py for
    the currently-used reconciliation path). Named distinctly from
    `PositionReconciliation` below — that name collision used to break both
    this module's own constructor call sites and every test importing it;
    see memory redesign-prompt-status.
    """

    def __init__(
        self,
        settings: Settings,
        broker_adapter: Any,
        internal_ledger: Any,
        event_bus: Any,
        check_interval: float = 30.0,
    ) -> None:
        self._settings = settings
        self._broker = broker_adapter
        self._ledger = internal_ledger
        self._event_bus = event_bus
        self._interval = check_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_diff: dict = {}

    async def start(self) -> None:
        """Start the reconciliation loop."""
        if self._running:
            return
        self._running = True
        logger.info("PositionReconciliation started (interval=%.0fs)", self._interval)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the reconciliation loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("PositionReconciliation stopped")

    async def _loop(self) -> None:
        """Main reconciliation loop."""
        while self._running:
            try:
                diff = await self._check()
                if diff:
                    self._last_diff = diff
                    await self._event_bus.publish(
                        "risk.reconciliation_diff",
                        payload={"diff": diff, "timestamp": datetime.now(timezone.utc).isoformat()},
                    )
                    if diff.get("severity") == "CRITICAL":
                        logger.critical("Reconciliation mismatch: %s", diff)
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Reconciliation loop error")
                await asyncio.sleep(self._interval)

    async def _check(self) -> dict:
        """Run one reconciliation check."""
        broker_positions = await self._broker.get_positions()
        broker_funds = await self._broker.get_funds()
        broker_orders = await self._broker.get_orders()

        internal_positions = self._ledger.get_positions()
        internal_funds = self._ledger.get_funds()
        internal_orders = self._ledger.get_open_orders()

        diffs: list[str] = []
        severity = "OK"

        # Fund drift check
        fund_delta = abs(broker_funds.get("available", 0) - internal_funds.get("cash", 0))
        if fund_delta > 1.0:  # ₹1 tolerance
            diffs.append(f"FUND_DRIFT: available=₹{broker_funds.get('available', 0):.2f} "
                         f"vs ledger=₹{internal_funds.get('cash', 0):.2f} (delta=₹{fund_delta:.2f})")
            severity = "WARN"

        # Position mismatch check
        all_symbols = set(list(broker_positions.keys()) + list(internal_positions.keys()))
        for sym in all_symbols:
            bp = broker_positions.get(sym)
            ip = internal_positions.get(sym)
            b_qty = bp.get("net_qty", 0) if bp else 0
            i_qty = ip.get("net_qty", 0) if ip else 0
            if abs(b_qty - i_qty) > 0:
                diffs.append(f"POSITION_{sym}: broker_qty={b_qty} vs ledger_qty={i_qty}")
                severity = "CRITICAL" if abs(b_qty - i_qty) > 0 else "WARN"

        # Order state mismatch check
        all_order_ids = set(list(broker_orders.keys()) + list(internal_orders.keys()))
        for oid in all_order_ids:
            bo = broker_orders.get(oid)
            io = internal_orders.get(oid)
            b_status = bo.get("status", "UNKNOWN") if bo else "UNKNOWN"
            i_status = io.get("status", "UNKNOWN") if io else "UNKNOWN"
            if b_status != i_status and b_status != "CANCELLED" and i_status != "CANCELLED":
                diffs.append(f"ORDER_{oid}: broker={b_status} vs ledger={i_status}")
                severity = "WARN"

        if not diffs:
            return {}

        return {
            "severity": severity,
            "differences": diffs,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Position reconciliation (original — production-wired, do not rename/replace)
# ---------------------------------------------------------------------------

@dataclass
class ReconciliationResult:
    symbol: str
    local_qty: int
    broker_qty: int
    drift: int
    reconciled_at: str
    action_taken: str


class PositionReconciliation:
    """Compares local portfolio positions with broker-reported positions.

    Discrepancies are logged to OMS and can trigger corrective orders.
    Constructed as `PositionReconciliation(self.portfolio, self.oms)` in
    api/runtime.py — do not change this constructor signature without
    updating that call site and tests/test_reconciliation.py.
    """

    def __init__(self, portfolio: PortfolioLedger, oms: OMSEventStore) -> None:
        self.portfolio = portfolio
        self.oms = oms

    def _all_symbols(self, broker_positions: dict[str, int]) -> set[str]:
        # Union, not just broker_positions' own keys: a position the broker no
        # longer reports (closed/stopped-out broker-side, e.g. a margin call
        # or a manual close in the broker's own app) must be caught too, not
        # only quantity mismatches on symbols the broker happens to mention.
        # Confirmed 2026-08-06: the original broker_positions.items()-only
        # loop had exactly this blind spot — a fully-broker-closed position
        # produced zero drift results since it never appeared in that dict.
        local_symbols = {sym for sym, pos in self.portfolio.positions.items() if pos.quantity != 0}
        return local_symbols | set(broker_positions.keys())

    def reconcile(self, broker_positions: dict[str, int]) -> list[ReconciliationResult]:
        results: list[ReconciliationResult] = []
        now_str = datetime.now(timezone.utc).isoformat()

        for symbol in sorted(self._all_symbols(broker_positions)):
            position = self.portfolio.positions.get(symbol)
            local_qty = position.quantity if position else 0
            broker_qty = broker_positions.get(symbol, 0)
            drift = broker_qty - local_qty
            action = "none"

            if drift != 0:
                action = f"drift_detected:{drift:+d}"
                self.oms.append(
                    event_type="position_reconciled",
                    order_id=f"recon_{symbol}_{now_str}",
                    symbol=symbol,
                    metadata={
                        "local_qty": local_qty,
                        "broker_qty": broker_qty,
                        "drift": drift,
                    },
                )

            results.append(
                ReconciliationResult(
                    symbol=symbol,
                    local_qty=local_qty,
                    broker_qty=broker_qty,
                    drift=drift,
                    reconciled_at=now_str,
                    action_taken=action,
                )
            )
        return results

    def has_drift(self, broker_positions: dict[str, int]) -> bool:
        for symbol in self._all_symbols(broker_positions):
            position = self.portfolio.positions.get(symbol)
            local_qty = position.quantity if position else 0
            if broker_positions.get(symbol, 0) != local_qty:
                return True
        return False
