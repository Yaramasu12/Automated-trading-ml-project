from __future__ import annotations

import asyncio
import dataclasses
import unittest
from unittest.mock import Mock

from trading_platform.api.runtime import TradingRuntime
from trading_platform.config import Settings, load_settings
from trading_platform.domain.enums import ExecutionMode


class RuntimeTests(unittest.TestCase):
    def test_live_arm_requires_credentials_and_flag(self):
        runtime = TradingRuntime(
            Settings(
                execution_mode=ExecutionMode.LIVE,
                broker="ANGEL_ONE",
                live_trading_enabled=False,
                initial_capital=1_000_000,
                max_drawdown=0.10,
                max_daily_loss=0.02,
                max_position_pct=0.05,
                max_margin_utilization=0.60,
                live_order_confirmation="",
                angel_one_api_key="",
                angel_one_api_secret="",
                angel_one_client_code="",
                angel_one_pin="",
                angel_one_totp_secret="",
                angel_one_instrument_master_url="https://example.invalid/OpenAPIScripMaster.json",
                angel_one_instrument_cache_path="data/processed/test_angel_instruments.json",
                aws_region="ap-south-1",
            )
        )

        with self.assertRaises(ValueError):
            runtime.arm_live(True)

    def test_execution_mode_toggle_changes_runtime_without_enabling_live_orders(self):
        runtime = TradingRuntime()

        state = runtime.set_execution_mode("LIVE")

        self.assertEqual(state["execution_mode"], "LIVE")
        self.assertFalse(state["live_order_confirmation_ready"])
        self.assertFalse(state["live_armed"])

    def test_startup_does_not_auto_start_broker_loops_by_default(self):
        base = load_settings()
        settings = dataclasses.replace(base, auto_start_agent=False, auto_start_live_feed=False)
        runtime = TradingRuntime(settings)
        runtime.start_live_feed = Mock()  # type: ignore[method-assign]
        runtime.agent.start = Mock()  # type: ignore[method-assign]

        async def run_lifecycle() -> None:
            await runtime.start_async_services()
            await runtime.stop_async_services()

        asyncio.run(run_lifecycle())

        runtime.start_live_feed.assert_not_called()
        runtime.agent.start.assert_not_called()

    def test_live_arm_requires_explicit_real_money_confirmation(self):
        runtime = TradingRuntime(
            Settings(
                execution_mode=ExecutionMode.LIVE,
                broker="ANGEL_ONE",
                live_trading_enabled=True,
                initial_capital=1_000_000,
                max_drawdown=0.10,
                max_daily_loss=0.02,
                max_position_pct=0.05,
                max_margin_utilization=0.60,
                live_order_confirmation="",
                angel_one_api_key="api-key",
                angel_one_api_secret="secret-key",
                angel_one_client_code="client",
                angel_one_pin="1234",
                angel_one_totp_secret="ABCDEF",
                angel_one_instrument_master_url="https://example.invalid/OpenAPIScripMaster.json",
                angel_one_instrument_cache_path="data/processed/test_angel_instruments.json",
                aws_region="ap-south-1",
            )
        )

        with self.assertRaises(ValueError):
            runtime.arm_live(True)

    def test_live_arm_allows_only_when_every_live_gate_is_present(self):
        """Phase 1 hardening: even with every credential field set,
        arm_live MUST refuse on synthetic instrument master + dead feed.

        Operators must explicitly refresh from Angel One and start the
        feed before live arming becomes possible.
        """
        runtime = TradingRuntime(
            Settings(
                execution_mode=ExecutionMode.LIVE,
                broker="ANGEL_ONE",
                live_trading_enabled=True,
                initial_capital=1_000_000,
                max_drawdown=0.10,
                max_daily_loss=0.02,
                max_position_pct=0.05,
                max_margin_utilization=0.60,
                live_order_confirmation="I_ACCEPT_REAL_MONEY_LIVE_ORDERS",
                angel_one_api_key="api-key",
                angel_one_api_secret="secret-key",
                angel_one_client_code="client",
                angel_one_pin="1234",
                angel_one_totp_secret="ABCDEF",
                angel_one_instrument_master_url="https://example.invalid/OpenAPIScripMaster.json",
                angel_one_instrument_cache_path="data/processed/test_angel_instruments.json",
                aws_region="ap-south-1",
            )
        )

        readiness = runtime.live_readiness_payload()
        self.assertFalse(readiness["armed_eligible"])
        # The synthetic-universe gate must be among the blockers.
        self.assertIn("instrument_master_is_synthetic", readiness["blocking_reasons"])

        with self.assertRaisesRegex(ValueError, "live_readiness_blocked"):
            runtime.arm_live(True)

        # Once we record a real refresh + tick a subscribed symbol,
        # readiness flips green and arm_live succeeds.
        runtime.instrument_freshness.record_refresh(
            source="https://api.angelone.in/test", parsed_count=120_000
        )
        runtime.live_feed._subscribed_symbols = ["NIFTY"]
        runtime.live_feed._running = True
        runtime.live_feed.staleness_tracker.record("NIFTY")
        runtime.live_canary_readiness_payload = lambda: {
            "can_consider_live_canary": True,
            "blocking_reasons": [],
        }

        state = runtime.arm_live(True)
        self.assertTrue(state["live_armed"])
        self.assertTrue(state["live_order_confirmation_ready"])

    def test_backtest_endpoint_payload(self):
        runtime = TradingRuntime()
        result = runtime.run_backtest({"days": 30, "underlyings": ["NIFTY", "RELIANCE"]})

        self.assertIn("metrics", result)
        self.assertIn("selected_strategies", result)

    def test_retraining_decision_endpoint(self):
        runtime = TradingRuntime()
        result = runtime.retraining_decision(
            {
                "model_name": "sentiment",
                "profit_factor": 1.2,
                "sharpe": 0.7,
                "drawdown": 0.03,
                "iv_interval_coverage": 0.95,
                "sentiment_precision": 0.7,
                "sample_size": 50,
            }
        )

        self.assertTrue(result["should_retrain"])
        self.assertEqual(result["reason"], "sentiment_precision_below_threshold")

    def test_model_catalog_and_forecast_endpoints(self):
        runtime = TradingRuntime()
        catalog = runtime.model_catalog()
        forecast = runtime.volatility_forecast({"symbol": "NIFTY", "days": 30})

        self.assertGreater(catalog["count"], 0)
        self.assertIn("volatility", catalog["by_family"])
        self.assertIn("forecast", forecast)
        self.assertGreater(forecast["forecast"]["daily_volatility"], 0)

    def test_sentiment_and_model_selection_endpoints(self):
        runtime = TradingRuntime()
        sentiment = runtime.sentiment({"text": "Bank reports strong profit growth"})
        selection = runtime.model_selection(
            {
                "performances": [
                    {
                        "model_name": "garch_baseline",
                        "profit_factor": 1.3,
                        "sharpe": 0.8,
                        "drawdown": 0.04,
                        "iv_interval_coverage": 0.94,
                        "sentiment_precision": 0.90,
                        "sample_size": 100,
                    }
                ]
            }
        )

        self.assertEqual(sentiment["sentiment"]["label"], "POSITIVE")
        self.assertEqual(selection["selected_model"], "garch_baseline")

    def test_data_status_reports_cache_and_credentials(self):
        from unittest.mock import patch, PropertyMock
        runtime = TradingRuntime()
        with patch.object(
            type(runtime.settings), "angel_one_configured",
            new_callable=PropertyMock, return_value=False,
        ):
            status = runtime.data_status()

        self.assertEqual(status["instrument_source"], "angel_one")
        self.assertIn("instrument_cache_exists", status)
        self.assertFalse(status["angel_one_configured"])

    def test_account_status_is_read_only_and_reports_live_gate(self):
        from unittest.mock import patch, PropertyMock
        runtime = TradingRuntime()
        with patch.object(
            type(runtime.settings), "angel_one_configured",
            new_callable=PropertyMock, return_value=False,
        ):
            status = runtime.account_status()

        self.assertEqual(status["broker"], "ANGEL_ONE")
        self.assertFalse(status["read_only_available"])
        self.assertFalse(status["live_orders_possible"])

    def test_account_snapshot_requires_credentials(self):
        from unittest.mock import patch, PropertyMock
        runtime = TradingRuntime()
        # Settings is a frozen dataclass, so patch the property on its class directly.
        with patch.object(
            type(runtime.settings), "angel_one_configured",
            new_callable=PropertyMock, return_value=False,
        ):
            with self.assertRaises(ValueError):
                runtime.account_snapshot()

    def test_strategy_catalog_endpoint_payload(self):
        runtime = TradingRuntime()
        catalog = runtime.strategy_catalog()

        self.assertGreater(catalog["count"], 10)
        self.assertIn("options", catalog["by_family"])

    def test_strategy_evaluation_endpoint_payload(self):
        runtime = TradingRuntime()
        result = runtime.evaluate_strategies(
            {
                "days": 30,
                "underlyings": ["NIFTY", "RELIANCE"],
                "strategy_names": ["futures_trend", "equity_momentum"],
            }
        )

        self.assertEqual(len(result["leaderboard"]), 2)
        self.assertIn("best_strategy", result)
        self.assertIn("selection_policy", result)

    def test_signal_scan_endpoint_does_not_submit_orders(self):
        runtime = TradingRuntime()
        result = runtime.signal_scan(
            {
                "underlyings": ["NIFTY", "RELIANCE"],
                "days": 30,
                "strategy_names": ["futures_trend", "equity_momentum"],
            }
        )

        self.assertEqual(result["submitted_orders"], 0)
        self.assertEqual(len(result["scans"]), 2)
        self.assertIn("approved_candidates", result)
        self.assertEqual(
            set(result.keys()),
            {"mode", "submitted_orders", "approved_candidates", "rejected_candidates", "scans"},
        )

    def test_high_end_scan_uses_baseline_first_canonical_cycle(self):
        # Force all advisory layers off so the test is environment-independent
        # (e.g. .env may have ENABLE_AI_COUNCIL=true in dev/production).
        base_settings = load_settings()
        settings = dataclasses.replace(
            base_settings,
            enable_ai_council=False,
            enable_neural_lab=False,
            enable_quantum_lab=False,
            enable_marl_lab=False,
        )
        runtime = TradingRuntime(settings)
        result = runtime.high_end_signal_scan(
            {
                "symbols": ["NIFTY"],
                "days": 30,
                "strategy_names": ["futures_trend"],
            }
        )

        self.assertIn("trace_id", result)
        self.assertEqual(result["symbols"], ["NIFTY"])
        self.assertIn("baseline", result)
        self.assertEqual(result["baseline"]["submitted_orders"], 0)
        self.assertEqual(len(result["baseline"]["scans"]), 1)
        self.assertIn("blackboard", result)
        self.assertEqual(result["blackboard"]["trace_id"], result["trace_id"])
        self.assertGreaterEqual(result["blackboard"]["n_pipeline_candidates"], 0)
        self.assertIsNone(result["ai_council"])
        self.assertIsNone(result["neural"])
        self.assertIsNone(result["quantum"])
        self.assertIsNone(result["ensemble"])
        replay = runtime.trace_replay(result["trace_id"])
        self.assertIsNotNone(replay)
        event_types = {event["event_type"] for event in replay["timeline"]}
        self.assertIn("decision_cycle_started", event_types)
        self.assertIn("baseline_scan_completed", event_types)

    def test_shadow_run_requires_paper_mode(self):
        from trading_platform.config import Settings
        runtime = TradingRuntime(
            Settings(
                execution_mode=ExecutionMode.BACKTEST,
                broker="ANGEL_ONE",
                live_trading_enabled=False,
                initial_capital=1_000_000,
                max_drawdown=0.10,
                max_daily_loss=0.02,
                max_position_pct=0.05,
                max_margin_utilization=0.60,
                live_order_confirmation="",
                angel_one_api_key="",
                angel_one_api_secret="",
                angel_one_client_code="",
                angel_one_pin="",
                angel_one_totp_secret="",
                angel_one_instrument_master_url="https://example.invalid/OpenAPIScripMaster.json",
                angel_one_instrument_cache_path="data/processed/test_angel_instruments.json",
                aws_region="ap-south-1",
            )
        )

        with self.assertRaises(ValueError):
            runtime.shadow_run({"underlyings": ["RELIANCE"], "strategy_names": ["equity_momentum"]})

    def test_shadow_run_executes_only_in_simulated_paper(self):
        runtime = TradingRuntime()
        runtime.set_execution_mode("PAPER")
        # Use multiple underlyings + strategies to reliably hit ≥1 signal
        result = runtime.shadow_run(
            {
                "underlyings": ["NIFTY", "RELIANCE", "BANKNIFTY"],
                "days": 60,
                "strategy_names": ["futures_trend", "mean_reversion", "breakout"],
            }
        )

        self.assertEqual(result["mode"], "PAPER")
        self.assertGreaterEqual(result["submitted_orders"], 1)
        self.assertEqual(result["submitted_orders"], result["filled_orders"] + result["rejected_orders"])
        self.assertIn("average_latency_ms", result)
        filled = next((item for item in result["executions"] if item["trade"]), None)
        if filled:
            journal = runtime.paper_learning_journal_status(trace_id=filled["trace_id"])
            self.assertEqual(journal["fill_count"], 1)
            self.assertEqual(journal["slippage_count"], 1)

    def test_monitoring_metrics_tracks_shadow_orders(self):
        runtime = TradingRuntime()
        before = runtime.monitoring_metrics()
        runtime.set_execution_mode("PAPER")
        runtime.shadow_run(
            {
                "underlyings": ["NIFTY", "RELIANCE", "BANKNIFTY"],
                "days": 60,
                "strategy_names": ["futures_trend", "mean_reversion", "breakout"],
            }
        )
        after = runtime.monitoring_metrics()
        events = runtime.monitoring_events()

        self.assertEqual(before["total_orders"], 0)
        self.assertGreater(after["total_orders"], 0)
        self.assertIn(after["status"], {"HEALTHY", "DEGRADED"})
        self.assertGreater(events["count"], 0)

    def test_derivatives_endpoint_payloads(self):
        runtime = TradingRuntime()
        expiries = runtime.expiries("NIFTY")
        chain = runtime.option_chain("NIFTY", expiries["nearest"], spot_price=22500)
        greeks = runtime.calculate_greeks(
            {
                "spot_price": 22500,
                "strike": 22500,
                "days_to_expiry": 7,
                "volatility": 0.18,
                "option_type": "CE",
            }
        )

        self.assertGreater(expiries["count"], 0)
        self.assertGreater(chain["call_count"], 0)
        self.assertIn("delta", greeks)

    def test_target_progress_and_supervisor_payloads(self):
        runtime = TradingRuntime()
        target = runtime.target_progress(
            {
                "start_date": "2026-01-01",
                "as_of": "2026-02-01",
                "current_equity": 1_000_000,
                "profit_factor": 1.0,
                "sharpe": 0.2,
            }
        )
        supervisor = runtime.supervisor_decision({"drawdown": 0.08})

        self.assertIn("required_run_rate", target)
        self.assertEqual(supervisor["action"], "REDUCE")

    def test_order_preview_runs_risk_without_submitting(self):
        runtime = TradingRuntime()
        preview = runtime.preview_order(
            {
                "symbol": "RELIANCE",
                "side": "BUY",
                "quantity": 1,
                "price": 2800,
                "strategy_name": "manual_preview",
            }
        )

        self.assertTrue(preview["approved"])
        self.assertEqual(preview["intent"]["symbol"], "RELIANCE")
        self.assertEqual(preview["intent"]["notional_value"], 2800)

    def test_order_preview_blocks_live_when_not_armed(self):
        runtime = TradingRuntime()
        runtime.set_execution_mode("LIVE")

        preview = runtime.preview_order(
            {
                "symbol": "RELIANCE",
                "side": "BUY",
                "quantity": 1,
                "price": 2800,
            }
        )

        self.assertFalse(preview["approved"])
        self.assertEqual(preview["reason"], "live_mode_not_armed")

    def test_paper_order_uses_shared_router(self):
        runtime = TradingRuntime()
        runtime.set_execution_mode("PAPER")

        result = runtime.simulate_order(
            {
                "symbol": "RELIANCE",
                "side": "BUY",
                "quantity": 1,
                "price": 2800,
            }
        )

        self.assertEqual(result["mode"], "PAPER")
        self.assertEqual(result["order"]["status"], "FILLED")
        self.assertEqual(result["risk_decision"]["reason"], "approved")
        journal = runtime.paper_learning_journal_status(trace_id=result["trace_id"])
        self.assertEqual(journal["fill_count"], 1)
        self.assertEqual(journal["slippage_count"], 1)
        self.assertGreater(journal["recent_events"][0]["payload"]["realized_slippage_pct"], 0)
        replay = runtime.trace_replay(result["trace_id"])
        self.assertEqual(replay["summary"]["fill_count"], 1)
        self.assertEqual(replay["summary"]["slippage_count"], 1)

    def test_paper_order_refuses_live_mode(self):
        runtime = TradingRuntime()
        runtime.set_execution_mode("LIVE")

        with self.assertRaises(ValueError):
            runtime.simulate_order(
                {
                    "symbol": "RELIANCE",
                    "side": "BUY",
                    "quantity": 1,
                    "price": 2800,
                }
            )


if __name__ == "__main__":
    unittest.main()
