"""Tests for Phase 1: trace module."""
import os
import tempfile
import unittest
from datetime import datetime, timezone

from trading_platform.trace.ids import new_trace_id
from trading_platform.trace.models import DecisionTrace, TraceEvent
from trading_platform.trace.store import TraceStore


class TestTraceIds(unittest.TestCase):
    def test_uniqueness(self):
        ids = {new_trace_id() for _ in range(1000)}
        self.assertEqual(len(ids), 1000)

    def test_prefix(self):
        tid = new_trace_id("scan")
        self.assertTrue(tid.startswith("scan-"))

    def test_default_prefix(self):
        tid = new_trace_id()
        self.assertTrue(tid.startswith("scan-"))

    def test_custom_prefix(self):
        tid = new_trace_id("test")
        self.assertTrue(tid.startswith("test-"))


class TestDecisionTrace(unittest.TestCase):
    def test_basic_creation(self):
        t = DecisionTrace(
            trace_id="scan-abc123",
            created_at=datetime.now(timezone.utc),
            execution_mode="BACKTEST",
        )
        self.assertEqual(t.trace_id, "scan-abc123")
        self.assertEqual(t.execution_mode, "BACKTEST")

    def test_add_event(self):
        t = DecisionTrace(
            trace_id="t1",
            created_at=datetime.now(timezone.utc),
            execution_mode="PAPER",
        )
        t.add_event("test_event", "TestComponent", {"key": "value"})
        self.assertEqual(len(t.events), 1)
        self.assertEqual(t.events[0].event_type, "test_event")

    def test_to_dict_no_secrets(self):
        t = DecisionTrace(
            trace_id="t2",
            created_at=datetime.now(timezone.utc),
            execution_mode="BACKTEST",
            metadata={"normal_key": "normal_value"},
        )
        d = t.to_dict()
        # Should not contain secret-looking keys in metadata
        self.assertNotIn("secret", str(d).lower().replace("no_secrets", ""))

    def test_round_trip(self):
        t = DecisionTrace(
            trace_id="t3",
            created_at=datetime.now(timezone.utc),
            execution_mode="SHADOW_LIVE",
            symbol_universe=["NIFTY", "BANKNIFTY"],
        )
        t.add_event("scan_started", "engine")
        d = t.to_dict()
        t2 = DecisionTrace.from_dict(d)
        self.assertEqual(t2.trace_id, t.trace_id)
        self.assertEqual(t2.symbol_universe, t.symbol_universe)
        self.assertEqual(len(t2.events), 1)


