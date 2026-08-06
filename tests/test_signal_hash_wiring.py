"""Verifies ExecutionScheduler.enqueue() actually stamps signal_hash onto
the OMS events it writes — the wiring half of REDESIGN_PROMPT.md §6.2's
compliance groundwork (compute_signal_hash itself is unit-tested in
test_signal_hash.py; PositionReconciliation-style scope: this file only
tests the low-risk additive wiring at the order-entry point in enqueue(),
not scheduler.py's deeper broker-submission internals)."""
from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from trading_platform.broker.base import BrokerClient, BrokerResult
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.domain.enums import OrderStatus, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal, compute_signal_hash
from trading_platform.execution.fill_processor import FillProcessor
from trading_platform.execution.lock_manager import InstrumentLockManager
from trading_platform.execution.oms_store import OMSEventStore
from trading_platform.execution.scheduler import ExecutionScheduler
from trading_platform.portfolio.ledger import PortfolioLedger
from trading_platform.risk.compliance import ComplianceGuard

_MASTER = build_default_universe(date(2026, 1, 5))


def _intent(strategy_name="test_strategy") -> OrderIntent:
    signal = Signal(
        strategy_name, "RELIANCE", Side.BUY, 0.9, 2800.0, "test",
        datetime.now(timezone.utc),
    )
    return OrderIntent(signal, _MASTER.get("RELIANCE"), 1, OrderType.MARKET, ProductType.INTRADAY)


class _NoOpBroker(BrokerClient):
    name = "FAKE"

    def is_ready(self) -> bool:
        return True

    def submit_order(self, intent: OrderIntent) -> BrokerResult:
        now = datetime.now(timezone.utc)
        return BrokerResult(OrderStatus.ACKNOWLEDGED, "AO-1", None, now, now, "ack")

    def positions(self) -> list[dict]:
        return []

    def order_status(self, order_id: str) -> dict | None:
        return None


@contextlib.contextmanager
def _scheduler_in_tmpdir(compliance=None):
    with tempfile.TemporaryDirectory() as tmp:
        portfolio = PortfolioLedger(10_000_000)
        oms = OMSEventStore(db_path=Path(tmp) / "oms.db")
        sched = ExecutionScheduler(
            broker=_NoOpBroker(), oms=oms,
            fill_processor=FillProcessor(portfolio, oms),
            lock_manager=InstrumentLockManager(),
            portfolio=portfolio, compliance=compliance,
        )
        try:
            yield sched
        finally:
            sched.oms.close()


class SignalHashWiringTests(unittest.TestCase):
    def test_compliance_approved_event_carries_signal_hash(self):
        with _scheduler_in_tmpdir(compliance=ComplianceGuard()) as sched:
            intent = _intent()
            asyncio.run(sched.enqueue(intent))
            events = sched.oms.events_for_order(intent.idempotency_key)
            approved = [e for e in events if e["event_type"] == "compliance_approved"]
            self.assertEqual(len(approved), 1)
            self.assertEqual(approved[0]["signal_hash"], compute_signal_hash(intent.signal))

    def test_compliance_rejected_event_carries_signal_hash(self):
        guard = ComplianceGuard(banned_symbols={"RELIANCE"})
        with _scheduler_in_tmpdir(compliance=guard) as sched:
            intent = _intent()
            asyncio.run(sched.enqueue(intent))
            events = sched.oms.events_for_order(intent.idempotency_key)
            rejected = [e for e in events if e["event_type"] == "compliance_rejected"]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["signal_hash"], compute_signal_hash(intent.signal))

    def test_kill_switch_cancelled_event_carries_signal_hash(self):
        with _scheduler_in_tmpdir() as sched:
            sched.kill_switch_active = True
            intent = _intent()
            asyncio.run(sched.enqueue(intent))
            events = sched.oms.events_for_order(intent.idempotency_key)
            cancelled = [e for e in events if e["event_type"] == "kill_switch_cancelled"]
            self.assertEqual(len(cancelled), 1)
            self.assertEqual(cancelled[0]["signal_hash"], compute_signal_hash(intent.signal))

    def test_different_signals_get_different_hashes(self):
        with _scheduler_in_tmpdir(compliance=ComplianceGuard()) as sched:
            a = _intent(strategy_name="strategy_a")
            b = _intent(strategy_name="strategy_b")
            asyncio.run(sched.enqueue(a))
            asyncio.run(sched.enqueue(b))
            hash_a = sched.oms.events_for_order(a.idempotency_key)[0]["signal_hash"]
            hash_b = sched.oms.events_for_order(b.idempotency_key)[0]["signal_hash"]
            self.assertNotEqual(hash_a, hash_b)


if __name__ == "__main__":
    unittest.main()
