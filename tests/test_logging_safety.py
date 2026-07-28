from __future__ import annotations

import logging
import unittest

from trading_platform.logging_safety import note_swallowed, redact_secret_text, swallowed_error_count


class LoggingSafetyTests(unittest.TestCase):
    def test_redacts_sensitive_broker_headers(self):
        raw = (
            "Headers: {'X-PrivateKey': 'api-key', 'Authorization': 'Bearer jwt-token', "
            "'x-feed-token': 'feed-secret', 'Accept': 'application/json'}"
        )

        redacted = redact_secret_text(raw)

        self.assertNotIn("api-key", redacted)
        self.assertNotIn("jwt-token", redacted)
        self.assertNotIn("feed-secret", redacted)
        self.assertIn("'Accept': 'application/json'", redacted)


class NoteSwallowedTests(unittest.TestCase):
    def test_logs_at_warning_not_debug(self):
        """Regression: previously logged at DEBUG, which is silently dropped
        app-wide since nothing here calls logging.basicConfig (root logger
        sits at Python's implicit WARNING default) — 57 real swallowed
        exceptions produced zero visible log lines in one session,
        2026-07-28. WARNING is the lowest level guaranteed visible without a
        wider logging reconfiguration."""
        logger = logging.getLogger("trading_platform.swallowed")
        with self.assertLogs(logger, level="WARNING") as captured:
            note_swallowed("test.component", ValueError("boom"))
        self.assertTrue(any("test.component" in line for line in captured.output))

    def test_redacts_secrets_in_swallowed_exception_text(self):
        logger = logging.getLogger("trading_platform.swallowed")
        secret_exc = ValueError("Headers: {'Authorization': 'Bearer jwt-token-xyz'}")
        with self.assertLogs(logger, level="WARNING") as captured:
            note_swallowed("test.component", secret_exc)
        joined = "\n".join(captured.output)
        self.assertNotIn("jwt-token-xyz", joined)

    def test_counter_increments_even_if_logging_fails(self):
        before = swallowed_error_count()
        note_swallowed("test.component", RuntimeError("x"))
        self.assertEqual(swallowed_error_count(), before + 1)
