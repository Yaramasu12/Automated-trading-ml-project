"""Unit tests for OMSEventStore's algo_id/signal_hash columns — SEBI
retail-algo compliance groundwork (REDESIGN_PROMPT.md §6.2)."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from trading_platform.execution.oms_store import OMSEventStore


class AlgoIdInjectionTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "oms.db"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_algo_id_none_by_default(self):
        store = OMSEventStore(db_path=self.db_path)
        store.append(event_type="intent_queued", order_id="o1")
        row = store.events_for_order("o1")[0]
        self.assertIsNone(row["algo_id"])
        store.close()

    def test_configured_algo_id_stamped_on_every_event(self):
        store = OMSEventStore(db_path=self.db_path, algo_id="ALGO123")
        store.append(event_type="intent_queued", order_id="o1")
        store.append(event_type="broker_submitted", order_id="o1")
        rows = store.events_for_order("o1")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["algo_id"] == "ALGO123" for r in rows))
        store.close()

    def test_per_call_algo_id_overrides_platform_default(self):
        store = OMSEventStore(db_path=self.db_path, algo_id="ALGO123")
        store.append(event_type="intent_queued", order_id="o1", algo_id="OVERRIDE")
        row = store.events_for_order("o1")[0]
        self.assertEqual(row["algo_id"], "OVERRIDE")
        store.close()

    def test_empty_string_algo_id_treated_as_unconfigured(self):
        store = OMSEventStore(db_path=self.db_path, algo_id="")
        store.append(event_type="intent_queued", order_id="o1")
        row = store.events_for_order("o1")[0]
        self.assertIsNone(row["algo_id"])
        store.close()


class SignalHashStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "oms.db"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_signal_hash_stored_and_retrievable(self):
        store = OMSEventStore(db_path=self.db_path)
        store.append(event_type="intent_queued", order_id="o1", signal_hash="abc123def456")
        row = store.events_for_order("o1")[0]
        self.assertEqual(row["signal_hash"], "abc123def456")
        store.close()

    def test_signal_hash_none_by_default(self):
        store = OMSEventStore(db_path=self.db_path)
        store.append(event_type="intent_queued", order_id="o1")
        row = store.events_for_order("o1")[0]
        self.assertIsNone(row["signal_hash"])
        store.close()


class SchemaMigrationTests(unittest.TestCase):
    """A pre-2026-08-06 database has no algo_id/signal_hash columns at all —
    OMSEventStore must upgrade it in place rather than fail or silently
    lose the ability to write those fields."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "old_oms.db"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _create_pre_migration_schema(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE oms_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id        TEXT NOT NULL,
                occurred_at     TEXT NOT NULL,
                event_type      TEXT NOT NULL,
                order_id        TEXT NOT NULL,
                idempotency_key TEXT,
                symbol          TEXT,
                strategy_name   TEXT,
                side            TEXT,
                quantity        INTEGER,
                price           REAL,
                priority        INTEGER,
                broker_order_id TEXT,
                fill_price      REAL,
                fill_qty        INTEGER,
                rejection_reason TEXT,
                metadata        TEXT
            )
        """)
        conn.execute(
            "INSERT INTO oms_events (event_id, occurred_at, event_type, order_id) "
            "VALUES ('evt_old_1', '2026-01-01T00:00:00', 'intent_queued', 'old_order')"
        )
        conn.commit()
        conn.close()

    def test_existing_database_is_upgraded_in_place(self):
        self._create_pre_migration_schema()

        store = OMSEventStore(db_path=self.db_path, algo_id="ALGO123")

        # The pre-existing row must survive the migration untouched.
        old_rows = store.events_for_order("old_order")
        self.assertEqual(len(old_rows), 1)
        self.assertIsNone(old_rows[0]["algo_id"])  # migrated column, no backfill for old rows

        # New writes after migration use the new columns normally.
        store.append(event_type="intent_queued", order_id="new_order", signal_hash="hash1")
        new_rows = store.events_for_order("new_order")
        self.assertEqual(new_rows[0]["algo_id"], "ALGO123")
        self.assertEqual(new_rows[0]["signal_hash"], "hash1")
        store.close()

    def test_migration_is_idempotent_across_reopens(self):
        self._create_pre_migration_schema()
        store1 = OMSEventStore(db_path=self.db_path)
        store1.close()
        # Reopening an already-migrated database must not raise (duplicate
        # ALTER TABLE would error if the PRAGMA check weren't guarding it).
        store2 = OMSEventStore(db_path=self.db_path)
        store2.append(event_type="intent_queued", order_id="o2")
        self.assertEqual(len(store2.events_for_order("o2")), 1)
        store2.close()


if __name__ == "__main__":
    unittest.main()
