"""
Tick-to-Bar Builder — converts TickV2 stream into 1m/5m/15m/1h/1D bars (REDESIGN_PROMPT.md §3, §3.2).

Purpose:
- Ingests TickV2 from Redis Streams tick bus
- Builds OHLCV bars at multiple timeframes
- Pushes to TimescaleDB hypertable with continuous aggregates
- Gap backfill on reconnect (replays candle history into bars)

Design:
- One consumer group per bar interval on Redis Streams
- Uses Polars for efficient in-memory aggregation
- Publishes completed bars back to Redis Streams (bar.* topics) for downstream consumers
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional
from enum import Enum

import polars as pl

from trading_platform.config import Settings
from trading_platform.data.tick_v2 import TickV2
from trading_platform.streaming.tick_bus import TickBus, BarTopic

logger = logging.getLogger(__name__)


class BarInterval(str, Enum):
    """Supported bar intervals."""
    ONE_MIN = "1m"
    FIVE_MIN = "5m"
    FIFTEEN_MIN = "15m"
    ONE_HOUR = "1h"
    ONE_DAY = "1D"

    @property
    def seconds(self) -> int:
        """Return interval in seconds."""
        mapping = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "1h": 3600,
            "1D": 86400,
        }
        return mapping.get(self.value, 60)

    @property
    def redis_key(self) -> str:
        """Redis stream key for this interval."""
        return f"bars:{self.value}"

    @classmethod
    def from_string(cls, s: str) -> BarInterval:
        """Convert string to BarInterval."""
        mapping = {
            "1": cls.ONE_MIN,
            "1m": cls.ONE_MIN,
            "5m": cls.FIVE_MIN,
            "15m": cls.FIFTEEN_MIN,
            "60": cls.ONE_HOUR,
            "1h": cls.ONE_HOUR,
            "D": cls.ONE_DAY,
            "1D": cls.ONE_DAY,
        }
        result = mapping.get(s.lower())
        if result is None:
            raise ValueError(f"Unknown bar interval: {s}")
        return result


@dataclass
class Bar:
    """OHLCV bar representation."""
    symbol: str
    interval: str
    timestamp: int  # unix timestamp (start of bar)
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int = 0
    vwap_num: float = 0.0  # numerator for VWAP calculation
    vwap_den: float = 0.0  # denominator for VWAP calculation
    tick_count: int = 0
    segment: str = ""
    exchange: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for DB insertion."""
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "ts": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "oi": self.oi,
            "vwap": self.vwap_num / self.vwap_den if self.vwap_den > 0 else 0.0,
            "tick_count": self.tick_count,
            "segment": self.segment,
            "exchange": self.exchange,
        }

    @property
    def vwap(self) -> float:
        """Compute VWAP from accumulators."""
        return self.vwap_num / self.vwap_den if self.vwap_den > 0 else self.close


