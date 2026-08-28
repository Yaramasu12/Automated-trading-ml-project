"""Integration tests for the Redis Streams tick bus + LiveTickFeed integration.

These tests verify that:
1. The TickBus publishes and subscribes correctly
2. LiveTickFeed's inject_tick flows through the tick bus
3. Tick v2 normalization works correctly
4. Multiple consumer groups can subscribe independently
5. Staleness tracking integrates with the bus

2026-08-27: rewritten against the REAL TickBus/TickV2/FeedStalenessTracker
APIs. The previous version was written against an imagined API that never
existed (TickV2.symbol instead of .symbol_id, FeedStalenessTracker's
constructor taking timeout_seconds instead of hard_seconds/warn_seconds, a
callback-based subscribe(segment, handler) instead of the real
asyncio.Queue-based subscribe(group_name, segments)) — every TickBus method
is also genuinely async (backed by redis.asyncio), so calling them without
await silently built coroutine objects that were never run. Some assertions
happened to pass anyway because nothing they depended on ever executed
(test_unsubscribe_stops_consumer asserted "the handler received nothing",
which is trivially true when the whole call chain is inert coroutines).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import trading_platform.streaming.tick_bus as tick_bus_module
from trading_platform.data.tick_v2 import TickV2, FeedSource, make_tick_v2
from trading_platform.streaming.tick_bus import TickBus, TickBusError, TickBusNotStartedError
from trading_platform.data.live_feed import LiveTickFeed, Tick
from trading_platform.data.feed_staleness import FeedStalenessTracker


def _run(coro):
    """Drive a coroutine synchronously — no pytest-asyncio installed."""
    return asyncio.run(coro)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_tick_bus() -> None:
    """Ensure TickBus singleton is clean for every test.

    The singleton actually lives in the module-level `_bus` global (see
    get_tick_bus()) — TickBus has no `_instance` class attribute at all.
    Resetting `TickBus._instance` (what this fixture did before 2026-08-27)
    silently did nothing: it just set an unused attribute on the class, so
    every test in this file was sharing ONE real TickBus instance the whole
    time. That only became visible once a test that touches `_loop` actually
    ran (closing that loop leaked a "loop is closed" error into every test
    after it) — the previous, entirely-synchronous-call version of this file
    never triggered it because nothing here ever really executed.
    """
    tick_bus_module._bus = None


def _make_mock_redis() -> MagicMock:
    """Fake `redis.asyncio.Redis` — every I/O method must be awaitable."""
    mock = MagicMock()
    mock.xadd = AsyncMock(return_value=b"1234567890-0")
    mock.xread = AsyncMock(return_value=[])
    mock.xgroup_create = AsyncMock(return_value=True)
    mock.xdel = AsyncMock(return_value=0)
    mock.delete = AsyncMock(return_value=1)
    mock.ping = AsyncMock(return_value=True)
    mock.aclose = AsyncMock(return_value=None)
    return mock


@pytest.fixture()
def fake_redis() -> MagicMock:
    return _make_mock_redis()


@pytest.fixture()
def tick_bus(fake_redis: MagicMock) -> TickBus:
    """TickBus wired to a fake Redis client, bypassing start()'s real connect."""
    bus = TickBus.get_instance()
    bus._pub = fake_redis
    bus._running = True
    return bus


# ------------------------------------------------------------------
# Tick v2 helpers
# ------------------------------------------------------------------

def _make_tick(
    symbol: str = "NIFTY",
    token: str = "25626",
    exchange: str = "NFO",
    last_price: float = 22000.0,
    **kwargs: Any,
) -> Tick:
    base = {
        "symbol": symbol,
        "token": token,
        "exchange": exchange,
        "last_price": last_price,
        "open": 21950.0,
        "high": 22050.0,
        "low": 21900.0,
        "close": 21980.0,
        "volume": 1000000,
        "timestamp": datetime.now(timezone.utc),
    }
    base.update(kwargs)
    return Tick(**base)


# ------------------------------------------------------------------
# Tick v2 factory tests
# ------------------------------------------------------------------

