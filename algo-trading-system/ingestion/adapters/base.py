"""
Base interface for market-data adapters.

All adapters (Datab Bento, Polygon, IBKR, crypto venues, mock replay)
implement this interface so the ingestion pipeline is vendor-agnostic.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterator
import uuid


class AssetClass(str, Enum):
    """Asset class enumeration."""
    EQUITY = "equity"
    ETF = "etf"
    OPTION = "option"
    FUTURE = "future"
    FXT = "fx"
    CRYPTO = "crypto"


class PriceType(str, Enum):
    """Price type for a tick."""
    TRADE = "trade"
    BID = "bid"
    ASK = "ask"


@dataclass
class Tick:
    """Canonical tick record — nanosecond timestamps throughout."""
    instrument_id: str
    venue: str
    timestamp_ns: int  # nanoseconds since epoch
    trade_time_ns: int  # exchange timestamp
    bid_price: float
    ask_price: float
    bid_size: float
    ask_size: float
    last_price: float
    last_size: float
    trade_volume: float
    tick_direction: int = 0
    exchange_timestamp_ns: int = 0
    seq_index: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class Bar:
    """OHLCV bar produced by the normalizer."""
    instrument_id: str
    venue: str
    timestamp_ns: int
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float
    num_trades: int = 0
    vwap: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class OrderBookSnapshot:
    """L2 order-book snapshot."""
    instrument_id: str
    venue: str
    timestamp_ns: int
    bids: list[tuple[float, int]] = field(default_factory=list)
    asks: list[tuple[float, int]] = field(default_factory=list)


@dataclass
class Instrument:
    """Instrument metadata."""
    instrument_id: str
    symbol: str
    asset_class: AssetClass
    venue: str
    tick_size: float
    lot_size: float
    min_price_increment: float
    multiplier: float = 1.0
    currency: str = "USD"
    unit_currency: str = "USD"
    margin_rate: float = 0.1
    description: str = ""
    metadata: dict = field(default_factory=dict)


class MarketDataAdaptor(abc.ABC):
    """
    Abstract base class for market-data adapters.

    Each adapter implements:
    - `subscribe()` — register instruments to follow
    - `ticks()` — iterator/generator of Tick
    - `bars()` — iterator of Bar for requested interval
    - `instruments()` — available instruments
    - `health_check()` — connectivity status
    """

    @abc.abstractmethod
    def subscribe(self, instruments: list[Instrument]) -> None:
        """Subscribe to the given instruments."""

    @abc.abstractmethod
    def ticks(self, instrument_ids: list[str] | None = None) -> Iterator[Tick]:
        """
        Yield Tick records.

        For live adapters this runs continuously until the connection is lost.
        For replay adapters this yields historical data and then stops.
        """

    @abc.abstractmethod
    def bars(
        self,
        instrument_ids: list[str],
        interval: str,
    ) -> Iterator[Bar]:
        """
        Yield Bar records for the given interval (e.g. '1s', '1min', '1h').
        """

    @abc.abstractmethod
    def instruments(self) -> list[Instrument]:
        """Return the list of instruments this adapter can provide data for."""

    @abc.abstractmethod
    def health_check(self) -> dict:
        """Return connectivity status for debugging/monitoring."""

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"


class ReplayMode(str, Enum):
    """Replay modes for mock/historical data."""
    SYNTHETIC = "synthetic"  # deterministic synthetic data
    HISTORICAL = "historical"  # from stored parquet/ClickHouse
    RECORDING = "recording"  # from a recorded Redpanda topic


class FeedState(str, Enum):
    """Adapter lifecycle state."""
    CREATED = "created"
    SUBSCRIBED = "subscribed"
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"