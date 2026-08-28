"""
Redis Streams Tick Bus — decouples market-data ingestion from consumption.

Every normalized TickV2 (and DepthSnapshot) is published to Redis Streams.
Strategies, bar-builder, staleness monitor, and UI WS gateway become
independent consumer groups — nothing downstream knows or cares which
source adapter is live (REDESIGN_PROMPT.md §3.0 / §3.2).

Architecture:
    Angel One WS sockets ──┐
    Upstox WS socket   ──├─► normalize → TickV2 → Redis Streams
    TrueData WS      ──┘                          │
                              ┌────────────────────┼────────────────────┐
                              ▼                    ▼                     ▼
                         bar builder        strategy engine        UI WS gateway
                        (→ Timescale)      (Feast online feats)  (per-tenant fan-out)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from decimal import Decimal
from typing import Any, Optional, Sequence

import redis.asyncio as redis

from trading_platform.data.tick_v2 import TickV2, DepthSnapshot, FeedSource
from trading_platform.data.depth import TickDepthV2

logger = logging.getLogger(__name__)


class TickBusError(Exception):
    """Base exception for TickBus errors."""


class TickBusConnectionError(TickBusError):
    """Raised when Redis connection fails."""


class TickBusNotStartedError(TickBusError):
    """Raised when operations are attempted before start()."""


# Redis Streams keys
TICK_STREAM: str = "tick.events"           # all ticks (segment-partitioned via key)
DEPTH_STREAM: str = "depth.events"         # depth snapshots (per-symbol)
TICK_GROUP_BASE: str = "tick_consumer"     # consumer group prefix


class TickBus:
    """
    Redis Streams tick bus.

    Publishers (adapters): publish TickV2 → `tick.<segment>`.
    Consumers: subscribe via `subscribe()` → get typed TickV2.

    All ticks flow through Redis — the feed is a service, not a library.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/2",
        max_stream_len: int = 100_000,
    ) -> None:
        self._redis_url = redis_url
        self._max_len = max_stream_len
        self._pub: Optional[redis.Redis] = None
        self._subs: dict[str, asyncio.Queue[TickV2]] = {}  # group → queue
        self._depth_subs: dict[str, asyncio.Queue[DepthSnapshot]] = {}
        self._running = False
        self._dispatcher: Optional[asyncio.Task] = None
        # Captured in start() so synchronous callers (LiveTickFeed runs its tick
        # processing on a plain background thread, not this event loop) have a
        # loop to schedule onto. See publish_tick_threadsafe().
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # --- Publishing (called by adapters) ---

    async def publish_tick(self, tick: TickV2) -> None:
        """Publish a single TickV2 to `tick.<segment>` stream."""
        if self._pub is None:
            logger.error("TickBus not started — dropping tick for %s", tick.symbol_id)
            return
        stream = f"{TICK_STREAM}:{tick.segment}"
        raw = json.dumps({
            "symbol": tick.symbol_id,
            "segment": tick.segment,
            "price": float(tick.price) if tick.price is not None else None,
            "qty": tick.qty,
            "timestamp": tick.timestamp.isoformat() if hasattr(tick.timestamp, "isoformat") else str(tick.timestamp),
            "source": tick.source.value if hasattr(tick.source, "value") else str(tick.source),
            "bid": float(tick.bid) if tick.bid is not None else None,
            "ask": float(tick.ask) if tick.ask is not None else None,
            "bid_qty": tick.bid_qty,
            "ask_qty": tick.ask_qty,
            "depth": tick.depth.to_dict() if tick.depth else None,
        })
        try:
            await self._pub.xadd(stream, {"data": raw}, maxlen=self._max_len, approximate=True)
        except Exception as exc:
            raise TickBusError(f"publish_tick failed for {tick.symbol_id}: {exc}") from exc

    def publish_tick_threadsafe(self, tick: TickV2) -> None:
        """Fire-and-forget publish from a plain synchronous thread.

        LiveTickFeed processes ticks on a background `threading.Thread`, not
        this bus's event loop, so calling the coroutine `publish_tick`
        directly from there just builds a coroutine object that nothing ever
        awaits — it silently never runs (confirmed 2026-08-27: the tick bus
        had been "publishing" nothing since inception, with only a
        RuntimeWarning to show for it). Honestly no-ops when the bus was
        never started, instead of manufacturing a dangling coroutine.
        """
        if self._pub is None or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.publish_tick(tick), self._loop)

    async def publish_depth(self, depth: DepthSnapshot) -> None:
        """Publish a DepthSnapshot to `depth.<symbol>` stream."""
        if self._pub is None:
            return
        stream = f"{DEPTH_STREAM}:{depth.symbol}"
        raw = json.dumps(depth.to_dict())
        await self._pub.xadd(stream, {"data": raw}, maxlen=self._max_len, approximate=True)

    async def publish_ticks(self, ticks: Sequence[TickV2]) -> None:
        """Batch-publish ticks (single Redis round-trip)."""
        if not ticks or self._pub is None:
            return
        pipeline = self._pub.pipeline(transaction=False)
        for tick in ticks:
            stream = f"{TICK_STREAM}:{tick.segment}"
            pipeline.xadd(stream, {"data": json.dumps({
                "symbol": tick.symbol_id,
                "segment": tick.segment,
                "price": float(tick.price) if tick.price is not None else None,
                "qty": tick.qty,
                "timestamp": tick.timestamp.isoformat() if hasattr(tick.timestamp, "isoformat") else str(tick.timestamp),
                "source": tick.source.value if hasattr(tick.source, "value") else str(tick.source),
                "bid": float(tick.bid) if tick.bid is not None else None,
                "ask": float(tick.ask) if tick.ask is not None else None,
                "bid_qty": tick.bid_qty,
                "ask_qty": tick.ask_qty,
                "depth": tick.depth.to_dict() if tick.depth else None,
            })}, maxlen=self._max_len, approximate=True)
        try:
            await pipeline.execute()
        except Exception as exc:
            raise TickBusError(f"publish_ticks failed for {len(ticks)} ticks: {exc}") from exc

    # --- Subscription lifecycle ---

    async def subscribe(
        self,
        group_name: str,
        segments: Optional[Sequence[str]] = None,
    ) -> asyncio.Queue[TickV2]:
        """
        Create a consumer group and return its queue.

        Args:
            group_name: unique consumer group (e.g. "bar_builder", "strategy_engine").
            segments: if None, subscribe to ALL segments; otherwise specific ones.
        """
        if self._pub is None:
            raise TickBusNotStartedError("TickBus not started — call start() first")

        queue: asyncio.Queue[TickV2] = asyncio.Queue(maxsize=50_000)
        self._subs[group_name] = queue

        # Create Redis Streams consumer group (idempotent via XGROUP CREATE ... MKSTREAM)
        for seg in (segments or ["NSE_FO", "NSE_CM", "BSE_FO"]):
            stream = f"{TICK_STREAM}:{seg}"
            try:
                await self._pub.xgroup_create(stream, group_name, id="0", mkstream=True)
            except redis.ResponseError as e:
                if "BUSYGROUP" in str(e):
                    pass  # group already exists
                else:
                    raise

        logger.info("TickBus subscriber '%s' connected (segments=%s)", group_name, segments)
        return queue

    async def unsubscribe(self, group_name: str) -> None:
        """Remove a consumer group."""
        if group_name not in self._subs:
            return
        del self._subs[group_name]
        # TBD: XGROUP DELGROUP per segment — skip for now; Redis cleanup on restart is fine.
        logger.info("TickBus subscriber '%s' disconnected", group_name)

    # --- Start / Stop ---

    async def start(self) -> None:
        """Connect to Redis and start dispatching."""
        self._loop = asyncio.get_running_loop()
        self._pub = redis.from_url(self._redis_url, decode_responses=True)
        try:
            await self._pub.ping()
        except redis.ConnectionError:
            logger.error("Cannot connect to Redis at %s", self._redis_url)
            raise
        self._running = True
        self._dispatcher = asyncio.create_task(self._dispatch_loop())
        logger.info("TickBus started (redis=%s)", self._redis_url)

    async def stop(self) -> None:
        """Shut down dispatcher and close Redis connection."""
        self._running = False
        if self._dispatcher:
            self._dispatcher.cancel()
            try:
                await self._dispatcher
            except asyncio.CancelledError:
                pass
        if self._pub:
            await self._pub.aclose()
        self._subs.clear()
        self._depth_subs.clear()
        self._loop = None
        logger.info("TickBus stopped")

    # --- Internal dispatch loop ---

    async def _dispatch_loop(self) -> None:
        """
        Poll all tick streams and fan out to consumer groups.
        Runs indefinitely while self._running is True.
        """
        import time
        while self._running:
            try:
                # Read from all active streams (non-blocking)
                streams_list: list[tuple] = []
                for seg in ("NSE_FO", "NSE_CM", "BSE_FO"):  # TBD: dynamic segments
                    stream = f"{TICK_STREAM}:{seg}"
                    if self._subs:
                        streams_list.append((stream, self._subs))
                if not streams_list:
                    await asyncio.sleep(0.05)  # no subscribers — sleep
                    continue

                # Multi-stream XREAD (non-blocking with COUNT=10)
                # Simplified: read from each stream independently
                for seg in ("NSE_FO", "NSE_CM", "BSE_FO"):
                    stream = f"{TICK_STREAM}:{seg}"
                    for group_name, queue in list(self._subs.items()):
                        try:
                            entries = await self._pub.xread(
                                {stream: "0"},  # "0" = read all pending + new
                                count=50,
                                block=50,  # 50ms poll interval
                            )
                            for _, stream_entries in entries:
                                for entry_id, fields in stream_entries:
                                    data = json.loads(fields["data"])
                                    # Parse timestamp: ISO string → datetime
                                    _ts = data["timestamp"]
                                    if isinstance(_ts, str):
                                        from datetime import datetime, timezone
                                        try:
                                            _ts = datetime.fromisoformat(_ts)
                                        except (ValueError, TypeError):
                                            _ts = datetime.now(timezone.utc)
                                    tick = TickV2(
                                        symbol_id=data["symbol"],
                                        segment=data["segment"],
                                        price=Decimal(str(data["price"])) if data.get("price") is not None else None,
                                        qty=data.get("qty") or 0,
                                        timestamp=_ts,
                                        source=FeedSource(data["source"]) if data.get("source") else FeedSource.UNKNOWN,
                                        bid=Decimal(str(data["bid"])) if data.get("bid") is not None else None,
                                        ask=Decimal(str(data["ask"])) if data.get("ask") is not None else None,
                                        bid_qty=data.get("bid_qty", 0),
                                        ask_qty=data.get("ask_qty", 0),
                                        depth=DepthSnapshot.from_dict(data["depth"]) if data.get("depth") else None,
                                    )
                                    await queue.put(tick)
                        except redis.ResponseError as e:
                            if "NOGROUP" in str(e):
                                # Group was deleted; skip
                                continue
                            logger.warning("TickBus xread error: %s", e)
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.error("TickBus dispatch error: %s", e)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("TickBus dispatch loop error: %s", e)
                await asyncio.sleep(1.0)

    # --- Singleton (module-level get_tick_bus also exists) ---

    @classmethod
    def get_instance(cls) -> "TickBus":
        """Get or create the module-level singleton (classmethod alias for compatibility)."""
        return get_tick_bus()

    # --- Health ---

    async def health(self) -> dict[str, Any]:
        if not self._pub:
            return {"healthy": False, "error": "not connected"}
        try:
            await self._pub.ping()
            return {
                "healthy": True,
                "redis_url": self._redis_url,
                "subscribers": len(self._subs),
                "sub_queue_sizes": {g: q.qsize() for g, q in self._subs.items()},
            }
        except redis.ConnectionError:
            return {"healthy": False, "error": "redis disconnected"}


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_bus: Optional[TickBus] = None


def get_tick_bus() -> TickBus:
    """Get or create the module-level tick bus singleton."""
    global _bus
    if _bus is None:
        _bus = TickBus()
    return _bus


def reset_tick_bus() -> None:
    """For testing only."""
    global _bus
    if _bus is not None:
        asyncio.create_task(_bus.stop())
    _bus = None