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
