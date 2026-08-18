"""Unit tests for LiveTickFeed's subscription cap/open-position exemption and
reconnect-loop robustness, added 2026-08-05 alongside the price-resolution
hardening work (see price_service.py's module docstring for the incident
that prompted all of it)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from trading_platform.data.live_feed import LiveTickFeed


def _settings(max_symbols=3):
    return SimpleNamespace(live_feed_max_symbols=max_symbols)


class SubscriptionCapTests(unittest.TestCase):
    def _feed(self, max_symbols=3, protected=frozenset()):
        return LiveTickFeed(_settings(max_symbols), get_protected_symbols=lambda: set(protected))

    def test_additions_within_cap_all_go_through(self):
        feed = self._feed(max_symbols=5)
        feed.add_subscriptions(["NIFTY", "BANKNIFTY"])
        self.assertEqual(set(feed.subscribed_symbols()), {"NIFTY", "BANKNIFTY"})

    def test_optional_additions_beyond_cap_are_dropped(self):
        feed = self._feed(max_symbols=2)
        feed.add_subscriptions(["A", "B", "C", "D"])
        self.assertEqual(len(feed.subscribed_symbols()), 2)

    def test_protected_symbols_always_go_through_past_the_cap(self):
        feed = self._feed(max_symbols=2, protected={"FINNIFTY25AUG2628400CE"})
        feed.add_subscriptions(["A", "B", "FINNIFTY25AUG2628400CE"])
        subs = set(feed.subscribed_symbols())
        self.assertIn("FINNIFTY25AUG2628400CE", subs)
        # cap only bounds the OPTIONAL portion; protected pushes past it
        self.assertLessEqual(len(subs) - 1, 2)

    def test_protected_symbol_added_alone_ignores_cap_entirely(self):
        feed = self._feed(max_symbols=1, protected={"X", "Y", "Z"})
        feed.add_subscriptions(["X"])
        feed.add_subscriptions(["Y"])
        feed.add_subscriptions(["Z"])
        self.assertEqual(set(feed.subscribed_symbols()), {"X", "Y", "Z"})

    def test_no_cap_configured_behaves_uncapped(self):
        feed = self._feed(max_symbols=None)
        feed.add_subscriptions([f"SYM{i}" for i in range(20)])
        self.assertEqual(len(feed.subscribed_symbols()), 20)

    def test_get_protected_symbols_exception_does_not_block_additions(self):
        settings = _settings(max_symbols=5)

        def boom():
            raise RuntimeError("portfolio not ready")

        feed = LiveTickFeed(settings, get_protected_symbols=boom)
        feed.add_subscriptions(["NIFTY"])
        self.assertEqual(feed.subscribed_symbols(), ["NIFTY"])

    def test_default_protected_symbols_is_empty_set(self):
        feed = LiveTickFeed(_settings(max_symbols=1))
        feed.add_subscriptions(["A", "B"])
        self.assertEqual(len(feed.subscribed_symbols()), 1)


class ReconnectRobustnessTests(unittest.TestCase):
    """Patches RawAngelOneWebSocket, the DEFAULT ws class since 2026-08-10 (SmartWebSocketV2 itself was proven to deliver zero ticks — see its docstring). The reconnect/backoff behaviour under test lives in _run(), above the ws-class selection, so it applies identically either way.

    2026-08-05 fix: the feed used to permanently give up after
    _MAX_RETRIES and stay dead until the market-hours-gated scan watchdog
    noticed. It must now keep retrying indefinitely at the capped backoff."""

    def test_run_keeps_retrying_past_max_retries(self):
        feed = LiveTickFeed(_settings())
        feed._login = mock.Mock(return_value=({"data": {"jwtToken": "x"}}, "feedtok"))
        feed._settings.angel_one_client_code = "C1"
        feed._settings.angel_one_api_key = "K1"
        feed._running = True

        connect_calls = {"n": 0}

        def fake_connect():
            connect_calls["n"] += 1
            if connect_calls["n"] > feed._MAX_RETRIES + 3:
                feed._running = False  # stop the test, not the production loop
            raise RuntimeError("socket drop")

        fake_ws_cls = mock.Mock()
        fake_ws_instance = mock.Mock()
        fake_ws_instance.connect.side_effect = fake_connect
        fake_ws_cls.return_value = fake_ws_instance

        with mock.patch("trading_platform.data.live_feed.RawAngelOneWebSocket", fake_ws_cls), \
                mock.patch("trading_platform.data.live_feed.time.sleep"):
            feed._run()

        # Proves the loop did NOT stop at _MAX_RETRIES — it kept going until
        # the test itself flipped _running off well past that point.
        self.assertGreater(connect_calls["n"], feed._MAX_RETRIES)
        self.assertFalse(feed._running)

    def test_run_stops_when_stop_is_called_externally(self):
        feed = LiveTickFeed(_settings())
        feed._login = mock.Mock(return_value=({"data": {"jwtToken": "x"}}, "feedtok"))
        feed._settings.angel_one_client_code = "C1"
        feed._settings.angel_one_api_key = "K1"
        feed._running = True

        def fake_connect():
            feed._running = False  # simulate stop() being called mid-flight
            raise RuntimeError("socket drop")

        fake_ws_instance = mock.Mock()
        fake_ws_instance.connect.side_effect = fake_connect
        fake_ws_cls = mock.Mock(return_value=fake_ws_instance)

        with mock.patch("trading_platform.data.live_feed.RawAngelOneWebSocket", fake_ws_cls), \
                mock.patch("trading_platform.data.live_feed.time.sleep"):
            feed._run()

        self.assertFalse(feed._running)
        self.assertEqual(fake_ws_instance.connect.call_count, 1)


if __name__ == "__main__":
    unittest.main()
