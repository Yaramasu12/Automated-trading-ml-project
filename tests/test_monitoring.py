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


if __name__ == "__main__":
    unittest.main()
