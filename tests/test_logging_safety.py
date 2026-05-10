from __future__ import annotations

import unittest

from trading_platform.logging_safety import redact_secret_text


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
