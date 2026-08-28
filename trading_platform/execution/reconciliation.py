"""Position/fund/order-state reconciliation between the internal ledger and
the broker.

Two implementations live here:
  BrokerPositionReconciliation — a periodic (default 30s) drift-detection
    loop against a generic broker_adapter/internal_ledger/event_bus. Not
    constructed anywhere yet (see execution/reconciliation_service.py for
    the currently-used reconciliation path).
  PositionReconciliation — the one actually wired into production, via
    `PositionReconciliation(self.portfolio, self.oms)` in api/runtime.py.
    Do not change its constructor signature without updating that call site
    and tests/test_reconciliation.py.

This module previously also held a limit-at-touch/chase SmartOrderRouter
implementing the same concept as execution/order_pricing.py + scheduler.py's
_chase_to_market_if_unfilled — but using its own incompatible OrderRequest/
Fill/MultiLegOrder type system, disconnected from OrderIntent/Order/
BrokerClient (nothing imported it, same as the separately-deleted
execution/smart_router.py). Removed 2026-08-22 rather than left in place,
since a second dead implementation of the thing this codebase actually does
for real is worse than none.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from trading_platform.config import Settings
from trading_platform.execution.oms_store import OMSEventStore
from trading_platform.portfolio.ledger import PortfolioLedger

logger = logging.getLogger(__name__)


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