@dataclass
class BarAccumulator:
    """Accumulates ticks into a bar."""
    symbol: str
    interval: BarInterval
    timestamp: int  # start of current bar window
    open: float = 0.0
    high: float = 0.0
    low: float = float("inf")
    close: float = 0.0
    volume: int = 0
    oi: int = 0
    vwap_num: float = 0.0
    vwap_den: float = 0.0
    tick_count: int = 0
    segment: str = ""
    exchange: str = ""

    def update(self, tick: TickV2) -> None:
        """Update accumulator with a new tick."""
        # Only update if tick is within the current bar window
        if tick.timestamp > 0:
            bar_start = (tick.timestamp // self.interval.seconds) * self.interval.seconds
            if bar_start != self.timestamp:
                # Tick is in a future bar — this can happen on reconnect gap
                # We'll handle gap backfill separately
                pass

        # OHLC
        price = tick.last_price or tick.close or 0.0
        if self.open == 0.0:
            self.open = price
        self.high = max(self.high, price)
        self.low = min(self.low, price) if self.low != float("inf") else price
        self.close = price

        # Volume
        self.volume += tick.volume or 0

        # OI (use latest non-zero)
        if tick.oi > 0:
            self.oi = tick.oi

        # VWAP accumulators
        self.vwap_num += price * (tick.volume or 0)
        self.vwap_den += tick.volume or 0

        # Metadata
        self.tick_count += 1
        self.segment = tick.segment or self.segment
        self.exchange = tick.exchange or self.exchange

    def to_bar(self) -> Bar:
        """Convert accumulator to Bar."""
        return Bar(
            symbol=self.symbol,
            interval=self.interval.value,
            timestamp=self.timestamp,
            open=self.open,
            high=self.high if self.high != float("inf") else self.open,
            low=self.low if self.low != float("inf") else self.open,
            close=self.close,
            volume=self.volume,
            oi=self.oi,
            vwap_num=self.vwap_num,
            vwap_den=self.vwap_den,
            tick_count=self.tick_count,
            segment=self.segment,
            exchange=self.exchange,
        )

    def is_empty(self) -> bool:
        """Check if accumulator has any data."""
        return self.tick_count == 0


class TickBarBuilder:
    """
    Tick-to-bar builder.

    Consumes TickV2 from TickBus, builds bars at multiple intervals,
    publishes to Redis Streams and TimescaleDB.
    """

    def __init__(
        self,
        settings: Settings,
        tick_bus: TickBus,
        intervals: list[BarInterval] | None = None,
    ) -> None:
        self._settings = settings
        self._tick_bus = tick_bus
        self._intervals = intervals or [BarInterval.ONE_MIN, BarInterval.FIVE_MIN, BarInterval.FIFTEEN_MIN, BarInterval.ONE_HOUR]

        # Per-symbol, per-interval accumulators
        self._accumulators: dict[str, dict[str, BarAccumulator]] = defaultdict(dict)

        # Running bar counts (for monitoring)
        self._bar_counts: dict[str, int] = defaultdict(int)

        # Running state
        self._running = False
        self._tasks: list[asyncio.Task] = []

        # TimescaleDB writer (lazily initialized)
        self._ts_writer: Any = None

        # Register with tick bus
        self._tick_bus.register_consumer("bar_builder", self._on_tick)

    async def start(self) -> None:
        """Start the bar builder."""
        self._running = True
        logger.info("TickBarBuilder starting for intervals: %s", [i.value for i in self._intervals])

        # Initialize TimescaleDB writer if configured
        if self._settings.timescale_url:
            self._ts_writer = await self._init_ts_writer()

    async def stop(self) -> None:
        """Stop the bar builder, flush remaining bars."""
        self._running = False

        # Flush remaining accumulators
        for symbol, intervals in self._accumulators.items():
            for interval_str, acc in intervals.items():
                if not acc.is_empty():
                    bar = acc.to_bar()
                    await self._publish_bar(bar)

        # Cancel tasks
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        logger.info("TickBarBuilder stopped. Total bars: %s", dict(self._bar_counts))

    async def _on_tick(self, tick: TickV2) -> None:
        """Handle incoming tick from tick bus."""
        for interval in self._intervals:
            acc = self._get_or_create_accumulator(tick.symbol, interval)
            acc.update(tick)

            # Check if bar is complete
            if self._is_bar_complete(acc, tick):
                bar = acc.to_bar()
                await self._publish_bar(bar)
                self._bar_counts[interval.value] += 1
                self._reset_accumulator(acc, interval, tick)

    def _get_or_create_accumulator(
        self, symbol: str, interval: BarInterval
    ) -> BarAccumulator:
        """Get or create a bar accumulator for the symbol/interval."""
        if interval.value not in self._accumulators[symbol]:
            self._accumulators[symbol][interval.value] = BarAccumulator(
                symbol=symbol,
                interval=interval,
                timestamp=0,
            )
        return self._accumulators[symbol][interval.value]

    def _is_bar_complete(self, acc: BarAccumulator, tick: TickV2) -> bool:
        """Check if a new bar window has started."""
        if acc.timestamp == 0:
            # First bar — initialize to tick's bar start
            if tick.timestamp > 0:
                acc.timestamp = (tick.timestamp // acc.interval.seconds) * acc.interval.seconds
                acc.open = tick.last_price or tick.close or 0.0
                acc.high = acc.open
                acc.low = acc.open
                acc.close = acc.open
            return False

        bar_start = (tick.timestamp // acc.interval.seconds) * acc.interval.seconds
        return bar_start > acc.timestamp

    def _reset_accumulator(self, acc: BarAccumulator, interval: BarInterval, tick: TickV2) -> None:
        """Reset accumulator for new bar window."""
        acc.timestamp = (tick.timestamp // interval.seconds) * interval.seconds
        acc.open = tick.last_price or tick.close or 0.0
        acc.high = acc.open
        acc.low = acc.open
        acc.close = acc.open
        acc.volume = 0
        acc.oi = 0
        acc.vwap_num = 0.0
        acc.vwap_den = 0.0
        acc.tick_count = 1

    async def _publish_bar(self, bar: Bar) -> None:
        """Publish completed bar to Redis Streams and TimescaleDB."""
        # Publish to tick bus for downstream consumers
        await self._tick_bus.publish_bar(bar)

        # Write to TimescaleDB if connected
        if self._ts_writer:
            try:
                await self._ts_writer.write_bar(bar)
            except Exception as e:
                logger.warning("TimescaleDB write failed for bar %s %s: %s", bar.symbol, bar.interval, e)

    async def _init_ts_writer(self) -> Any:
        """Initialize TimescaleDB writer."""
        from trading_platform.data.timescale_writer import TimescaleWriter
        return TimescaleWriter(self._settings)

    async def get_bars(
        self,
        symbol: str,
        interval: BarInterval,
        start_ts: int,
        end_ts: int,
    ) -> Optional[pl.DataFrame]:
        """
        Get bars for a symbol from TimescaleDB.

        Returns Polars DataFrame or None if no data.
        """
        if not self._ts_writer:
            return None
        return await self._ts_writer.get_bars(symbol, interval, start_ts, end_ts)

    def get_accumulator_state(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Get current accumulator state (for monitoring/debug)."""
        state = {}
        for symbol, intervals in self._accumulators.items():
            state[symbol] = {}
            for interval_str, acc in intervals.items():
                if not acc.is_empty():
                    state[symbol][interval_str] = {
                        "timestamp": acc.timestamp,
                        "open": acc.open,
                        "high": acc.high if acc.high != float("inf") else acc.open,
                        "low": acc.low if acc.low != float("inf") else acc.open,
                        "close": acc.close,
                        "volume": acc.volume,
                        "tick_count": acc.tick_count,
                    }
        return state

    def get_bar_counts(self) -> dict[str, int]:
        """Get total bars built per interval."""
        return dict(self._bar_counts)


# ---------------------------------------------------------------------------
# Gap backfill helper
# ---------------------------------------------------------------------------

async def backfill_gap(
    settings: Settings,
    tick_bus: TickBus,
    symbol: str,
    start_ts: int,
    end_ts: int,
    interval: BarInterval = BarInterval.ONE_MIN,
) -> int:
    """
    Backfill a gap in the bar timeline using candle API data.

    Called on reconnect to fill holes left by WebSocket downtime.

    Args:
        symbol: internal symbol ID
        start_ts: start timestamp (unix)
        end_ts: end timestamp (unix)
        interval: bar interval

    Returns:
        Number of bars backfilled
    """
    logger.info("Backfilling gap for %s [%s - %s]", symbol, start_ts, end_ts)

    # Get candle history from the market data adapter
    # This uses the existing Angel One candle API (angel_one_history.py)
    from trading_platform.data.angel_one_adapter import create_angel_one_adapter
    from trading_platform.config import Settings as AppSettings

    adapter = create_angel_one_adapter(AppSettings())
    df = await adapter.get_history_api(
        symbol=symbol,
        interval=interval.value,
        start=start_ts,
        end=end_ts,
    )

    if df is None or len(df) == 0:
        logger.warning("No gap data for %s", symbol)
        return 0

    # Convert to bars and publish
    count = 0
    for row in df.to_dicts():
        bar = Bar(
            symbol=symbol,
            interval=interval.value,
            timestamp=int(row.get("timestamp", row.get("dt", 0))),
            open=float(row.get("open", 0)),
            high=float(row.get("high", 0)),
            low=float(row.get("low", 0)),
            close=float(row.get("close", 0)),
            volume=int(row.get("volume", 0)),
            oi=int(row.get("oi", 0)),
            segment="NSE_FO",
        )
        await tick_bus.publish_bar(bar)
        count += 1

    logger.info("Backfilled %d bars for %s", count, symbol)
    return count