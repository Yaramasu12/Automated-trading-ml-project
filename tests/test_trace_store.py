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


if __name__ == "__main__":
    unittest.main()