class TestMakeTickV2:

    def test_normal_tick_becomes_tick_v2(self) -> None:
        tick = _make_tick(symbol="BANKNIFTY", token="26682", exchange="NFO")
        v2 = make_tick_v2(tick)
        assert v2 is not None
        assert v2.symbol_id == "BANKNIFTY"
        assert v2.correlation_id == "26682"  # token maps to correlation_id, not a symbol/token field
        assert v2.segment == "NSE_FO"        # NFO -> NSE_FO per make_tick_v2's segment_map
        assert float(v2.price) == 22000.0
        assert v2.bid is None  # Angel mode-3 doesn't provide bid/ask
        assert v2.ask is None

    def test_none_tick_returns_none(self) -> None:
        assert make_tick_v2(None) is None

    def test_tick_with_depth_becomes_tick_v2_with_depth(self) -> None:
        tick = _make_tick(
            symbol="RELIANCE",
            token="2348",
            exchange="NSE",
            bid=2450.0,
            ask=2450.5,
            bid_qty=100,
            ask_qty=200,
        )
        v2 = make_tick_v2(tick)
        assert v2 is not None
        assert float(v2.bid) == 2450.0
        assert float(v2.ask) == 2450.5
        assert v2.bid_qty == 100
        assert v2.ask_qty == 200

    def test_zero_price_tick_returns_none(self) -> None:
        tick = _make_tick(last_price=0.0)
        assert make_tick_v2(tick) is None

    def test_timestamp_is_utc(self) -> None:
        tick = _make_tick()
        v2 = make_tick_v2(tick)
        assert v2 is not None
        assert v2.timestamp.tzinfo is not None


# ------------------------------------------------------------------
# TickBus unit tests (fake Redis)
# ------------------------------------------------------------------

class TestTickBus:

    def test_publish_tick_writes_to_redis(self, tick_bus: TickBus, fake_redis: MagicMock) -> None:
        tick = _make_tick()
        v2 = make_tick_v2(tick)
        assert v2 is not None
        _run(tick_bus.publish_tick(v2))
        assert fake_redis.xadd.called

    def test_publish_tick_uses_correct_stream(self, tick_bus: TickBus, fake_redis: MagicMock) -> None:
        tick = _make_tick(symbol="NIFTY", exchange="NFO")
        v2 = make_tick_v2(tick)
        assert v2 is not None
        _run(tick_bus.publish_tick(v2))
        call_args = fake_redis.xadd.call_args
        assert call_args is not None
        stream_name = call_args[0][0]
        # Real format is "<TICK_STREAM>:<mapped segment>" — NFO maps to NSE_FO.
        assert stream_name == "tick.events:NSE_FO"

    def test_subscribe_returns_a_queue_and_creates_consumer_groups(
        self, tick_bus: TickBus, fake_redis: MagicMock,
    ) -> None:
        queue = _run(tick_bus.subscribe("bar_builder", segments=["NSE_FO"]))
        assert isinstance(queue, asyncio.Queue)
        assert tick_bus._subs["bar_builder"] is queue
        fake_redis.xgroup_create.assert_called_once()
        args, kwargs = fake_redis.xgroup_create.call_args
        assert args[0] == "tick.events:NSE_FO"
        assert args[1] == "bar_builder"

    def test_subscribe_before_start_raises(self) -> None:
        bus = TickBus.get_instance()  # _pub is None — never started
        with pytest.raises(TickBusNotStartedError):
            _run(bus.subscribe("bar_builder"))

    def test_unsubscribe_removes_consumer_group(self, tick_bus: TickBus) -> None:
        _run(tick_bus.subscribe("bar_builder", segments=["NSE_FO"]))
        assert "bar_builder" in tick_bus._subs
        _run(tick_bus.unsubscribe("bar_builder"))
        assert "bar_builder" not in tick_bus._subs

    def test_multiple_consumer_groups_get_independent_queues(self, tick_bus: TickBus) -> None:
        q1 = _run(tick_bus.subscribe("bar_builder", segments=["NSE_FO"]))
        q2 = _run(tick_bus.subscribe("strategy_engine", segments=["NSE_FO"]))
        assert q1 is not q2
        assert set(tick_bus._subs.keys()) == {"bar_builder", "strategy_engine"}

    def test_xadd_failure_raises_tick_bus_error(self, tick_bus: TickBus, fake_redis: MagicMock) -> None:
        fake_redis.xadd = AsyncMock(side_effect=Exception("Redis connection lost"))
        tick = _make_tick()
        v2 = make_tick_v2(tick)
        assert v2 is not None
        with pytest.raises(TickBusError):
            _run(tick_bus.publish_tick(v2))

    def test_publish_tick_threadsafe_noops_without_pub_or_loop(self) -> None:
        """Honest no-op (not a dangling unawaited coroutine) when the bus was
        never start()ed — this was the actual production bug: LiveTickFeed
        calls this from a plain background thread, and every real deployment
        never calls TickBus.start() at all, so _pub/_loop are always None."""
        bus = TickBus.get_instance()
        tick = make_tick_v2(_make_tick())
        with patch("trading_platform.streaming.tick_bus.asyncio.run_coroutine_threadsafe") as scheduled:
            bus.publish_tick_threadsafe(tick)
        scheduled.assert_not_called()

    def test_publish_tick_threadsafe_schedules_onto_captured_loop(
        self, tick_bus: TickBus,
    ) -> None:
        tick_bus._loop = MagicMock()
        tick = make_tick_v2(_make_tick())
        with patch("trading_platform.streaming.tick_bus.asyncio.run_coroutine_threadsafe") as scheduled:
            tick_bus.publish_tick_threadsafe(tick)
        scheduled.assert_called_once()
        assert scheduled.call_args[0][1] is tick_bus._loop


