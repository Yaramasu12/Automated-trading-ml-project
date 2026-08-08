"""Integration tests for the Redis Streams tick bus + LiveTickFeed integration.

These tests verify that:
1. The TickBus publishes and subscribes correctly
2. LiveTickFeed's inject_tick flows through the tick bus
3. Tick v2 normalization works correctly
4. Multiple consumer groups can subscribe independently
5. Staleness tracking integrates with the bus
"""

from __future__ import annotations

import json
import time
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trading_platform.data.tick_v2 import TickV2, make_tick_v2
from trading_platform.streaming.tick_bus import TickBus, TickBusError
from trading_platform.data.live_feed import LiveTickFeed, Tick
from trading_platform.data.feed_staleness import FeedStalenessTracker


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_tick_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure TickBus singleton is clean for every test."""
    TickBus._instance = None


def _make_mock_redis() -> MagicMock:
    """Create a mock Redis client with xadd/xread/xdel that record calls."""
    mock = MagicMock()
    mock.xadd = MagicMock(return_value=b"1234567890-0")
    mock.xread = MagicMock(return_value=[])
    mock.xdel = MagicMock(return_value=0)
    mock.delete = MagicMock(return_value=1)
    mock.ping = MagicMock(return_value=True)
    mock.info = MagicMock(return_value={"stream": {"running": True}})
    return mock


@pytest.fixture()
def fake_redis() -> MagicMock:
    return _make_mock_redis()


@pytest.fixture()
def tick_bus(fake_redis: MagicMock) -> TickBus:
    """TickBus wired to a fake Redis client."""
    with patch("trading_platform.streaming.tick_bus.redis.Redis", return_value=fake_redis):
        bus = TickBus.get_instance()
        bus._redis = fake_redis
        bus._running = True
        yield bus
        bus.stop()


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
        assert v2.symbol == "BANKNIFTY"
        assert v2.token == "26682"
        assert v2.exchange_segment == "NFO"
        assert v2.last_price == 22000.0
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
        assert v2.bid == 2450.0
        assert v2.ask == 2450.5
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
        tick_bus.publish_tick(v2)
        assert fake_redis.xadd.called

    def test_publish_tick_uses_correct_stream(self, tick_bus: TickBus, fake_redis: MagicMock) -> None:
        tick = _make_tick(symbol="NIFTY", exchange="NFO")
        v2 = make_tick_v2(tick)
        assert v2 is not None
        tick_bus.publish_tick(v2)
        call_args = fake_redis.xadd.call_args
        assert call_args is not None
        stream_name = call_args[0][0]
        assert stream_name == b"tick.NFO"

    def test_subscribe_returns_consumer(self, tick_bus: TickBus) -> None:
        def handler(tick_v2: TickV2) -> None:
            pass
        consumer = tick_bus.subscribe("NFO", handler)
        assert consumer is not None
        assert consumer.group_name == "group-NFO"

    def test_unsubscribe_stops_consumer(self, tick_bus: TickBus) -> None:
        received: list[TickV2] = []

        def handler(tick_v2: TickV2) -> None:
            received.append(tick_v2)

        consumer = tick_bus.subscribe("NFO", handler)
        assert consumer is not None
        tick_bus.unsubscribe(consumer)

        # Publish after unsubscribe — handler should not receive
        tick = _make_tick(symbol="NIFTY", exchange="NFO")
        v2 = make_tick_v2(tick)
        assert v2 is not None
        tick_bus.publish_tick(v2)

        # Give the consumer thread a moment (it should be stopped)
        time.sleep(0.1)
        assert len(received) == 0

    def test_multiple_consumers_on_same_stream(self, tick_bus: TickBus) -> None:
        received_a: list[TickV2] = []
        received_b: list[TickV2] = []

        def handler_a(tick_v2: TickV2) -> None:
            received_a.append(tick_v2)

        def handler_b(tick_v2: TickV2) -> None:
            received_b.append(tick_v2)

        c1 = tick_bus.subscribe("NFO", handler_a)
        c2 = tick_bus.subscribe("NFO", handler_b)
        assert c1 is not None and c2 is not None

        tick = _make_tick(symbol="NIFTY", exchange="NFO")
        v2 = make_tick_v2(tick)
        assert v2 is not None
        tick_bus.publish_tick(v2)

        time.sleep(0.2)
        assert len(received_a) >= 1
        assert len(received_b) >= 1

    def test_xadd_failure_raises_tick_bus_error(self, tick_bus: TickBus, fake_redis: MagicMock) -> None:
        fake_redis.xadd = MagicMock(side_effect=Exception("Redis connection lost"))
        tick = _make_tick()
        v2 = make_tick_v2(tick)
        assert v2 is not None
        with pytest.raises(TickBusError):
            tick_bus.publish_tick(v2)


# ------------------------------------------------------------------
# LiveTickFeed × TickBus integration
# ------------------------------------------------------------------

class TestLiveTickFeedTickBusIntegration:

    def test_inject_tick_flows_through_tick_bus(self, tick_bus: TickBus) -> None:
        """inject_tick → tick bus should publish TickV2."""
        received: list[TickV2] = []

        def handler(tick_v2: TickV2) -> None:
            received.append(tick_v2)

        consumer = tick_bus.subscribe("NFO", handler)
        assert consumer is not None

        feed = LiveTickFeed(MagicMock())
        tick = _make_tick(symbol="NIFTY", token="25626", exchange="NFO")
        feed.inject_tick(tick)

        time.sleep(0.2)
        assert len(received) >= 1
        assert received[0].symbol == "NIFTY"

        tick_bus.unsubscribe(consumer)

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
        staleness = feed.staleness_tracker.snapshot(["FINNIFTY"])
        assert "FINNIFTY" in staleness
        assert staleness["FINNIFTY"]["age_seconds"] >= 0
        assert staleness["FINNIFTY"]["stale"] is False

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
        tracker = FeedStalenessTracker(timeout_seconds=30.0)
        now = datetime.now(timezone.utc)
        tracker.record("NIFTY", now)
        snap = tracker.snapshot(["NIFTY"])
        assert snap["NIFTY"]["last_tick_time"] == now

    def test_snapshot_returns_empty_for_unknown_symbol(self) -> None:
        tracker = FeedStalenessTracker(timeout_seconds=30.0)
        snap = tracker.snapshot(["UNKNOWN_SYMBOL"])
        assert len(snap) == 0

    def test_stale_flag_true_when_timeout_exceeded(self) -> None:
        tracker = FeedStalenessTracker(timeout_seconds=1.0)
        old_time = datetime.now(timezone.utc)
        tracker.record("NIFTY", old_time)
        time.sleep(1.1)  # Exceed timeout
        snap = tracker.snapshot(["NIFTY"])
        assert snap["NIFTY"]["stale"] is True

    def test_stale_flag_false_when_within_timeout(self) -> None:
        tracker = FeedStalenessTracker(timeout_seconds=300.0)
        now = datetime.now(timezone.utc)
        tracker.record("NIFTY", now)
        snap = tracker.snapshot(["NIFTY"])
        assert snap["NIFTY"]["stale"] is False

    def test_is_ready_returns_false_when_any_stale(self) -> None:
        tracker = FeedStalenessTracker(timeout_seconds=1.0)
        old_time = datetime.now(timezone.utc)
        tracker.record("NIFTY", old_time)
        tracker.record("BANKNIFTY", old_time)
        time.sleep(1.1)
        assert tracker.is_ready(["NIFTY", "BANKNIFTY"]) is False

    def test_is_ready_returns_true_when_all_fresh(self) -> None:
        tracker = FeedStalenessTracker(timeout_seconds=300.0)
        now = datetime.now(timezone.utc)
        tracker.record("NIFTY", now)
        tracker.record("BANKNIFTY", now)
        assert tracker.is_ready(["NIFTY", "BANKNIFTY"]) is True


# ------------------------------------------------------------------
# Cross-stream: NSE vs NFO streams
# ------------------------------------------------------------------

class TestStreamSeparation:

    def test_different_exchanges_go_to_different_streams(
        self, tick_bus: TickBus, fake_redis: MagicMock
    ) -> None:
        """NSE ticks → tick.NSE, NFO ticks → tick.NFO."""
        tick_bus._running = True

        nse_tick = _make_tick(symbol="RELIANCE", exchange="NSE")
        nfo_tick = _make_tick(symbol="NIFTY", exchange="NFO")

        v2_nse = make_tick_v2(nse_tick)
        v2_nfo = make_tick_v2(nfo_tick)

        assert v2_nse is not None and v2_nfo is not None
        tick_bus.publish_tick(v2_nse)
        tick_bus.publish_tick(v2_nfo)

        calls = fake_redis.xadd.call_args_list
        assert len(calls) == 2
        streams = {c[0][0] for c in calls}
        assert b"tick.NSE" in streams
        assert b"tick.NFO" in streams