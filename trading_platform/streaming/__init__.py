"""
Streaming package — Redis Streams tick bus and consumers.

Per REDESIGN_PROMPT.md §3.0 / §3.2: decouple market-data ingestion from
consumption via Redis Streams. Every normalized TickV2 is published to
Redis Streams. Strategies, bar-builder, staleness monitor, and UI WS
gateway become independent consumer groups.
"""

from trading_platform.streaming.tick_bus import (
    TickBus,
    TickBusError,
    TickBusConnectionError,
    TickBusNotStartedError,
    get_tick_bus,
    reset_tick_bus,
    TICK_STREAM,
    DEPTH_STREAM,
    TICK_GROUP_BASE,
)

__all__ = [
    "TickBus",
    "TickBusError",
    "TickBusConnectionError",
    "TickBusNotStartedError",
    "get_tick_bus",
    "reset_tick_bus",
    "TICK_STREAM",
    "DEPTH_STREAM",
    "TICK_GROUP_BASE",
]