# ------------------------------------------------------------------
# LiveTickFeed x TickBus integration
# ------------------------------------------------------------------

class TestLiveTickFeedTickBusIntegration:

    def test_inject_tick_publishes_via_threadsafe_bridge(
        self, tick_bus: TickBus, fake_redis: MagicMock,
    ) -> None:
        """inject_tick -> publish_tick_threadsafe -> real publish_tick, once
        the bus has a loop to schedule onto (as it would in a running app)."""
        tick_bus._loop = asyncio.get_event_loop_policy().new_event_loop()
        try:
            feed = LiveTickFeed(MagicMock())
            tick = _make_tick(symbol="NIFTY", token="25626", exchange="NFO")
            feed.inject_tick(tick)
            # publish_tick_threadsafe hands the coroutine to the other loop via
            # call_soon_threadsafe; run it one turn so the scheduled callback fires.
            tick_bus._loop.run_until_complete(asyncio.sleep(0.05))
            assert fake_redis.xadd.called
        finally:
            tick_bus._loop.close()

    def test_inject_tick_updates_last_ticks(self) -> None:
        feed = LiveTickFeed(MagicMock())
        tick = _make_tick(symbol="BANKNIFTY", token="26682", exchange="NFO")
        feed.inject_tick(tick)
        latest = feed.latest_tick("BANKNIFTY")
        assert latest is not None
        assert latest.last_price == 22000.0

    def test_inject_tick_updates_staleness_tracker(self) -> None:
        feed = LiveTickFeed(MagicMock())
        tick = _make_tick(symbol="FINNIFTY", token="25638", exchange="NFO")
        feed.inject_tick(tick)
        status = feed.staleness_tracker.status("FINNIFTY")
        assert status.age_seconds is not None and status.age_seconds >= 0
        assert status.is_stale is False

    def test_snapshot_includes_staleness(self) -> None:
        feed = LiveTickFeed(MagicMock())
        tick = _make_tick(symbol="SENSEX", token="1", exchange="BFO")
        feed.inject_tick(tick)
        snap = feed.snapshot()
        assert "staleness" in snap
        assert "shards" in snap
        assert "running" in snap

    def test_multiple_handlers_all_receive_injected_tick(self) -> None:
        feed = LiveTickFeed(MagicMock())
        received: list[Tick] = []

        def handler1(tick: Tick) -> None:
            received.append(tick)

        def handler2(tick: Tick) -> None:
            received.append(tick)

        feed.add_handler(handler1)
        feed.add_handler(handler2)
        feed.inject_tick(_make_tick(symbol="TEST", token="999", exchange="NSE"))

        assert len(received) == 2


