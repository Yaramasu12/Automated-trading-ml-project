from __future__ import annotations

import unittest

from trading_platform.monitoring.metrics import OperationalMonitor


class MonitoringTests(unittest.TestCase):
    def test_monitor_records_order_metrics(self):
        monitor = OperationalMonitor()

        monitor.record_order({"status": "FILLED", "latency_ms": 12})
        monitor.record_order({"status": "RISK_REJECTED", "latency_ms": None})
        snapshot = monitor.snapshot("PAPER", live_armed=False, kill_switch_active=False)

        self.assertEqual(snapshot.total_orders, 2)
        self.assertEqual(snapshot.filled_orders, 1)
        self.assertEqual(snapshot.rejected_orders, 1)
        self.assertEqual(snapshot.rejection_rate, 0.5)
        self.assertEqual(snapshot.status, "DEGRADED")

    def test_monitor_reports_halted_when_kill_switch_active(self):
        monitor = OperationalMonitor()

        snapshot = monitor.snapshot("LIVE", live_armed=False, kill_switch_active=True)

        self.assertEqual(snapshot.status, "HALTED")

    def test_recent_events_are_serialized(self):
        monitor = OperationalMonitor()
        monitor.record_event("test", "hello")

        events = monitor.recent_events()

        self.assertEqual(events[0]["event_type"], "test")
        self.assertIn("timestamp", events[0])

    def test_critical_write_failure_degrades_status(self):
        # OBS-2: a money-path WRITE swallow must flip the top-line status to
        # DEGRADED (previously it only moved the runtime-monitor digest's
        # severity, a separate channel the badge never read).
        monitor = OperationalMonitor()
        healthy = monitor.snapshot("PAPER", live_armed=False, kill_switch_active=False)
        self.assertEqual(healthy.status, "HEALTHY")
        self.assertEqual(healthy.critical_write_failures, 0)

        degraded = monitor.snapshot(
            "PAPER", live_armed=False, kill_switch_active=False,
            critical_write_failures=1,
        )
        self.assertEqual(degraded.status, "DEGRADED")
        self.assertEqual(degraded.critical_write_failures, 1)
        self.assertIn("critical_write_failures", degraded.to_dict())

    def test_kill_switch_outranks_critical_write_failure(self):
        # HALTED (kill switch) must win over DEGRADED, as for every other signal.
        monitor = OperationalMonitor()
        snap = monitor.snapshot(
            "LIVE", live_armed=False, kill_switch_active=True,
            critical_write_failures=3,
        )
        self.assertEqual(snap.status, "HALTED")


class CriticalSwallowClassifierTests(unittest.TestCase):
    """OBS-2: only money-path WRITE components count as critical; the constant
    benign background swallows (news, AI council, vector memory, analytics,
    the fail-closed data fallback) must never degrade health."""

    def setUp(self):
        import trading_platform.logging_safety as ls
        # Isolate the process-global swallow counters for this test.
        self._ls = ls
        self._saved_by = dict(ls._SWALLOWED_BY_COMPONENT)
        self._saved_count = ls._SWALLOWED["count"]
        ls._SWALLOWED_BY_COMPONENT.clear()
        ls._SWALLOWED["count"] = 0

    def tearDown(self):
        self._ls._SWALLOWED_BY_COMPONENT.clear()
        self._ls._SWALLOWED_BY_COMPONENT.update(self._saved_by)
        self._ls._SWALLOWED["count"] = self._saved_count

    def test_only_critical_components_counted(self):
        ls = self._ls
        ls.note_swallowed("periodic_news_fetch", RuntimeError("no feed"))
        ls.note_swallowed("vector_memory.embed", RuntimeError("qdrant down"))
        ls.note_swallowed("pipeline.real_bars_fallback", RuntimeError("rate limit"))
        self.assertEqual(ls.critical_swallowed_error_count(), 0)  # all benign

        ls.note_swallowed("save_exit_plan", RuntimeError("db write failed"))
        ls.note_swallowed("settle_expired.apply_fill", RuntimeError("db write failed"))
        self.assertEqual(ls.critical_swallowed_error_count(), 2)
        # The full breakdown still shows everything, critical or not.
        self.assertGreaterEqual(ls.swallowed_error_count(), 5)


if __name__ == "__main__":
    unittest.main()
