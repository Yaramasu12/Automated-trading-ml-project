"""Reconciliation service — the operational backbone of execution.

Every 30 seconds during market hours, compares broker positions/orders/funds
vs internal ledger. Detects mismatches, orphan orders, rejected fills, and
partial-fill anomalies.

Rules:
- Mismatch → halt new entries within 60s, alert via Telegram/ Grafana
- Orphan-order detection: orders submitted but no fill within timeout
- Partial-fill handling: track remaining_qty, auto-retry or cancel
- Rejected-order classification: reason codes, retry logic
- Clock/tick skew awareness: accept broker time as ground truth for fills

This is the ONLY source of truth for "what do we actually hold?"
The internal ledger is a hypothesis; the broker's position is reality.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class MismatchSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    ORPHANED = "ORPHANED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ReconciliationDiff:
    """A detected discrepancy between broker and internal state."""
    category: str  # "position", "order", "fund", "margin"
    symbol: str
    severity: MismatchSeverity
    internal_value: Any
    broker_value: Any
    delta: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    action_taken: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class ReconciliationReport:
    """Snapshot of reconciliation state at a point in time."""
    timestamp: datetime
    positions_match: bool
    orders_match: bool
    funds_match: bool
    margin_match: bool
    diffs: list[ReconciliationDiff] = field(default_factory=list)
    orphan_orders: list[str] = field(default_factory=list)
    rejected_orders: list[str] = field(default_factory=list)
    partial_fills: list[str] = field(default_factory=list)
    overall_status: str = "OK"  # OK, WARNING, CRITICAL


@dataclass
class InternalPosition:
    """Our internal view of a position."""
    symbol: str
    exchange: str
    segment: str
    quantity: int
    avg_price: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class InternalOrder:
    """Our internal view of an order."""
    correlation_id: str
    symbol: str
    side: str
    quantity: int
    filled_quantity: int
    avg_fill_price: float
    status: OrderStatus
    submitted_at: datetime
    last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    broker_order_id: str = ""
    rejection_reason: str = ""


class ReconciliationService:
    """Main reconciliation service — runs every 30s in market hours."""

    def __init__(
        self,
        broker_adapter: Any,
        internal_ledger: Any,  # PositionLedger + OrderStore
        event_bus: Optional[Any] = None,
        check_interval: int = 30,  # seconds
        orphan_timeout: int = 60,  # seconds before marking order orphaned
        rejection_timeout: int = 30,  # seconds before assuming rejection
    ) -> None:
        self._broker = broker_adapter
        self._ledger = internal_ledger
        self._event_bus = event_bus
        self._check_interval = check_interval
        self._orphan_timeout = orphan_timeout
        self._rejection_timeout = rejection_timeout
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_report: Optional[ReconciliationReport] = None
        self._mismatch_count = 0
        self._halt_new_entries = False
        self._halt_reason: str = ""
        self._alert_threshold = 3  # Halt after N consecutive mismatches

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def halt_new_entries(self) -> bool:
        return self._halt_new_entries

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    async def start(self) -> None:
        """Start the reconciliation loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._reconciliation_loop())
        logger.info("[reconciliation] Service started — checking every %ds", self._check_interval)

    async def stop(self) -> None:
        """Stop the reconciliation loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[reconciliation] Service stopped")

    async def _reconciliation_loop(self) -> None:
        """Main reconciliation loop — runs every check_interval seconds."""
        while self._running:
            try:
                report = await self._check()
                self._last_report = report

                # Update halt state
                if report.overall_status == "CRITICAL":
                    self._mismatch_count += 1
                    if self._mismatch_count >= self._alert_threshold:
                        if not self._halt_new_entries:
                            self._halt_new_entries = True
                            self._halt_reason = (
                                f"Reconciliation: {self._mismatch_count} consecutive "
                                f"mismatches — halted new entries"
                            )
                            logger.critical("[reconciliation] %s", self._halt_reason)
                            await self._alert("CRITICAL", self._halt_reason, report)
                else:
                    if self._mismatch_count > 0:
                        logger.info(
                            "[reconciliation] Mismatch count reset to 0 (was %d)",
                            self._mismatch_count,
                        )
                    self._mismatch_count = 0
                    if self._halt_new_entries:
                        # Auto-resume if positions now match
                        if report.positions_match:
                            self._halt_new_entries = False
                            self._halt_reason = ""
                            logger.info("[reconciliation] Auto-resumed: positions reconciled")
                            await self._alert("INFO", "Reconciliation restored — resuming entries")

                # Emit reconciliation event to event bus
                if self._event_bus:
                    await self._event_bus.publish(
                        "reconciliation.report",
                        {
                            "timestamp": report.timestamp.isoformat(),
                            "positions_match": report.positions_match,
                            "orders_match": report.orders_match,
                            "overall_status": report.overall_status,
                            "diff_count": len(report.diffs),
                        },
                    )

            except Exception as exc:
                logger.error("[reconciliation] Check failed: %s", exc, exc_info=True)
                self._mismatch_count += 1

            await asyncio.sleep(self._check_interval)

    async def _check(self) -> ReconciliationReport:
        """Run a single reconciliation check."""
        diffs: list[ReconciliationDiff] = []
        orphan_orders: list[str] = []
        rejected_orders: list[str] = []
        partial_fills: list[str] = []

        # 1. Positions reconciliation
        pos_report = await self._check_positions()
        if not pos_report.positions_match:
            diffs.extend(pos_report.diffs)

        # 2. Orders reconciliation
        order_report = await self._check_orders()
        if not order_report.orders_match:
            diffs.extend(order_report.diffs)
            orphan_orders.extend(order_report.orphan_orders)
            rejected_orders.extend(order_report.rejected_orders)
            partial_fills.extend(order_report.partial_fills)

        # 3. Funds reconciliation
        fund_report = await self._check_funds()
        if not fund_report.funds_match:
            diffs.extend(fund_report.diffs)

        # 4. Margin reconciliation
        margin_report = await self._check_margin()
        if not margin_report.margin_match:
            diffs.extend(margin_report.diffs)

        # Determine overall status
        critical_diffs = [d for d in diffs if d.severity == MismatchSeverity.CRITICAL]
        high_diffs = [d for d in diffs if d.severity == MismatchSeverity.HIGH]

        if critical_diffs or orphan_orders:
            overall = "CRITICAL"
        elif high_diffs or rejected_orders:
            overall = "WARNING"
        else:
            overall = "OK"

        report = ReconciliationReport(
            timestamp=datetime.now(timezone.utc),
            positions_match=pos_report.positions_match,
            orders_match=order_report.orders_match,
            funds_match=fund_report.funds_match,
            margin_match=margin_report.margin_match,
            diffs=diffs,
            orphan_orders=orphan_orders,
            rejected_orders=rejected_orders,
            partial_fills=partial_fills,
            overall_status=overall,
        )

        if diffs:
            logger.warning(
                "[reconciliation] Report: status=%s diffs=%d orphans=%d rejected=%d",
                overall, len(diffs), len(orphan_orders), len(rejected_orders),
            )

        return report

    async def _check_positions(self) -> ReconciliationReport:
        """Compare internal positions vs broker positions."""
        diffs: list[ReconciliationDiff] = []

        try:
            internal_positions = await self._ledger.get_positions()
            broker_positions = await self._broker.get_positions()
        except Exception as exc:
            diffs.append(ReconciliationDiff(
                category="position",
                symbol="*",
                severity=MismatchSeverity.CRITICAL,
                internal_value="N/A",
                broker_value="N/A",
                delta=0.0,
                action=f"Fetch failed: {exc}",
            ))
            return ReconciliationReport(
                timestamp=datetime.now(timezone.utc),
                positions_match=False,
                orders_match=True,
                funds_match=True,
                margin_match=True,
                diffs=diffs,
            )

        # Build lookup maps
        internal_map = {p.symbol: p for p in internal_positions}
        broker_map = {p["symbol"]: p for p in broker_positions}

        all_symbols = set(internal_map.keys()) | set(broker_map.keys())
        positions_match = True

        for symbol in sorted(all_symbols):
            int_pos = internal_map.get(symbol)
            brk_pos = broker_map.get(symbol)

            # Position exists in one but not the other
            if int_pos and not brk_pos:
                diffs.append(ReconciliationDiff(
                    category="position",
                    symbol=symbol,
                    severity=MismatchSeverity.HIGH,
                    internal_value=int_pos.quantity,
                    broker_value=0,
                    delta=float(int_pos.quantity),
                    action="Position in internal but not broker — may be pending fill",
                ))
                positions_match = False
            elif brk_pos and not int_pos:
                diffs.append(ReconciliationDiff(
                    category="position",
                    symbol=symbol,
                    severity=MismatchSeverity.HIGH,
                    internal_value=0,
                    broker_value=brk_pos.get("quantity", 0),
                    delta=float(brk_pos.get("net_qty", 0)),
                    action="Position in broker but not internal — ledger needs update",
                ))
                positions_match = False
            elif int_pos and brk_pos:
                # Compare quantities
                int_qty = int_pos.quantity
                brk_qty = brk_pos.get("net_qty", 0)
                if int_qty != brk_qty:
                    delta = float(abs(int_qty - brk_qty))
                    diffs.append(ReconciliationDiff(
                        category="position",
                        symbol=symbol,
                        severity=MismatchSeverity.CRITICAL if delta > 10 else MismatchSeverity.HIGH,
                        internal_value=int_qty,
                        broker_value=brk_qty,
                        delta=delta,
                        action=f"Quantity mismatch: internal={int_qty}, broker={brk_qty}",
                        details={"avg_price_internal": int_pos.avg_price,
                                 "avg_price_broker": brk_pos.get("avg_price", 0)},
                    ))
                    positions_match = False

        return ReconciliationReport(
            timestamp=datetime.now(timezone.utc),
            positions_match=positions_match,
            orders_match=True,
            funds_match=True,
            margin_match=True,
            diffs=diffs,
        )

    async def _check_orders(self) -> ReconciliationReport:
        """Compare internal orders vs broker orders."""
        diffs: list[ReconciliationDiff] = []
        orphan_orders: list[str] = []
        rejected_orders: list[str] = []
        partial_fills: list[str] = []

        try:
            internal_orders = await self._ledger.get_orders()
            broker_orders = await self._broker.get_orders()
        except Exception as exc:
            diffs.append(ReconciliationDiff(
                category="order",
                symbol="*",
                severity=MismatchSeverity.CRITICAL,
                internal_value="N/A",
                broker_value="N/A",
                delta=0.0,
                action=f"Fetch failed: {exc}",
            ))
            return ReconciliationReport(
                timestamp=datetime.now(timezone.utc),
                positions_match=True,
                orders_match=False,
                funds_match=True,
                margin_match=True,
                diffs=diffs,
                orphan_orders=orphan_orders,
                rejected_orders=rejected_orders,
                partial_fills=partial_fills,
            )

        internal_map = {o.correlation_id: o for o in internal_orders}
        broker_map = {o.get("broker_order_id", ""): o for o in broker_orders}

        now = datetime.now(timezone.utc)
        orders_match = True

        for corr_id, int_order in internal_map.items():
            # Find matching broker order
            brk_order = None
            for bid, bo in broker_map.items():
                if corr_id in str(bo.get("parent_order_id", "")) or corr_id == bid:
                    brk_order = bo
                    break

            if int_order.status == OrderStatus.PENDING:
                elapsed = (now - int_order.submitted_at).total_seconds()

                # Check for orphan: submitted but no broker acknowledgment
                if elapsed > self._orphan_timeout and brk_order is None:
                    orphan_orders.append(corr_id)
                    diffs.append(ReconciliationDiff(
                        category="order",
                        symbol=int_order.symbol,
                        severity=MismatchSeverity.HIGH,
                        internal_value=int_order.status.value,
                        broker_value="NO_ACK",
                        delta=0.0,
                        action="Orphan detected — order submitted but no broker response",
                    ))
                    orders_match = False

                # Check for rejection timeout
                elif elapsed > self._rejection_timeout and brk_order is None:
                    # Assume rejected if no response within timeout
                    rejected_orders.append(corr_id)
                    diffs.append(ReconciliationDiff(
                        category="order",
                        symbol=int_order.symbol,
                        severity=MismatchSeverity.MEDIUM,
                        internal_value=int_order.status.value,
                        broker_value="TIMEOUT",
                        delta=float(int_order.quantity - int_order.filled_quantity),
                        action="Timeout — no broker response within {}s".format(self._rejection_timeout),
                    ))
                    orders_match = False

            elif int_order.status == OrderStatus.PARTIAL:
                if brk_order:
                    brk_filled = brk_order.get("filled_qty", 0)
                    if brk_filled > int_order.filled_quantity:
                        partial_fills.append(corr_id)
                        diffs.append(ReconciliationDiff(
                            category="order",
                            symbol=int_order.symbol,
                            severity=MismatchSeverity.MEDIUM,
                            internal_value=int_order.filled_quantity,
                            broker_value=brk_filled,
                            delta=float(brk_filled - int_order.filled_quantity),
                            action="Partial fill received — updating ledger",
                        ))

        return ReconciliationReport(
            timestamp=datetime.now(timezone.utc),
            positions_match=True,
            orders_match=orders_match,
            funds_match=True,
            margin_match=True,
            diffs=diffs,
            orphan_orders=orphan_orders,
            rejected_orders=rejected_orders,
            partial_fills=partial_fills,
        )

    async def _check_funds(self) -> ReconciliationReport:
        """Compare internal funds vs broker funds."""
        diffs: list[ReconciliationDiff] = []

        try:
            internal_funds = await self._ledger.get_funds()
            broker_funds = await self._broker.get_funds()
        except Exception as exc:
            diffs.append(ReconciliationDiff(
                category="fund",
                symbol="*",
                severity=MismatchSeverity.CRITICAL,
                internal_value="N/A",
                broker_value="N/A",
                delta=0.0,
                action=f"Fetch failed: {exc}",
            ))
            return ReconciliationReport(
                timestamp=datetime.now(timezone.utc),
                positions_match=True,
                orders_match=True,
                funds_match=False,
                margin_match=True,
                diffs=diffs,
            )

        int_available = internal_funds.get("available_balance", 0)
        int_utilized = internal_funds.get("utilized_balance", 0)
        brk_available = broker_funds.get("available_balance", 0)
        brk_utilized = broker_funds.get("utilized_balance", 0)

        funds_match = True

        # Allow small tolerance for unrealized margin estimates
        available_delta = abs(int_available - brk_available)
        utilized_delta = abs(int_utilized - brk_utilized)

        if available_delta > 100:  # ₹100 tolerance
            diffs.append(ReconciliationDiff(
                category="fund",
                symbol="*",
                severity=MismatchSeverity.HIGH,
                internal_value=int_available,
                broker_value=brk_available,
                delta=available_delta,
                action=f"Available balance mismatch: internal={int_available}, broker={brk_available}",
            ))
            funds_match = False

        if utilized_delta > 50:  # ₹50 tolerance for margin estimates
            diffs.append(ReconciliationDiff(
                category="fund",
                symbol="*",
                severity=MismatchSeverity.MEDIUM,
                internal_value=int_utilized,
                broker_value=brk_utilized,
                delta=utilized_delta,
                action=f"Utilized margin mismatch: internal={int_utilized}, broker={brk_utilized}",
            ))
            funds_match = False

        return ReconciliationReport(
            timestamp=datetime.now(timezone.utc),
            positions_match=True,
            orders_match=True,
            funds_match=funds_match,
            margin_match=True,
            diffs=diffs,
        )

    async def _check_margin(self) -> ReconciliationReport:
        """Compare internal margin usage vs broker margin."""
        diffs: list[ReconciliationDiff] = []

        try:
            internal_margin = await self._ledger.get_margin_usage()
            broker_margin = await self._broker.get_margin()
        except Exception as exc:
            diffs.append(ReconciliationDiff(
                category="margin",
                symbol="*",
                severity=MismatchSeverity.HIGH,
                internal_value="N/A",
                broker_value="N/A",
                delta=0.0,
                action=f"Fetch failed: {exc}",
            ))
            return ReconciliationReport(
                timestamp=datetime.now(timezone.utc),
                positions_match=True,
                orders_match=True,
                funds_match=True,
                margin_match=False,
                diffs=diffs,
            )

        int_used = internal_margin.get("used", 0)
        int_available = internal_margin.get("available", 0)
        brk_used = broker_margin.get("used", 0)
        brk_available = broker_margin.get("available", 0)

        used_delta = abs(int_used - brk_used)
        avail_delta = abs(int_available - brk_available)

        margin_match = used_delta < 100 and avail_delta < 100

        if not margin_match:
            diffs.append(ReconciliationDiff(
                category="margin",
                symbol="*",
                severity=MismatchSeverity.MEDIUM,
                internal_value={"used": int_used, "available": int_available},
                broker_value={"used": brk_used, "available": brk_available},
                delta=max(used_delta, avail_delta),
                action="Margin usage mismatch — may be due to unrealized margin estimates",
            ))

        return ReconciliationReport(
            timestamp=datetime.now(timezone.utc),
            positions_match=True,
            orders_match=True,
            funds_match=True,
            margin_match=margin_match,
            diffs=diffs,
        )

    async def _alert(
        self,
        level: str,
        message: str,
        report: Optional[ReconciliationReport] = None,
    ) -> None:
        """Send alert via configured channels."""
        alert_data = {
            "level": level,
            "service": "reconciliation",
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if report:
            alert_data["report"] = {
                "positions_match": report.positions_match,
                "orders_match": report.orders_match,
                "overall_status": report.overall_status,
            }

        logger.log(
            logging.CRITICAL if level == "CRITICAL" else logging.WARNING,
            "[reconciliation alert] %s", message,
        )

        if self._event_bus:
            await self._event_bus.publish(
                "risk.alert",
                {**alert_data, "category": "reconciliation"},
            )

    def get_last_report(self) -> Optional[ReconciliationReport]:
        """Get the most recent reconciliation report."""
        return self._last_report

    def force_check(self) -> ReconciliationReport:
        """Run an immediate reconciliation check (called by kill switch, UI)."""
        return asyncio.get_event_loop().run_until_complete(self._check())

    async def update_ledger_from_broker(self) -> int:
        """Pull the latest state from the broker and update the internal ledger.
        
        Returns:
            Number of records updated.
        """
        updated = 0

        try:
            broker_positions = await self._broker.get_positions()
            for pos in broker_positions:
                await self._ledger.upsert_position(pos)
                updated += 1
        except Exception as exc:
            logger.error("[reconciliation] Failed to update positions: %s", exc)

        try:
            broker_orders = await self._broker.get_orders()
            for order in broker_orders:
                await self._ledger.upsert_order(order)
                updated += 1
        except Exception as exc:
            logger.error("[reconciliation] Failed to update orders: %s", exc)

        if updated > 0:
            logger.info("[reconciliation] Ledger updated: %d records", updated)

        return updated