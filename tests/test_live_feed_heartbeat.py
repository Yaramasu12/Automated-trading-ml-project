"""Unit tests for LiveTickFeed's per-socket heartbeat watchdog
(REDESIGN_PROMPT.md §3.2: "a socket silent >N seconds in market hours is
force-reconnected even if it never fired on_close — silent half-open
sockets are a real Angel One failure mode").

FeedStalenessTracker already answers "did THIS SYMBOL tick recently?" —
these tests cover the separate, new question "is THIS SOCKET alive at
all?", which matters because a socket can sit open, deliver nothing, and
never fire on_close on its own."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from trading_platform.data.live_feed import LiveTickFeed, _Shard


def _settings():
    return SimpleNamespace(live_feed_max_symbols=None)


def _market_open(open_=True):
    return mock.patch.multiple(
        "trading_platform.data.live_feed",
        is_entry_allowed=mock.Mock(return_value=open_),
        is_mcx_entry_allowed=mock.Mock(return_value=False),
    )


class HeartbeatCheckTests(unittest.TestCase):
    def _feed_with_shard(self, *, connected_at=None, last_message_at=None, symbols=("NIFTY",)):
        feed = LiveTickFeed(_settings())
        shard = _Shard(index=0, correlation_id="c0", symbols=list(symbols), ws=mock.Mock(),
                        connected_at=connected_at, last_message_at=last_message_at)
        feed._shards.append(shard)
        return feed, shard

    def test_silent_shard_beyond_timeout_during_market_hours_is_force_reconnected(self):
        stale_time = datetime.now(timezone.utc) - timedelta(
            seconds=LiveTickFeed._SOCKET_HEARTBEAT_TIMEOUT_SECONDS + 5
        )
        feed, shard = self._feed_with_shard(connected_at=stale_time, last_message_at=stale_time)

        with _market_open(True):
            feed._check_shard_heartbeats()

        shard.ws.close_connection.assert_called_once()
        self.assertIsNone(shard.connected_at)
        self.assertIsNone(shard.last_message_at)

    def test_recently_ticking_shard_is_left_alone(self):
        fresh = datetime.now(timezone.utc) - timedelta(seconds=1)
        feed, shard = self._feed_with_shard(connected_at=fresh, last_message_at=fresh)

        with _market_open(True):
            feed._check_shard_heartbeats()

        shard.ws.close_connection.assert_not_called()

    def test_outside_market_hours_a_silent_shard_is_not_touched(self):
        stale_time = datetime.now(timezone.utc) - timedelta(hours=1)
        feed, shard = self._feed_with_shard(connected_at=stale_time, last_message_at=stale_time)

        with _market_open(False):
            feed._check_shard_heartbeats()

        shard.ws.close_connection.assert_not_called()

    def test_never_ticked_shard_uses_connected_at_as_fallback_reference(self):
        """The silent-half-open case: on_open fired long ago, on_data never
        has — must still be caught, not skipped just because last_message_at
        is None."""
        long_ago = datetime.now(timezone.utc) - timedelta(
            seconds=LiveTickFeed._SOCKET_HEARTBEAT_TIMEOUT_SECONDS + 5
        )
        feed, shard = self._feed_with_shard(connected_at=long_ago, last_message_at=None)

        with _market_open(True):
            feed._check_shard_heartbeats()

        shard.ws.close_connection.assert_called_once()

    def test_shard_with_no_connected_at_yet_is_skipped(self):
        """Still mid-handshake (on_open hasn't fired) — not this watchdog's job."""
        feed, shard = self._feed_with_shard(connected_at=None, last_message_at=None)

        with _market_open(True):
            feed._check_shard_heartbeats()

        shard.ws.close_connection.assert_not_called()

    def test_shard_with_no_socket_yet_is_skipped(self):
        feed, shard = self._feed_with_shard(connected_at=datetime.now(timezone.utc) - timedelta(hours=1))
        shard.ws = None

        with _market_open(True):
            feed._check_shard_heartbeats()  # must not raise

    def test_shard_with_no_symbols_is_skipped(self):
        stale_time = datetime.now(timezone.utc) - timedelta(hours=1)
        feed, shard = self._feed_with_shard(connected_at=stale_time, last_message_at=stale_time, symbols=())

        with _market_open(True):
            feed._check_shard_heartbeats()

        shard.ws.close_connection.assert_not_called()


class OnOpenOnDataHeartbeatWiringTests(unittest.TestCase):
    def test_on_open_sets_connected_at_and_clears_last_message_at(self):
        feed = LiveTickFeed(_settings())
        shard = _Shard(index=0, correlation_id="c0", last_message_at=datetime.now(timezone.utc))
        feed._shards.append(shard)

        feed._on_open(mock.Mock(), shard=shard)

        self.assertIsNotNone(shard.connected_at)
        self.assertIsNone(shard.last_message_at)

    def test_on_data_records_last_message_at_even_on_unparseable_message(self):
        feed = LiveTickFeed(_settings())
        shard = _Shard(index=0, correlation_id="c0")

        feed._on_data(mock.Mock(), "not a valid tick payload", shard=shard)

        self.assertIsNotNone(shard.last_message_at)


class ForceReconnectTests(unittest.TestCase):
    def test_force_reconnect_closes_socket_and_clears_heartbeat_state(self):
        feed = LiveTickFeed(_settings())
        shard = _Shard(index=0, correlation_id="c0", ws=mock.Mock(),
                        connected_at=datetime.now(timezone.utc), last_message_at=datetime.now(timezone.utc))

        feed._force_reconnect_shard(shard)

        shard.ws.close_connection.assert_called_once()
        self.assertIsNone(shard.connected_at)
        self.assertIsNone(shard.last_message_at)

    def test_force_reconnect_with_no_socket_is_safe(self):
        feed = LiveTickFeed(_settings())
        shard = _Shard(index=0, correlation_id="c0", ws=None)
        feed._force_reconnect_shard(shard)  # must not raise

    def test_force_reconnect_swallows_close_connection_errors(self):
        feed = LiveTickFeed(_settings())
        shard = _Shard(index=0, correlation_id="c0", ws=mock.Mock())
        shard.ws.close_connection.side_effect = RuntimeError("socket already dead")

        feed._force_reconnect_shard(shard)  # must not raise


class SnapshotHeartbeatAgeTests(unittest.TestCase):
    def test_snapshot_reports_last_message_age(self):
        feed = LiveTickFeed(_settings())
        feed._assign_symbols_to_shards(["A"])
        feed._shards[0].ws = mock.Mock()
        feed._shards[0].last_message_at = datetime.now(timezone.utc) - timedelta(seconds=5)

        snap = feed.snapshot()

        age = snap["shards"][0]["last_message_age_seconds"]
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 5)
        self.assertLess(age, 6)

    def test_snapshot_age_is_none_when_never_connected(self):
        feed = LiveTickFeed(_settings())
        feed._assign_symbols_to_shards(["A"])

        snap = feed.snapshot()

        self.assertIsNone(snap["shards"][0]["last_message_age_seconds"])


if __name__ == "__main__":
    unittest.main()
