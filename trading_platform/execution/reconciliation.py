from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trading_platform.execution.oms_store import OMSEventStore
from trading_platform.portfolio.ledger import PortfolioLedger


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
    """

    def __init__(self, portfolio: PortfolioLedger, oms: OMSEventStore) -> None:
        self.portfolio = portfolio
        self.oms = oms

    def reconcile(self, broker_positions: dict[str, int]) -> list[ReconciliationResult]:
        results: list[ReconciliationResult] = []
        now_str = datetime.now(timezone.utc).isoformat()

        for symbol, broker_qty in broker_positions.items():
            position = self.portfolio.positions.get(symbol)
            local_qty = position.quantity if position else 0
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
        for symbol, broker_qty in broker_positions.items():
            position = self.portfolio.positions.get(symbol)
            local_qty = position.quantity if position else 0
            if broker_qty != local_qty:
                return True
        return False
