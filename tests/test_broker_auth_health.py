"""Tests for the shared Angel One auth-health tracker + circuit breaker.

Covers the 2026-10-27 hardening: classify auth vs transient failures, trip the
breaker after N consecutive auth failures, short-circuit further logins while
open, recover on success, fire the operator alert exactly once, and expose a
/health snapshot. See trading_platform/broker/auth_health.py.
"""
from __future__ import annotations

import unittest
from unittest import mock

from trading_platform.broker import auth_health
from trading_platform.broker.auth_health import BrokerAuthCircuitOpen

_ENV = {
    "ANGEL_AUTH_BREAKER_ENABLED": "true",
    "ANGEL_AUTH_BREAKER_THRESHOLD": "3",
    "ANGEL_AUTH_BREAKER_COOLDOWN_SECONDS": "900",
    # Keep alerting delivery inert (no network) regardless of the host env.
    "MONITORING_TELEGRAM_ENABLED": "false",
    "ANGEL_AUTH_ALERT_WEBHOOK_URL": "",
}


class AuthErrorClassificationTests(unittest.TestCase):
    def test_ab1050_and_totp_messages_are_auth_errors(self):
        self.assertTrue(auth_health.is_auth_error("Angel One login failed: {'errorcode': 'AB1050'}"))
        self.assertTrue(auth_health.is_auth_error("Invalid totp and client combination"))
        self.assertTrue(auth_health.is_auth_error("unauthorized"))

    def test_rate_limit_and_network_are_not_auth_errors(self):
        self.assertFalse(auth_health.is_auth_error("Access denied because of exceeding access rate"))
        self.assertFalse(auth_health.is_auth_error("rate_limited: too many requests"))
        self.assertFalse(auth_health.is_auth_error("connection reset / timed out"))
        self.assertFalse(auth_health.is_auth_error(""))
        self.assertFalse(auth_health.is_auth_error(None))


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        auth_health.reset()
        self._p = mock.patch.dict("os.environ", _ENV, clear=False)
        self._p.start()
        # Capture alerts instead of delivering them.
        self._alerts: list[tuple] = []
        self._ap = mock.patch.object(
            auth_health._AUTH, "_alert",
            side_effect=lambda *a: self._alerts.append(a),
        )
        self._ap.start()

    def tearDown(self):
        self._ap.stop()
        self._p.stop()
        auth_health.reset()

    def test_breaker_stays_closed_below_threshold(self):
        auth_health.record_failure("login: AB1050 Invalid totp")
        auth_health.record_failure("login: AB1050 Invalid totp")
        # 2 < threshold(3): still closed, no raise, no alert.
        auth_health.before_login_attempt()  # must not raise
        self.assertFalse(auth_health.snapshot()["breaker_open"])
        self.assertEqual(self._alerts, [])

    def test_breaker_opens_at_threshold_and_blocks_logins(self):
        for _ in range(3):
            auth_health.record_failure("login: {'errorcode': 'AB1050'}")
        snap = auth_health.snapshot()
        self.assertTrue(snap["breaker_open"])
        self.assertFalse(snap["ok"])
        self.assertEqual(snap["consecutive_auth_failures"], 3)
        with self.assertRaises(BrokerAuthCircuitOpen):
            auth_health.before_login_attempt()

    def test_alert_fires_exactly_once_per_episode(self):
        for _ in range(5):  # keep failing past the threshold
            auth_health.record_failure("AB1050 invalid totp")
        self.assertEqual(len(self._alerts), 1)
        self.assertEqual(self._alerts[0][0], "CRITICAL")

    def test_transient_failures_never_trip_the_breaker(self):
        for _ in range(10):
            auth_health.record_failure("rate_limited: exceeding access rate")
        snap = auth_health.snapshot()
        self.assertFalse(snap["breaker_open"])
        self.assertEqual(snap["consecutive_auth_failures"], 0)
        self.assertIn("rate", (snap["last_error"] or "").lower())
        auth_health.before_login_attempt()  # must not raise

    def test_success_closes_breaker_and_sends_recovery_alert(self):
        for _ in range(3):
            auth_health.record_failure("AB1050 invalid totp")
        self.assertEqual(len(self._alerts), 1)  # the CRITICAL
        auth_health.record_success()
        snap = auth_health.snapshot()
        self.assertTrue(snap["ok"])
        self.assertFalse(snap["breaker_open"])
        self.assertEqual(snap["consecutive_auth_failures"], 0)
        self.assertIsNotNone(snap["last_success_at"])
        self.assertEqual(len(self._alerts), 2)  # CRITICAL + RECOVERED
        self.assertEqual(self._alerts[1][0], "RECOVERED")
        auth_health.before_login_attempt()  # closed again: must not raise

    def test_half_open_allows_one_trial_after_cooldown(self):
        with mock.patch.dict("os.environ", {"ANGEL_AUTH_BREAKER_COOLDOWN_SECONDS": "0"}):
            for _ in range(3):
                auth_health.record_failure("AB1050 invalid totp")
            # cooldown 0 → deadline already elapsed → half-open trial permitted.
            auth_health.before_login_attempt()  # must not raise

    def test_breaker_can_be_disabled_by_env(self):
        with mock.patch.dict("os.environ", {"ANGEL_AUTH_BREAKER_ENABLED": "false"}):
            for _ in range(10):
                auth_health.record_failure("AB1050 invalid totp")
            auth_health.before_login_attempt()  # disabled → never raises


if __name__ == "__main__":
    unittest.main()