# ------------------------------------------------------------------
# Staleness tracker integration
# ------------------------------------------------------------------

class TestFeedStalenessTracker:

    def test_record_updates_last_tick_time(self) -> None:
        tracker = FeedStalenessTracker(hard_seconds=30.0, warn_seconds=30.0)
        now = datetime.now(timezone.utc)
        tracker.record("NIFTY", now)
        assert tracker.last_tick_at("NIFTY") == now

    def test_snapshot_returns_no_tracked_symbols_for_unknown_symbol(self) -> None:
        tracker = FeedStalenessTracker(hard_seconds=30.0, warn_seconds=30.0)
        snap = tracker.snapshot(["UNKNOWN_SYMBOL"])
        # Never-ticked symbols are still reported (as stale-by-default), just
        # with no recorded tick time — real API reports presence, not absence.
        assert snap["tracked_symbols"] == 1
        assert "UNKNOWN_SYMBOL" in snap["stale_symbols"]

    def test_stale_flag_true_when_timeout_exceeded(self) -> None:
        tracker = FeedStalenessTracker(hard_seconds=1.0, warn_seconds=1.0)
        old_time = datetime.now(timezone.utc)
        tracker.record("NIFTY", old_time)
        time.sleep(1.1)  # Exceed timeout
        assert tracker.is_stale("NIFTY") is True

    def test_stale_flag_false_when_within_timeout(self) -> None:
        tracker = FeedStalenessTracker(hard_seconds=300.0, warn_seconds=300.0)
        now = datetime.now(timezone.utc)
        tracker.record("NIFTY", now)
        assert tracker.is_stale("NIFTY") is False

    def test_gate_fails_when_any_subscribed_symbol_stale(self) -> None:
        tracker = FeedStalenessTracker(hard_seconds=1.0, warn_seconds=1.0)
        old_time = datetime.now(timezone.utc)
        tracker.record("NIFTY", old_time)
        tracker.record("BANKNIFTY", old_time)
        time.sleep(1.1)
        result = tracker.gate(["NIFTY", "BANKNIFTY"], feed_running=True)
        assert result.passed is False

    def test_gate_passes_when_all_subscribed_symbols_fresh(self) -> None:
        tracker = FeedStalenessTracker(hard_seconds=300.0, warn_seconds=300.0)
        now = datetime.now(timezone.utc)
        tracker.record("NIFTY", now)
        tracker.record("BANKNIFTY", now)
        result = tracker.gate(["NIFTY", "BANKNIFTY"], feed_running=True)
        assert result.passed is True

    def test_gate_fails_when_feed_not_running(self) -> None:
        tracker = FeedStalenessTracker(hard_seconds=300.0, warn_seconds=300.0)
        tracker.record("NIFTY", datetime.now(timezone.utc))
        result = tracker.gate(["NIFTY"], feed_running=False)
        assert result.passed is False
        assert result.reason == "feed_not_running"


# ------------------------------------------------------------------
# Cross-stream: NSE vs NFO streams
# ------------------------------------------------------------------

class TestStreamSeparation:

    def test_different_exchanges_go_to_different_streams(
        self, tick_bus: TickBus, fake_redis: MagicMock,
    ) -> None:
        """NSE ticks -> tick.events:NSE_CM, NFO ticks -> tick.events:NSE_FO."""
        nse_tick = _make_tick(symbol="RELIANCE", exchange="NSE")
        nfo_tick = _make_tick(symbol="NIFTY", exchange="NFO")

        v2_nse = make_tick_v2(nse_tick)
        v2_nfo = make_tick_v2(nfo_tick)

        assert v2_nse is not None and v2_nfo is not None
        _run(tick_bus.publish_tick(v2_nse))
        _run(tick_bus.publish_tick(v2_nfo))

        calls = fake_redis.xadd.call_args_list
        assert len(calls) == 2
        streams = {c[0][0] for c in calls}
        assert "tick.events:NSE_CM" in streams
        assert "tick.events:NSE_FO" in streams


if __name__ == "__main__":
    import unittest
    unittest.main()
