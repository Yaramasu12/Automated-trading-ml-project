"""Unit tests for LiveTickFeed's multi-socket sharding (REDESIGN_PROMPT.md
§3.2 — "the single most important live-feed fix"): Angel One caps a single
SmartWebSocketV2 connection at 1000 tokens and 3 connections per client
code, so subscriptions beyond 1000 symbols must shard across additional
sockets instead of being silently truncated.

These tests exercise the sharding pieces directly (assignment, per-shard
subscribe routing, on_open, stop) rather than spinning up real background
threads, so they stay deterministic. `tests/test_live_feed.py`'s existing
reconnect tests already cover the single-shard path end to end and must
keep passing unchanged — sharding is designed to be inert until
subscriptions exceed 1000 tokens."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from trading_platform.data.live_feed import LiveTickFeed, _Shard


def _settings(max_symbols=None):
    return SimpleNamespace(live_feed_max_symbols=max_symbols)


class ShardAssignmentTests(unittest.TestCase):
    def test_up_to_1000_symbols_use_one_shard(self):
        feed = LiveTickFeed(_settings())
        feed._assign_symbols_to_shards([f"SYM{i}" for i in range(1000)])
        self.assertEqual(len(feed._shards), 1)
        self.assertEqual(len(feed._shards[0].symbols), 1000)

    def test_1001_symbols_spill_into_second_shard(self):
        feed = LiveTickFeed(_settings())
        feed._assign_symbols_to_shards([f"SYM{i}" for i in range(1001)])
        self.assertEqual(len(feed._shards), 2)
        self.assertEqual(len(feed._shards[0].symbols), 1000)
        self.assertEqual(len(feed._shards[1].symbols), 1)

    def test_2500_symbols_use_three_shards(self):
        feed = LiveTickFeed(_settings())
        feed._assign_symbols_to_shards([f"SYM{i}" for i in range(2500)])
        self.assertEqual([len(s.symbols) for s in feed._shards], [1000, 1000, 500])

    def test_beyond_3000_symbols_are_dropped_not_crashed(self):
        feed = LiveTickFeed(_settings())
        feed._assign_symbols_to_shards([f"SYM{i}" for i in range(3001)])
        self.assertEqual(len(feed._shards), 3)
        self.assertEqual(sum(len(s.symbols) for s in feed._shards), 3000)
        self.assertNotIn("SYM3000", feed._shard_of_symbol)

    def test_reassigning_already_assigned_symbol_is_noop(self):
        feed = LiveTickFeed(_settings())
        feed._assign_symbols_to_shards(["NIFTY"])
        first_shard = feed._shard_of_symbol["NIFTY"]

        feed._assign_symbols_to_shards(["NIFTY"])  # e.g. a resubscribe() replay

        self.assertEqual(feed._shard_of_symbol["NIFTY"], first_shard)
        self.assertEqual(feed._shards[first_shard].symbols.count("NIFTY"), 1)

    def test_correlation_ids_are_unique_uuids_not_hardcoded(self):
        feed = LiveTickFeed(_settings())
        feed._assign_symbols_to_shards([f"SYM{i}" for i in range(1001)])
        ids = {s.correlation_id for s in feed._shards}
        self.assertEqual(len(ids), 2)
        for cid in ids:
            self.assertNotEqual(cid, "abc123")


class OnOpenTests(unittest.TestCase):
    def test_on_open_subscribes_only_that_shards_symbols(self):
        feed = LiveTickFeed(_settings())
        feed._token_map = {"A": "1", "B": "2", "C": "3"}
        feed._exchange_map = {"A": "NSE", "B": "NSE", "C": "NSE"}
        feed._assign_symbols_to_shards(["A", "B"])  # "C" deliberately excluded
        shard = feed._shards[0]
        fake_ws = mock.Mock()

        feed._on_open(fake_ws, shard=shard)

        fake_ws.subscribe.assert_called_once()
        correlation_id, mode, token_list = fake_ws.subscribe.call_args[0]
        self.assertEqual(correlation_id, shard.correlation_id)
        self.assertNotEqual(correlation_id, "abc123")
        self.assertEqual(mode, 3)
        tokens = {t for entry in token_list for t in entry["tokens"]}
        self.assertEqual(tokens, {"1", "2"})


class SubscribeSymbolsRoutingTests(unittest.TestCase):
    def test_new_symbol_on_existing_connected_shard_sends_incremental_subscribe(self):
        feed = LiveTickFeed(_settings())
        feed._running = True
        feed._token_map = {"A": "1", "B": "2"}
        feed._exchange_map = {"A": "NSE", "B": "NSE"}
        feed._assign_symbols_to_shards(["A"])
        shard = feed._shards[0]
        shard.ws = mock.Mock()  # pretend already connected
        feed._spawn_shard_thread = mock.Mock()

        feed._subscribe_symbols(["B"])

        shard.ws.subscribe.assert_called_once()
        correlation_id, mode, token_list = shard.ws.subscribe.call_args[0]
        self.assertEqual(correlation_id, shard.correlation_id)
        tokens = {t for entry in token_list for t in entry["tokens"]}
        self.assertEqual(tokens, {"2"})  # only the new symbol, not re-sending "A"
        feed._spawn_shard_thread.assert_not_called()

    def test_overflow_past_a_full_shard_spawns_a_new_shard_thread(self):
        feed = LiveTickFeed(_settings())
        feed._running = True
        feed._assign_symbols_to_shards([f"SYM{i}" for i in range(1000)])  # fills shard 0
        feed._shards[0].ws = mock.Mock()
        spawned = []
        feed._spawn_shard_thread = lambda shard: spawned.append(shard.index)

        feed._subscribe_symbols(["OVERFLOW"])

        self.assertEqual(len(feed._shards), 2)
        self.assertEqual(spawned, [1])

    def test_not_running_is_a_noop(self):
        feed = LiveTickFeed(_settings())
        feed._running = False
        feed._subscribe_symbols(["A"])
        self.assertEqual(feed._shards, [])


class StopTests(unittest.TestCase):
    def test_stop_closes_all_shard_sockets(self):
        feed = LiveTickFeed(_settings())
        feed._assign_symbols_to_shards(["A"])
        feed._shards[0].ws = mock.Mock()
        feed._shards.append(_Shard(index=1, correlation_id="c1", ws=mock.Mock()))
        feed._running = True

        feed.stop()

        self.assertFalse(feed._running)
        feed._shards[0].ws.close_connection.assert_called_once()
        feed._shards[1].ws.close_connection.assert_called_once()

    def test_stop_with_no_shards_is_safe(self):
        feed = LiveTickFeed(_settings())
        feed.stop()  # must not raise
        self.assertFalse(feed._running)


class RunOrchestrationTests(unittest.TestCase):
    """Verifies _run() spawns one thread per extra shard but runs shard 0
    synchronously on the caller's own thread — without touching real
    threads or sockets, so this stays fast and deterministic."""

    def test_run_spawns_threads_for_extra_shards_and_runs_shard_zero_inline(self):
        feed = LiveTickFeed(_settings())
        feed._login = mock.Mock(return_value=({"data": {"jwtToken": "x"}}, "feedtok"))
        feed._settings.angel_one_client_code = "C1"
        feed._settings.angel_one_api_key = "K1"
        feed._subscribed_symbols = [f"SYM{i}" for i in range(1500)]
        feed._running = True

        spawned = []
        feed._spawn_shard_thread = lambda shard: spawned.append(shard.index)
        ran_inline = []
        feed._run_shard = lambda shard: ran_inline.append(shard.index)

        with mock.patch("SmartApi.smartWebSocketV2.SmartWebSocketV2", mock.Mock()):
            feed._run()

        self.assertEqual(len(feed._shards), 2)
        self.assertEqual(len(feed._shards[0].symbols), 1000)
        self.assertEqual(len(feed._shards[1].symbols), 500)
        self.assertEqual(spawned, [1])
        self.assertEqual(ran_inline, [0])

    def test_run_with_no_symbols_yet_still_stands_up_shard_zero(self):
        """Feed started before any symbols are subscribed — must still run
        shard 0's connect loop so add_subscriptions() has somewhere to go."""
        feed = LiveTickFeed(_settings())
        feed._login = mock.Mock(return_value=({"data": {"jwtToken": "x"}}, "feedtok"))
        feed._settings.angel_one_client_code = "C1"
        feed._settings.angel_one_api_key = "K1"
        feed._running = True

        ran_inline = []
        feed._run_shard = lambda shard: ran_inline.append(shard.index)

        with mock.patch("SmartApi.smartWebSocketV2.SmartWebSocketV2", mock.Mock()):
            feed._run()

        self.assertEqual(len(feed._shards), 1)
        self.assertEqual(ran_inline, [0])


class SnapshotShardHealthTests(unittest.TestCase):
    def test_snapshot_reports_per_shard_health(self):
        feed = LiveTickFeed(_settings())
        feed._assign_symbols_to_shards(["A", "B"])
        feed._shards[0].ws = mock.Mock()  # "connected"

        snap = feed.snapshot()

        self.assertEqual(len(snap["shards"]), 1)
        self.assertEqual(snap["shards"][0]["index"], 0)
        self.assertTrue(snap["shards"][0]["connected"])
        self.assertEqual(snap["shards"][0]["symbol_count"], 2)


if __name__ == "__main__":
    unittest.main()
