"""EXEC-1: PAPER-mode unresolved-order remediation.

The OMS event log is append-only with no 'resolved' event type, so a single
fill_unresolved / fill_price_missing row stays an order's newest event forever
and pins operational_status to DEGRADED (monitoring/metrics.py). These tests
cover the mechanism that closes that loop without fabricating a fill: the
fill_resolved event, and the age-out selection that only touches stale rows.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trading_platform.execution.oms_store import VALID_EVENT_TYPES, OMSEventStore


class FillResolvedEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OMSEventStore(db_path=Path(self._tmp.name) / "oms.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_fill_resolved_is_a_valid_event_type(self):
        self.assertIn("fill_resolved", VALID_EVENT_TYPES)

    def _unresolved_ids(self) -> set[str]:
        return {r["order_id"] for r in self.store.unresolved_orders()}

    def test_fill_resolved_clears_order_from_unresolved_list(self):
        # An order whose latest event is fill_unresolved shows up as unresolved.
        self.store.append(event_type="fill_unresolved", order_id="ord-1", symbol="NIFTY")
        self.assertIn("ord-1", self._unresolved_ids())

        # Appending fill_resolved (a later event for the same order_id) clears it,
        # because unresolved_orders() keys off each order's MOST RECENT event.
        self.store.append(
            event_type="fill_resolved",
            order_id="ord-1",
            symbol="NIFTY",
            rejection_reason="auto_aged_out",
            metadata={"resolution": "aged_out"},
        )
        self.assertNotIn("ord-1", self._unresolved_ids())

    def test_fill_price_missing_also_clearable(self):
        self.store.append(event_type="fill_price_missing", order_id="ord-2", symbol="BANKNIFTY")
        self.assertIn("ord-2", self._unresolved_ids())
        self.store.append(event_type="fill_resolved", order_id="ord-2", symbol="BANKNIFTY")
        self.assertNotIn("ord-2", self._unresolved_ids())

    def test_unrelated_orders_are_untouched(self):
        self.store.append(event_type="fill_unresolved", order_id="keep", symbol="NIFTY")
        self.store.append(event_type="fill_unresolved", order_id="clear", symbol="NIFTY")
        self.store.append(event_type="fill_resolved", order_id="clear", symbol="NIFTY")
        self.assertEqual(self._unresolved_ids(), {"keep"})


class AgeOutSelectionTests(unittest.TestCase):
    """The pure selection used by the PAPER remediation tick — only rows older
    than the grace window, never fresh or timestamp-less ones."""

    def test_selects_only_rows_older_than_grace(self):
        from trading_platform.api.runtime import TradingRuntime

        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        grace = timedelta(hours=6)
        rows = [
            {"order_id": "old", "occurred_at": (now - timedelta(hours=7)).isoformat()},
            {"order_id": "fresh", "occurred_at": (now - timedelta(hours=1)).isoformat()},
            {"order_id": "exactly_grace", "occurred_at": (now - timedelta(hours=6)).isoformat()},
            {"order_id": "bad_ts", "occurred_at": "not-a-date"},
            {"order_id": "no_ts", "occurred_at": None},
        ]
        selected = {r["order_id"] for r in TradingRuntime._select_aged_unresolved(rows, now, grace)}
        # Fresh (<grace), unparseable and missing timestamps are all left alone.
        self.assertEqual(selected, {"old", "exactly_grace"})


if __name__ == "__main__":
    unittest.main()
