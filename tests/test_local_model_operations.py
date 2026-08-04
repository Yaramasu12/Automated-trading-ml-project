"""Tests for the continuous local-model operations feature set: the 24/7
runtime monitor, the post-market daily review/tuning advisor, and the
strategy-hypothesis research assistant (idea generation only)."""
from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import date
from pathlib import Path

from trading_platform.api.runtime import TradingRuntime
from trading_platform.data.persistence import TradingDatabase
from trading_platform.monitoring.metrics import OperationalEventLogHandler, OperationalMonitor


class TestPersistenceNewTables(unittest.TestCase):
    """Round-trip tests for the four new tables, isolated in a tempdir SQLite
    file (mirrors tests/test_diagnostic_report_fixes.py's pattern)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(Path(self._tmp.name) / "trading.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_monitor_digest_round_trip(self):
        self.db.save_monitor_digest(
            severity="warn", summary_text="something's off",
            anomalies=["rising swallow count"], metrics={"swallowed_errors": 3},
        )
        digests = self.db.recent_monitor_digests(limit=5)
        self.assertEqual(len(digests), 1)
        self.assertEqual(digests[0]["severity"], "warn")
        self.assertEqual(digests[0]["anomalies"], ["rising swallow count"])
        self.assertEqual(digests[0]["metrics"], {"swallowed_errors": 3})

    def test_monitor_digest_order_most_recent_first(self):
        self.db.save_monitor_digest(severity="ok", summary_text="1", anomalies=[], metrics={})
        self.db.save_monitor_digest(severity="ok", summary_text="2", anomalies=[], metrics={})
        digests = self.db.recent_monitor_digests(limit=5)
        self.assertEqual(digests[0]["summary_text"], "2")

    def test_json_field_decodes_sqlite_style_string(self):
        # SQLite has no native JSON type — these columns come back as raw text.
        self.assertEqual(TradingDatabase._json_field('["a", "b"]', default=[]), ["a", "b"])

    def test_json_field_passes_through_postgres_style_list(self):
        # Regression 2026-07-28: psycopg2 auto-decodes JSONB columns to Python
        # objects on SELECT — every /monitor/runtime-digest call 500'd against
        # the live Postgres-backed container (TypeError: json object must be
        # str, bytes or bytearray, not list) even though the SQLite-backed
        # unit tests above passed, because they never exercised this path.
        self.assertEqual(TradingDatabase._json_field(["a", "b"], default=[]), ["a", "b"])

    def test_json_field_defaults_on_none_or_empty(self):
        self.assertEqual(TradingDatabase._json_field(None, default=[]), [])
        self.assertEqual(TradingDatabase._json_field("", default={}), {})

    def test_daily_ai_review_upsert(self):
        today = date(2026, 7, 28)
        self.db.save_daily_ai_review(today, "first pass", "qwen/qwen3.6-35b-a3b")
        self.db.save_daily_ai_review(today, "revised", "qwen/qwen3.6-35b-a3b")  # same date -> update
        review = self.db.get_daily_ai_review(today)
        self.assertEqual(review["summary_text"], "revised")

    def test_daily_ai_review_missing_date_returns_none(self):
        self.assertIsNone(self.db.get_daily_ai_review(date(2020, 1, 1)))

    def test_tuning_suggestion_lifecycle(self):
        today = date(2026, 7, 28)
        sid = self.db.save_tuning_suggestion(
            review_date=today, parameter="CREW_HOLD_BAND", current_value="0.10",
            proposed_value="0.08", rationale="win rate improving on weak signals",
            confidence=0.6, model_id="google/gemma-4-e4b",
        )
        pending = self.db.tuning_suggestions(status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], sid)

        self.assertTrue(self.db.update_tuning_suggestion_status(sid, "approved"))
        self.assertEqual(self.db.tuning_suggestions(status="pending"), [])
        approved = self.db.tuning_suggestions(status="approved")
        self.assertEqual(approved[0]["id"], sid)
        self.assertIsNotNone(approved[0]["reviewed_at"])

    def test_update_unknown_suggestion_returns_false(self):
        self.assertFalse(self.db.update_tuning_suggestion_status(999999, "approved"))

    def test_strategy_hypothesis_lifecycle(self):
        hid = self.db.save_strategy_hypothesis(
            title="Overnight gap fade on high-OI strikes",
            thesis_text="...", suggested_universe="NIFTY options",
            suggested_features="gap_pct, oi_change", model_id="qwen/qwen3.6-35b-a3b",
        )
        proposed = self.db.strategy_hypotheses(status="proposed")
        self.assertEqual(proposed[0]["id"], hid)

        self.assertTrue(self.db.mark_hypothesis_tested(hid, "run-123", "rejected: AUC 0.499"))
        self.assertEqual(self.db.strategy_hypotheses(status="proposed"), [])
        tested = self.db.strategy_hypotheses(status="tested")
        self.assertEqual(tested[0]["validation_verdict"], "rejected: AUC 0.499")

    def test_mark_unknown_hypothesis_returns_false(self):
        self.assertFalse(self.db.mark_hypothesis_tested(999999, "run-1", "n/a"))


class TestOperationalEventLogHandler(unittest.TestCase):
    def setUp(self):
        self.monitor = OperationalMonitor()
        self.handler = OperationalEventLogHandler(self.monitor)
        self.logger = logging.getLogger("test_local_model_operations.probe")
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)

    def tearDown(self):
        self.logger.removeHandler(self.handler)

    def test_warning_is_forwarded(self):
        self.logger.warning("disk usage high")
        events = self.monitor.recent_events(limit=10)
        self.assertTrue(any(e["message"] == "disk usage high" and e["severity"] == "WARNING" for e in events))

    def test_info_is_not_forwarded(self):
        before = len(self.monitor.events)
        self.logger.info("routine tick")
        self.assertEqual(len(self.monitor.events), before)

    def test_recent_warnings_filters_severity(self):
        self.logger.info("noise")
        self.logger.warning("signal")
        warnings = self.monitor.recent_warnings(limit=10)
        self.assertTrue(all(w["severity"] in ("WARNING", "ERROR", "CRITICAL") for w in warnings))
        self.assertTrue(any(w["message"] == "signal" for w in warnings))

    def test_handler_failure_never_raises(self):
        # record_event raising should not propagate out of emit().
        broken_monitor = OperationalMonitor()
        broken_monitor.record_event = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        handler = OperationalEventLogHandler(broken_monitor)
        record = logging.LogRecord("x", logging.WARNING, __file__, 1, "msg", None, None)
        handler.emit(record)  # must not raise

    def test_warnings_since_excludes_events_before_cutoff(self):
        # Regression 2026-07-28: a single one-time startup warning kept
        # re-appearing in every runtime-monitor tick's prompt under the old
        # "last N ever" window, causing repeated false severity escalation
        # for state that hadn't changed since process start.
        from datetime import datetime, timezone
        import time

        self.logger.warning("stale startup notice")
        cutoff = datetime.now(timezone.utc)
        time.sleep(0.01)
        self.logger.warning("genuinely new issue")

        since_cutoff = self.monitor.warnings_since(cutoff, limit=10)
        messages = [w["message"] for w in since_cutoff]
        self.assertIn("genuinely new issue", messages)
        self.assertNotIn("stale startup notice", messages)


class TestRuntimeMonitorTick(unittest.TestCase):
    """The monitor's deterministic digest path must never depend on — or be
    blocked by — the LLM call, since the whole point is a reliable 24/7
    baseline signal even when the shared gateway is stubbed/saturated."""

    def setUp(self):
        self.runtime = TradingRuntime()
        # Force deterministic stub behavior. TradingRuntime() loads the real
        # .env from cwd (config.py's _load_env_file), so relying on the
        # gateway being unreachable to fall back to stub is a latent trap:
        # confirmed 2026-08-04 — passes on bare Windows (host.docker.internal
        # in .env doesn't resolve outside a container) but a "stub gateway is
        # safe" test made a genuine real LM Studio call and failed when run
        # from inside a container where that hostname DOES resolve. These
        # tests are about stub-mode behavior specifically, so force it rather
        # than depend on incidental network reachability.
        if self.runtime._llm_gateway is not None:
            self.runtime._llm_gateway.runtime = "stub"

    def test_tick_runs_and_stores_a_digest_with_stub_gateway(self):
        before = len(self.runtime.db.recent_monitor_digests(limit=1000))
        self.runtime._run_monitor_tick()
        after = self.runtime.db.recent_monitor_digests(limit=1000)
        self.assertEqual(len(after), before + 1)
        self.assertIn(after[0]["severity"], ("ok", "warn", "critical"))

    def test_tick_never_raises_even_if_gateway_missing(self):
        self.runtime._llm_gateway = None
        self.runtime._run_monitor_tick()  # must not raise

    def test_second_tick_does_not_resurface_warning_from_before_first_tick(self):
        # Regression 2026-07-28: the prompt sent to the model must only
        # contain warnings that are new since the previous tick, not a
        # rolling "last N ever" window — otherwise a single old warning (e.g.
        # the one-time startup "AI layers DEGRADED" log line) keeps looking
        # fresh forever and the model keeps re-escalating severity for
        # nothing having actually changed.
        gw = self.runtime._llm_gateway
        if gw is None:
            self.skipTest("AI council disabled in this environment")

        logging.getLogger("test_local_model_operations.pre_existing").warning("pre-existing issue")

        prompts: list[str] = []
        original_generate = gw.generate

        def capturing_generate(model, system, user, *a, **k):
            prompts.append(user)
            return original_generate(model, system, user, *a, **k)

        gw.generate = capturing_generate
        try:
            self.runtime._run_monitor_tick()  # tick 1: sees the pre-existing warning
            self.runtime._run_monitor_tick()  # tick 2: should NOT see it again
        finally:
            gw.generate = original_generate

        self.assertIn("pre-existing issue", prompts[0])
        self.assertNotIn("pre-existing issue", prompts[1])


class TestDailyAiReview(unittest.TestCase):
    def setUp(self):
        self.runtime = TradingRuntime()
        # Force deterministic stub behavior. TradingRuntime() loads the real
        # .env from cwd (config.py's _load_env_file), so relying on the
        # gateway being unreachable to fall back to stub is a latent trap:
        # confirmed 2026-08-04 — passes on bare Windows (host.docker.internal
        # in .env doesn't resolve outside a container) but a "stub gateway is
        # safe" test made a genuine real LM Studio call and failed when run
        # from inside a container where that hostname DOES resolve. These
        # tests are about stub-mode behavior specifically, so force it rather
        # than depend on incidental network reachability.
        if self.runtime._llm_gateway is not None:
            self.runtime._llm_gateway.runtime = "stub"

    def test_run_daily_ai_review_with_stub_gateway_is_safe(self):
        # Stub responses don't carry "summary"/"suggestions" keys, so this
        # exercises the graceful-degradation path end to end.
        result = self.runtime.run_daily_ai_review()
        self.assertEqual(result["review_date"], date.today().isoformat())
        self.assertEqual(result["suggestions"], [])
        review = self.runtime.daily_ai_review()
        self.assertIsNotNone(review)

    def test_suggestions_outside_allowed_parameters_are_never_persisted(self):
        # A model proposing e.g. "MAX_DRAWDOWN" (a real risk parameter, not in
        # the tunable allowlist) must be silently dropped, never stored.
        gw = self.runtime._llm_gateway
        if gw is None:
            self.skipTest("AI council disabled in this environment")
        original_generate = gw.generate
        gw.generate = lambda *a, **k: {
            "model_id": "test", "failure_mode": None,
            "summary": "test", "suggestions": [
                {"parameter": "MAX_DRAWDOWN", "current_value": "0.10",
                 "proposed_value": "0.20", "rationale": "x", "confidence": 0.9},
            ],
        }
        try:
            result = self.runtime.run_daily_ai_review()
        finally:
            gw.generate = original_generate
        self.assertEqual(result["suggestions"], [])
        pending = self.runtime.tuning_suggestions(status="pending", limit=1000)
        self.assertFalse(any(s["parameter"] == "MAX_DRAWDOWN" for s in pending))

    def test_set_tuning_suggestion_status_rejects_invalid_status(self):
        with self.assertRaises(ValueError):
            self.runtime.set_tuning_suggestion_status(1, "not_a_real_status")


class TestGenerateStrategyHypotheses(unittest.TestCase):
    def setUp(self):
        self.runtime = TradingRuntime()
        # Force deterministic stub behavior. TradingRuntime() loads the real
        # .env from cwd (config.py's _load_env_file), so relying on the
        # gateway being unreachable to fall back to stub is a latent trap:
        # confirmed 2026-08-04 — passes on bare Windows (host.docker.internal
        # in .env doesn't resolve outside a container) but a "stub gateway is
        # safe" test made a genuine real LM Studio call and failed when run
        # from inside a container where that hostname DOES resolve. These
        # tests are about stub-mode behavior specifically, so force it rather
        # than depend on incidental network reachability.
        if self.runtime._llm_gateway is not None:
            self.runtime._llm_gateway.runtime = "stub"

    def test_generate_with_stub_gateway_is_safe(self):
        result = self.runtime.generate_strategy_hypotheses()
        self.assertEqual(result["hypotheses"], [])
        self.assertIn("note", result)

    def test_generate_with_gateway_disabled(self):
        self.runtime._llm_gateway = None
        result = self.runtime.generate_strategy_hypotheses()
        self.assertEqual(result["hypotheses"], [])


if __name__ == "__main__":
    unittest.main()
