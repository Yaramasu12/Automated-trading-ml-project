"""Unit tests for LiveTickFeed's subscription cap/open-position exemption and
reconnect-loop robustness, added 2026-08-05 alongside the price-resolution
hardening work (see price_service.py's module docstring for the incident
that prompted all of it)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from datetime import date

from trading_platform.data.live_feed import LiveTickFeed, Tick, resolve_underlying_reference_tick


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
                mock.patch("trading_platform.data.live_feed.time.sleep"), \
                mock.patch("trading_platform.data.live_feed._market_is_open_now", return_value=True):
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
                mock.patch("trading_platform.data.live_feed.time.sleep"), \
                mock.patch("trading_platform.data.live_feed._market_is_open_now", return_value=True):
            feed._run()

        self.assertFalse(feed._running)
        self.assertEqual(fake_ws_instance.connect.call_count, 1)


def _tick(symbol: str, price: float) -> Tick:
    return Tick(
        symbol=symbol, token="1", exchange="MCX", last_price=price,
        open=price, high=price, low=price, close=price, volume=0,
    )


class ResolveUnderlyingReferenceTickTests(unittest.TestCase):
    """2026-09-03: MCX commodities have no cash/spot market -- only futures --
    so live_feed.latest_tick(bare_underlying_name) always returned None for
    them, while the front-month future (the same contract select_future()
    resolves elsewhere) does tick. Confirmed live: this left every MCX
    options-chain query at spot_price=0 despite real options data being
    available, and separately hard-blocked every MCX futures entry via
    pipeline.py's "no_live_tick" gate. 11 call sites across the codebase had
    each reinvented the same bare-name-only lookup; this is the single
    shared resolver they now all use instead."""

    def test_returns_bare_name_tick_when_available(self):
        """NSE indices/equities: the common case, no futures fallback needed."""
        live_feed = mock.Mock()
        live_feed.latest_tick.return_value = _tick("NIFTY", 24000.0)
        instrument_master = mock.Mock()

        tick = resolve_underlying_reference_tick(live_feed, instrument_master, "NIFTY")

        self.assertEqual(tick.last_price, 24000.0)
        instrument_master.select_future.assert_not_called()

    def test_falls_back_to_front_month_future_for_mcx(self):
        live_feed = mock.Mock()

        def fake_latest_tick(symbol):
            if symbol == "CRUDEOIL":
                return None  # no cash/spot tick -- MCX has none
            if symbol == "CRUDEOIL21SEP26FUT":
                return _tick("CRUDEOIL21SEP26FUT", 6543.0)
            return None

        live_feed.latest_tick.side_effect = fake_latest_tick
        instrument_master = mock.Mock()
        instrument_master.select_future.return_value = SimpleNamespace(symbol="CRUDEOIL21SEP26FUT")

        tick = resolve_underlying_reference_tick(
            live_feed, instrument_master, "CRUDEOIL", as_of=date(2026, 9, 3)
        )

        self.assertIsNotNone(tick)
        self.assertEqual(tick.last_price, 6543.0)
        instrument_master.select_future.assert_called_once_with("CRUDEOIL", date(2026, 9, 3))

    def test_zero_price_tick_also_triggers_fallback(self):
        """A tick object that exists but carries last_price=0 (e.g. an echoed
        placeholder) must not be treated as a usable reference price."""
        live_feed = mock.Mock()

        def fake_latest_tick(symbol):
            if symbol == "GOLD":
                return _tick("GOLD", 0.0)
            if symbol == "GOLD05OCT26FUT":
                return _tick("GOLD05OCT26FUT", 91234.0)
            return None

        live_feed.latest_tick.side_effect = fake_latest_tick
        instrument_master = mock.Mock()
        instrument_master.select_future.return_value = SimpleNamespace(symbol="GOLD05OCT26FUT")

        tick = resolve_underlying_reference_tick(live_feed, instrument_master, "GOLD")

        self.assertEqual(tick.last_price, 91234.0)

    def test_no_future_contract_returns_none_not_raise(self):
        """select_future() raises ValueError when no contract is found (its
        own documented behavior) -- this must degrade to None, never propagate."""
        live_feed = mock.Mock()
        live_feed.latest_tick.return_value = None
        instrument_master = mock.Mock()
        instrument_master.select_future.side_effect = ValueError("No future contract found")

        tick = resolve_underlying_reference_tick(live_feed, instrument_master, "UNKNOWN")

        self.assertIsNone(tick)

    def test_no_tick_at_all_returns_none(self):
        live_feed = mock.Mock()
        live_feed.latest_tick.return_value = None
        instrument_master = mock.Mock()
        instrument_master.select_future.return_value = SimpleNamespace(symbol="X01JAN27FUT")

        tick = resolve_underlying_reference_tick(live_feed, instrument_master, "X")

        self.assertIsNone(tick)


if __name__ == "__main__":
    unittest.main()
