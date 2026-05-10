from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("AUTO_LOAD_MODELS", "0")

from trading_platform.api.runtime import TradingRuntime
from trading_platform.trace.ids import new_trace_id
from trading_platform.trace.learning_journal import PaperLearningJournal


class ArchitectureWiringTests(unittest.TestCase):
    def _isolated_runtime(self) -> TradingRuntime:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = TradingRuntime()
        runtime.paper_learning_journal = PaperLearningJournal(Path(tmp.name) / "paper_learning_journal.db")
        self.addCleanup(runtime.paper_learning_journal.close)
        return runtime

    def _paper_entry_exit_trace(self, runtime: TradingRuntime, strategy_name: str = "momentum_v1") -> str:
        runtime.set_execution_mode("PAPER")
        runtime.event_risk.blocked_dates = set()
        trace_id = new_trace_id("paper")

        entry = runtime._intent_from_payload(
            {
                "symbol": "RELIANCE",
                "side": "BUY",
                "quantity": 1,
                "price": 2800,
                "strategy_name": strategy_name,
                "metadata": {"trace_id": trace_id, "predicted_return": 0.02},
            }
        )
        asyncio.run(runtime.scheduler._process_intent(entry))

        exit_intent = runtime._intent_from_payload(
            {
                "symbol": "RELIANCE",
                "side": "SELL",
                "quantity": 1,
                "price": 2860,
                "priority": "TARGET",
                "strategy_name": "exit_manager:target",
                "metadata": {"trace_id": trace_id, "opens_position": False},
            }
        )
        asyncio.run(runtime.scheduler._process_intent(exit_intent))
        return trace_id

    def _live_runtime_with_canary(self, canary_ready: bool) -> TradingRuntime:
        runtime = TradingRuntime()
        runtime.set_execution_mode("LIVE")
        runtime.live_armed = True
        runtime.broker_client = lambda: runtime.paper_broker
        runtime._can_submit_live_orders = lambda: True
        runtime.live_readiness_payload = lambda: {
            "armed_eligible": True,
            "blocking_reasons": [],
        }
        runtime.live_canary_readiness_payload = lambda: {
            "can_consider_live_canary": canary_ready,
            "status": "READY" if canary_ready else "NOT_READY",
            "blocking_reasons": [] if canary_ready else ["trace_replay_evidence_present"],
        }
        return runtime

    def test_manual_approval_holds_large_intent_and_approval_enqueues(self):
        runtime = TradingRuntime()
        runtime.event_risk.blocked_dates = set()
        runtime.manual_approval.approval_threshold_notional = 2_000

        result = asyncio.run(
            runtime.enqueue_order(
                {
                    "symbol": "RELIANCE",
                    "side": "BUY",
                    "quantity": 2,
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

    def test_manual_order_cannot_approval_bypass_capital_risk(self):
        runtime = TradingRuntime()
        runtime.event_risk.blocked_dates = set()

        result = asyncio.run(
            runtime.enqueue_order(
                {
                    "symbol": "RELIANCE",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 2800,
                    "strategy_name": "capital_bypass_test",
                }
            )
        )

        self.assertFalse(result["enqueued"])
        self.assertFalse(result["approval_required"])
        self.assertTrue(result["risk_rejected"])
        self.assertEqual(result["final_gate"]["reason"], "position_size_exceeds_limit")
        self.assertEqual(runtime.manual_approval_status()["pending_count"], 0)
        self.assertEqual(runtime.scheduler_stats()["queue_depth"], 0)

    def test_trace_replay_reconstructs_final_gate_rejection(self):
        runtime = TradingRuntime()
        runtime.event_risk.blocked_dates = set()

        result = asyncio.run(
            runtime.enqueue_order(
                {
                    "symbol": "RELIANCE",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 2800,
                    "strategy_name": "trace_rejection_test",
                }
            )
        )

        replay = runtime.trace_replay(result["trace_id"])
        self.assertIsNotNone(replay)
        event_types = {event["event_type"] for event in replay["timeline"]}

        self.assertEqual(replay["summary"]["status"], "REJECTED")
        self.assertTrue(replay["summary"]["lifecycle_complete"])
        self.assertFalse(replay["summary"]["broker_submitted"])
        self.assertIn("final_gate_rejected", event_types)
        self.assertIn("risk_rejected", event_types)
        self.assertEqual(replay["orders"][0]["status"], "RISK_REJECTED")

    def test_scheduler_final_gate_blocks_direct_live_bypass(self):
        runtime = TradingRuntime()
        runtime.set_execution_mode("LIVE")
        intent = runtime._intent_from_payload(
            {
                "symbol": "RELIANCE",
                "side": "BUY",
                "quantity": 1,
                "price": 2800,
                "strategy_name": "direct_scheduler_bypass_test",
            }
        )

        asyncio.run(runtime.scheduler._process_intent(intent))

        events = runtime.oms.events_for_order(intent.idempotency_key)
        self.assertTrue(any(event["event_type"] == "risk_rejected" for event in events))
        self.assertTrue(any(event["rejection_reason"] == "live_mode_not_armed" for event in events))
        self.assertFalse(any(event["event_type"] == "broker_submitted" for event in events))

    def test_stop_loss_exit_can_still_queue_during_kill_switch(self):
        runtime = TradingRuntime()
        runtime.set_kill_switch(True)
        intent = runtime._intent_from_payload(
            {
                "symbol": "RELIANCE",
                "side": "SELL",
                "quantity": 1,
                "price": 2800,
                "priority": "STOP_LOSS",
                "strategy_name": "exit_manager:stop_loss",
                "metadata": {"opens_position": False},
            }
        )

        event_id = asyncio.run(runtime.scheduler.enqueue(intent))

        self.assertTrue(event_id.startswith("evt_"))
        self.assertEqual(runtime.scheduler_stats()["queue_depth"], 1)
        events = runtime.oms.events_for_order(intent.idempotency_key)
        self.assertTrue(any(event["event_type"] == "intent_queued" for event in events))
        self.assertFalse(any(event["event_type"] == "kill_switch_cancelled" for event in events))

    def test_trace_replay_reconstructs_fill_exit_label_and_learning(self):
        runtime = TradingRuntime()
        trace_id = self._paper_entry_exit_trace(runtime, "trace_fill_test")

        replay = runtime.trace_replay(trace_id)
        self.assertIsNotNone(replay)
        event_types = {event["event_type"] for event in replay["timeline"]}

        self.assertEqual(replay["summary"]["status"], "LABELED")
        self.assertTrue(replay["summary"]["lifecycle_complete"])
        self.assertEqual(replay["summary"]["order_count"], 2)
        self.assertEqual(replay["summary"]["label_count"], 1)
        self.assertGreaterEqual(replay["summary"]["fill_count"], 2)
        self.assertGreaterEqual(replay["summary"]["slippage_count"], 2)
        self.assertGreaterEqual(replay["summary"]["learning_update_count"], 1)
        self.assertIn("broker_filled", event_types)
        self.assertIn("exit_plan_created", event_types)
        self.assertIn("outcome_label_created", event_types)
        self.assertIn("post_trade_learning_updated", event_types)

        journal_events = runtime.paper_learning_journal.events_for_trace(trace_id)
        journal_event_types = [event["event_type"] for event in journal_events]
        self.assertGreaterEqual(journal_event_types.count("fill_recorded"), 2)
        self.assertGreaterEqual(journal_event_types.count("slippage_recorded"), 2)
        self.assertIn("outcome_label_created", journal_event_types)
        self.assertIn("post_trade_learning_updated", journal_event_types)

        label_events = [
            event for event in journal_events
            if event["event_type"] == "outcome_label_created"
        ]
        self.assertGreater(label_events[0]["payload"]["realized_slippage_pct"], 0)

        reloaded = PaperLearningJournal(runtime.paper_learning_journal.db_path)
        try:
            self.assertEqual(
                len(reloaded.events_for_trace(trace_id)),
                len(journal_events),
            )
        finally:
            reloaded.close()

    def test_policy_promotion_blocks_live_canary_without_paper_evidence(self):
        runtime = TradingRuntime()

        result = runtime.promote_policy({"policy_id": "momentum_v1", "status": "live_canary"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["gate"]["reason"], "trace_replay_evidence_present")
        self.assertEqual(runtime._policy_registry.get_record("momentum_v1").status, "paper")

    def test_policy_promotion_rejects_stage_skipping(self):
        runtime = TradingRuntime()

        result = runtime.promote_policy({"policy_id": "noop_baseline", "status": "live_canary"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["gate"]["reason"], "single_step_forward")
        self.assertEqual(runtime._policy_registry.get_record("noop_baseline").status, "shadow")

    def test_policy_promotion_allows_live_canary_with_clean_replay_evidence(self):
        runtime = TradingRuntime()
        runtime._policy_promotion_requirements.update(
            {
                "min_paper_trading_days": 1,
                "min_paper_fills": 2,
                "min_paper_labels": 1,
                "min_slippage_records": 2,
                "min_learning_updates": 1,
                "min_complete_replays": 1,
                "min_profit_factor": 1.0,
                "min_sharpe": 0.0,
                "max_drawdown_pct": 0.10,
                "max_avg_slippage_surprise": 0.01,
            }
        )
        trace_id = self._paper_entry_exit_trace(runtime, "momentum_v1")
        record = runtime._policy_registry.get_record("momentum_v1")
        record.metadata["promotion_evidence"] = {
            "trace_ids": [trace_id],
            "paper_trading_days": 1,
        }

        result = runtime.promote_policy({"policy_id": "momentum_v1", "status": "live_canary"})

        self.assertTrue(result["ok"])
        self.assertTrue(result["gate"]["approved"])
        self.assertEqual(runtime._policy_registry.get_record("momentum_v1").status, "live_canary")

    def test_policy_live_approved_requires_manual_gate(self):
        runtime = TradingRuntime()
        runtime._policy_promotion_requirements.update(
            {
                "min_paper_trading_days": 1,
                "min_paper_fills": 2,
                "min_paper_labels": 1,
                "min_slippage_records": 2,
                "min_learning_updates": 1,
                "min_complete_replays": 1,
                "min_profit_factor": 1.0,
                "min_sharpe": 0.0,
                "max_drawdown_pct": 0.10,
                "max_avg_slippage_surprise": 0.01,
            }
        )
        trace_id = self._paper_entry_exit_trace(runtime, "momentum_v1")
        record = runtime._policy_registry.get_record("momentum_v1")
        record.metadata["promotion_evidence"] = {
            "trace_ids": [trace_id],
            "paper_trading_days": 1,
        }
        runtime.promote_policy({"policy_id": "momentum_v1", "status": "live_canary"})

        blocked = runtime.promote_policy({"policy_id": "momentum_v1", "status": "live_approved"})
        approved = runtime.promote_policy(
            {"policy_id": "momentum_v1", "status": "live_approved", "manual_approval": True}
        )

        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["gate"]["reason"], "manual_live_approval_required")
        self.assertTrue(approved["ok"])
        self.assertEqual(runtime._policy_registry.get_record("momentum_v1").status, "live_approved")

    def test_live_canary_readiness_blocks_without_paper_window(self):
        runtime = self._isolated_runtime()

        readiness = runtime.live_canary_readiness_payload()

        self.assertFalse(readiness["can_consider_live_canary"])
        self.assertEqual(readiness["paper_window"]["actual_days"], 0)
        self.assertIn("trace_replay_evidence_present", readiness["blocking_reasons"])

    def test_live_canary_readiness_requires_policy_gate_candidate(self):
        runtime = self._isolated_runtime()
        runtime._policy_promotion_requirements.update(
            {
                "min_paper_trading_days": 1,
                "min_paper_fills": 2,
                "min_paper_labels": 1,
                "min_slippage_records": 2,
                "min_learning_updates": 1,
                "min_complete_replays": 1,
                "min_profit_factor": 1.0,
                "min_sharpe": 0.0,
                "max_drawdown_pct": 0.10,
                "max_avg_slippage_surprise": 0.01,
            }
        )
        self._paper_entry_exit_trace(runtime, "momentum_v1")

        readiness = runtime.live_canary_readiness_payload()

        self.assertFalse(readiness["can_consider_live_canary"])
        self.assertIn("policy_gate_ready", readiness["blocking_reasons"])
        self.assertGreaterEqual(readiness["metrics"]["label_count"], 1)

    def test_live_canary_readiness_allows_consideration_with_clean_policy_gate(self):
        runtime = self._isolated_runtime()
        runtime._policy_promotion_requirements.update(
            {
                "min_paper_trading_days": 1,
                "min_paper_fills": 2,
                "min_paper_labels": 1,
                "min_slippage_records": 2,
                "min_learning_updates": 1,
                "min_complete_replays": 1,
                "min_profit_factor": 1.0,
                "min_sharpe": 0.0,
                "max_drawdown_pct": 0.10,
                "max_avg_slippage_surprise": 0.01,
            }
        )
        trace_id = self._paper_entry_exit_trace(runtime, "momentum_v1")
        record = runtime._policy_registry.get_record("momentum_v1")
        record.metadata["promotion_evidence"] = {
            "trace_ids": [trace_id],
            "paper_trading_days": 1,
        }

        readiness = runtime.live_canary_readiness_payload()

        self.assertTrue(readiness["can_consider_live_canary"])
        self.assertEqual(readiness["status"], "READY")
        self.assertEqual(readiness["blocking_reasons"], [])
        self.assertEqual(readiness["paper_window"]["actual_days"], 1)

    def test_live_canary_readiness_blocks_stale_review_window(self):
        runtime = self._isolated_runtime()
        runtime._policy_promotion_requirements.update(
            {
                "min_paper_trading_days": 1,
                "min_paper_fills": 2,
                "min_paper_labels": 1,
                "min_slippage_records": 2,
                "min_learning_updates": 1,
                "min_complete_replays": 1,
                "min_profit_factor": 1.0,
                "min_sharpe": 0.0,
                "max_drawdown_pct": 0.10,
                "max_avg_slippage_surprise": 0.01,
            }
        )
        trace_id = self._paper_entry_exit_trace(runtime, "momentum_v1")
        record = runtime._policy_registry.get_record("momentum_v1")
        record.metadata["promotion_evidence"] = {
            "trace_ids": [trace_id],
            "paper_trading_days": 1,
        }
        stale_days = {f"2026-01-{day:02d}" for day in range(1, 22)}
        runtime._live_canary_evidence_trace_ids = lambda: ([trace_id], stale_days)

        readiness = runtime.live_canary_readiness_payload()

        self.assertFalse(readiness["can_consider_live_canary"])
        self.assertTrue(readiness["paper_window"]["review_overdue"])
        self.assertIn("paper_review_overdue", readiness["blocking_reasons"])

    def test_arm_live_requires_m6_canary_readiness(self):
        runtime = TradingRuntime()
        runtime.live_readiness_payload = lambda: {
            "armed_eligible": True,
            "blocking_reasons": [],
        }
        runtime.live_canary_readiness_payload = lambda: {
            "can_consider_live_canary": False,
            "blocking_reasons": ["trace_replay_evidence_present"],
        }

        with self.assertRaisesRegex(ValueError, "live_canary_readiness_blocked"):
            runtime.arm_live(True)

        self.assertFalse(runtime.live_armed)

    def test_final_gate_blocks_live_entry_when_m6_not_ready(self):
        runtime = self._live_runtime_with_canary(canary_ready=False)
        intent = runtime._intent_from_payload(
            {
                "symbol": "RELIANCE",
                "side": "BUY",
                "quantity": 1,
                "price": 2800,
                "strategy_name": "m6_live_entry_block_test",
            }
        )

        decision = runtime._evaluate_final_execution_gate(
            intent,
            payload={"_trace_side_effects": False},
        )

        self.assertFalse(decision.approved)
        self.assertTrue(decision.reason.startswith("live_canary_readiness_blocked"))
        self.assertEqual(
            decision.details["canary_readiness"]["blocking_reasons"],
            ["trace_replay_evidence_present"],
        )

    def test_final_gate_allows_protective_live_exit_when_m6_not_ready(self):
        runtime = self._live_runtime_with_canary(canary_ready=False)
        intent = runtime._intent_from_payload(
            {
                "symbol": "RELIANCE",
                "side": "SELL",
                "quantity": 1,
                "price": 2800,
                "priority": "STOP_LOSS",
                "strategy_name": "m6_live_exit_allowed_test",
                "metadata": {"opens_position": False},
            }
        )

        decision = runtime._evaluate_final_execution_gate(
            intent,
            payload={"_trace_side_effects": False},
        )

        self.assertTrue(decision.approved)

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