class TestTraceStore(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._store = TraceStore(base_dir=self._tmpdir)

    def test_save_and_get(self):
        t = DecisionTrace(
            trace_id=new_trace_id(),
            created_at=datetime.now(timezone.utc),
            execution_mode="BACKTEST",
        )
        self._store.save(t)
        retrieved = self._store.get(t.trace_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.trace_id, t.trace_id)

    def test_missing_returns_none(self):
        result = self._store.get("nonexistent-trace-id")
        self.assertIsNone(result)

    def test_no_secrets_in_store(self):
        t = DecisionTrace(
            trace_id=new_trace_id(),
            created_at=datetime.now(timezone.utc),
            execution_mode="BACKTEST",
            metadata={"api_token": "SHOULD_BE_REDACTED"},
        )
        self._store.save(t)
        # Read the file and check no raw token
        import json
        from pathlib import Path
        files = list(Path(self._tmpdir).glob("traces_*.jsonl"))
        self.assertTrue(len(files) > 0)
        content = files[0].read_text()
        self.assertNotIn("SHOULD_BE_REDACTED", content)
        self.assertIn("REDACTED", content)

    def test_count(self):
        for _ in range(5):
            t = DecisionTrace(
                trace_id=new_trace_id(),
                created_at=datetime.now(timezone.utc),
                execution_mode="BACKTEST",
            )
            self._store.save(t)
        self.assertEqual(self._store.count(), 5)

    def test_iter_recent(self):
        for _ in range(3):
            t = DecisionTrace(
                trace_id=new_trace_id(),
                created_at=datetime.now(timezone.utc),
                execution_mode="BACKTEST",
            )
            self._store.save(t)
        results = list(self._store.iter_recent(2))
        self.assertEqual(len(results), 2)

    def test_persistence_across_instances(self):
        trace_id = new_trace_id()
        t = DecisionTrace(
            trace_id=trace_id,
            created_at=datetime.now(timezone.utc),
            execution_mode="BACKTEST",
        )
        self._store.save(t)
        # New store instance, same dir
        store2 = TraceStore(base_dir=self._tmpdir)
        retrieved = store2.get(trace_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.trace_id, trace_id)

    def test_get_returns_latest_saved_trace_after_restart(self):
        trace_id = new_trace_id()
        t = DecisionTrace(
            trace_id=trace_id,
            created_at=datetime.now(timezone.utc),
            execution_mode="BACKTEST",
            metadata={"stage": "started"},
        )
        self._store.save(t)
        t.metadata["stage"] = "ensemble_done"
        t.add_event("ensemble_decision", "EnsembleDecisionEngine")
        self._store.save(t)

        store2 = TraceStore(base_dir=self._tmpdir)
        retrieved = store2.get(trace_id)

        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.metadata["stage"], "ensemble_done")
        self.assertEqual(retrieved.events[-1].event_type, "ensemble_decision")

    def test_get_finds_trace_via_sqlite_mirror_without_any_jsonl_file(self):
        # Found 2026-09-06: get() on a cache miss used to go straight to
        # scanning JSONL files, ignoring the SQLite mirror save() already
        # upserts every trace into (trace_id is that table's PRIMARY KEY, an
        # indexed lookup). With ~1500+ distinct trace_ids requested in one
        # real API call (M6 live-canary-readiness), that turned a single
        # request into several CPU-bound minutes. Deleting the JSONL file
        # entirely proves this path is the SQLite lookup, not a scan that
        # happens to still find a file.
        import glob
        import os

        db_path = os.path.join(self._tmpdir, "trace_test.db")
        store = TraceStore(base_dir=self._tmpdir, db_path=db_path)
        trace_id = new_trace_id()
        t = DecisionTrace(
            trace_id=trace_id, created_at=datetime.now(timezone.utc),
            execution_mode="BACKTEST", metadata={"stage": "done"},
        )
        store.save(t)

        for path in glob.glob(os.path.join(self._tmpdir, "traces_*.jsonl")):
            os.remove(path)

        fresh_store = TraceStore(base_dir=self._tmpdir, db_path=db_path)
        retrieved = fresh_store.get(trace_id)

        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.trace_id, trace_id)
        self.assertEqual(retrieved.metadata["stage"], "done")

    def test_backfill_sqlite_mirror_fills_a_gap(self):
        # Simulates the exact real-world gap found 2026-09-06: traces that
        # exist only in the JSONL files, not in decision_traces (an unknown
        # past event, not something this test needs to reproduce). Wiping
        # the SQLite row after a normal save() recreates that state.
        import glob
        import os

        db_path = os.path.join(self._tmpdir, "backfill_test.db")
        store = TraceStore(base_dir=self._tmpdir, db_path=db_path)
        trace_ids = []
        for i in range(3):
            tid = new_trace_id()
            trace_ids.append(tid)
            store.save(DecisionTrace(
                trace_id=tid, created_at=datetime.now(timezone.utc),
                execution_mode="BACKTEST", metadata={"i": i},
            ))
        store._db_conn().execute("DELETE FROM decision_traces")
        store._db_conn().commit()
        store._traces.clear()  # also drop the in-memory cache

        # Confirm the gap actually exists before backfilling.
        for tid in trace_ids:
            row = store._db_conn().execute(
                "SELECT 1 FROM decision_traces WHERE trace_id = ?", (tid,)
            ).fetchone()
            self.assertIsNone(row)

        backfilled = store.backfill_sqlite_mirror()
        self.assertEqual(backfilled, 3)

        # Delete the JSONL files so a fresh store can ONLY find these via SQLite.
        for path in glob.glob(os.path.join(self._tmpdir, "traces_*.jsonl")):
            os.remove(path)

        fresh_store = TraceStore(base_dir=self._tmpdir, db_path=db_path)
        for tid in trace_ids:
            retrieved = fresh_store.get(tid)
            self.assertIsNotNone(retrieved, f"{tid} should be findable after backfill")
            self.assertEqual(retrieved.trace_id, tid)

    def test_backfill_sqlite_mirror_keeps_the_latest_version_of_an_updated_trace(self):
        import os

        db_path = os.path.join(self._tmpdir, "backfill_latest_test.db")
        store = TraceStore(base_dir=self._tmpdir, db_path=db_path)
        trace_id = new_trace_id()
        t = DecisionTrace(
            trace_id=trace_id, created_at=datetime.now(timezone.utc),
            execution_mode="BACKTEST", metadata={"stage": "started"},
        )
        store.save(t)
        t.metadata["stage"] = "ensemble_done"  # a second, later append to the same file
        store.save(t)

        store._db_conn().execute("DELETE FROM decision_traces")
        store._db_conn().commit()
        store.backfill_sqlite_mirror()

        row = store._db_conn().execute(
            "SELECT payload FROM decision_traces WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        self.assertIn('"ensemble_done"', row[0])
        self.assertNotIn('"started"', row[0])

    def test_get_falls_back_to_file_scan_when_sqlite_lookup_errors(self):
        # A broken SQLite mirror must not make an otherwise-findable trace
        # (still on disk in a JSONL file) disappear.
        from unittest.mock import patch

        trace_id = new_trace_id()
        t = DecisionTrace(
            trace_id=trace_id, created_at=datetime.now(timezone.utc), execution_mode="BACKTEST",
        )
        self._store.save(t)
        self._store._traces.clear()  # force a cache miss without a restart

        with patch.object(self._store, "_db_conn", side_effect=RuntimeError("db unavailable")):
            retrieved = self._store.get(trace_id)

        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.trace_id, trace_id)


if __name__ == "__main__":
    unittest.main()
