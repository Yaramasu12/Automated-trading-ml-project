from __future__ import annotations

import asyncio
import unittest

from trading_platform.api.runtime import TradingRuntime


class ArchitectureWiringTests(unittest.TestCase):
    def test_manual_approval_holds_large_intent_and_approval_enqueues(self):
        runtime = TradingRuntime()
        runtime.event_risk.blocked_dates = set()

        result = asyncio.run(
            runtime.enqueue_order(
                {
                    "symbol": "RELIANCE",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 2800,
                    "strategy_name": "manual_approval_test",
                }
            )
        )

        self.assertFalse(result["enqueued"])
        self.assertTrue(result["approval_required"])
        self.assertEqual(runtime.manual_approval_status()["pending_count"], 1)

        request_id = result["approval_request"]["request_id"]
        approved = asyncio.run(runtime.approve_order(request_id, {"approval_reason": "unit test"}))

        self.assertTrue(approved["approved"])
        self.assertTrue(approved["enqueue"]["enqueued"])
        self.assertEqual(runtime.manual_approval_status()["pending_count"], 0)
        self.assertEqual(runtime.scheduler_stats()["queue_depth"], 1)

    def test_news_event_updates_event_risk_and_regime_context(self):
        runtime = TradingRuntime()

        news = runtime.news_analyze(
            {
                "headline": "Breaking RBI shock may hurt banks",
                "summary": "RBI policy shock and market fall risk for BANKNIFTY",
                "country": "IN",
            }
        )
        event_risk = runtime.event_risk_check()
        regime = runtime.current_regime("NIFTY")

        self.assertEqual(news["recommended_action"], "BLOCK_ENTRIES")
        self.assertTrue(event_risk["blocked"])
        self.assertEqual(regime["adjusted_regime"], "EVENT_RISK")

    def test_event_bus_and_broker_capabilities_are_exposed(self):
        runtime = TradingRuntime()

        bus = runtime.event_bus_summary()
        capabilities = runtime.broker_capability_status()

        self.assertEqual(bus["backend"], "in_memory")
        self.assertGreaterEqual(bus["event_count"], 1)
        self.assertIn("active_broker", capabilities)
        self.assertGreaterEqual(len(capabilities["brokers"]), 2)


if __name__ == "__main__":
    unittest.main()
