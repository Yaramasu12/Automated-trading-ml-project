"""
MarketDataAdapter — abstract interface for all market-data sources.

Every source (Angel One, Upstox, Simulated, Replay) implements this
contract. Strategies, bar-builder, and UI never know or care which
source is live. (TrueData was evaluated and abandoned — see
docs/redesign-prompt-status memory — its vendor-neutral symbol-mapping
stub below is kept only because it's harmless dead code, not a plan.)

Design: REDESIGN_PROMPT.md §3.0 / §3.2
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Optional, Sequence

from trading_platform.data.tick_v2 import TickV2, FeedSource
from trading_platform.data.depth import DepthSnapshot


class MarketDataAdapter(ABC):
    """
    Base contract for a market-data source adapter.

    Sub-classes:
        AngelOneDataAdapter — WebSocket sharded + REST fallback
        UpstoxDataAdapter   — option chain + Greeks + expired history
        SimulatedBrokerClient already has synthetic data — not a MarketDataAdapter
        ReplayDriver         — replays from Timescale tick hypertable
    """

    # --- Lifecycle ---

    @abstractmethod
    async def start(self) -> None:
        """
        Authenticate, subscribe to requested symbols, and begin
        streaming normalized TickV2 objects onto the Redis Streams
        tick bus (see `trading_platform/streaming/tick_bus.py`).
        """

    @abstractmethod
    async def stop(self) -> None:
        """Unsubscribe, close sockets, release resources."""

    # --- Symbol management ---

    @abstractmethod
    async def subscribe(self, symbols: Sequence[str], segment: str) -> None:
        """
        Subscribe to *symbols* in *segment* (NSE_FO, NSE_CM, …).

        Angel One: shards tokens across up to 3 WS connections.
        Upstox: subscribes to option-chain bands (±10% moneyness).
        TrueData: subscribes within its own symbol-plan limits.
        """

    @abstractmethod
    async def unsubscribe(self, symbols: Sequence[str]) -> None:
        """Remove subscription; source may close socket if no symbols remain."""

    # --- History / backfill ---

    @abstractmethod
    async def get_history(
        self,
        symbol: str,
        interval: str,       # "1m", "3m", "5m", "15m", "1D", …
        from_dt: Any,        # datetime or exchange timestamp str
        to_dt: Optional[Any] = None,
    ) -> Any:
        """
        Return bars as a Polars DataFrame (schema-free columns:
        `timestamp, open, high, low, close, volume`).

        Used by: bar builder gap-fill, IV-rank history, backtests.
        """

    # --- Account / portfolio data (SPLIT-SOURCE RULE §16.7) ---
    # NOTE: account data is NOT market data.  The BrokerAdapter
    # handles funds/margin/orders/fills.  This interface only
    # provides reference data that any source can supply.

    @abstractmethod
    async def get_instrument_master(self) -> dict[str, Any]:
        """
        Return the platform's internal instrument map:
        {symbol_id: {segment, symbol, expiry, strike, option_type, lot_size, …}}
        """

    @abstractmethod
    async def get_option_chain(self, underlying: str, expiry: str) -> list[dict[str, Any]]:
        """
        Full option chain for *underlying* on *expiry*:
        [{strike, option_type, ce_pv, pe_pv, ce_oi, pe_oi, ce_iv, pe_iv, …}, …]

        Upstox provides this natively; Angel One via REST;
        TrueData via its chain endpoint.
        """

    # --- Health ---

    @abstractmethod
    async def health(self) -> dict[str, Any]:
        """
        Return {healthy: bool, source: FeedSource, last_tick_ts: datetime, …}.
        Consumed by the staleness watchdog (§3.2) and UI health screens.
        """

    # --- Internal ---

    @property
    @abstractmethod
    def feed_source(self) -> FeedSource:
        """Which source this adapter represents."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """True if the underlying socket(s) are live and subscribed."""


# ---------------------------------------------------------------------------
# Symbol mapping helpers — broker/vendor symbols ↔ internal instrument IDs
# ---------------------------------------------------------------------------

class SymbolMapper:
    """
    Translates between internal symbol_id (from instrument_master)
    and each source's native symbol format.

    Angel One:  "NIFTY240500C"  (near-month + strike + C/P)
    Upstox:     "NIFTY_!NIFTY25JULFUT"  style
    TrueData:   "NIFTY-I"  for near-month future

    Every adapter owns its own mapping table — nothing upstream
    sees vendor symbols.
    """

    @staticmethod
    def angel_one_symbol(segment: str, symbol: str) -> str:
        """Angel One SmartAPI symbol format."""
        return symbol  # already in AO format from instrument_master

    @staticmethod
    def upstox_symbol(segment: str, symbol: str) -> str:
        """Upstox exchange-instrument ID format."""
        return symbol  # stored in instrument_master in Upstox format

    @staticmethod
    def truedata_symbol(segment: str, symbol: str) -> str:
        """TrueData WS symbol format (e.g. 'NIFTY-I' for near-month fut)."""
        # Full translation lives in TrueDataAdapter — this is a stub
        return symbol

    @staticmethod
    def from_truedata(symbol: str) -> str:
        """Reverse map TrueData symbol → internal symbol_id."""
        return symbol  # TBD: full map in TrueDataAdapter


def get_market_adapter(settings: Any, **kwargs: Any) -> "MarketDataAdapter":
    """
    Construct the configured MarketDataAdapter per `settings.data_source_priority`
    (Angel One / Upstox — TrueData was evaluated and abandoned, see
    docs/TRUEDATA_SETUP.md's absence and the redesign-prompt-status memory).

    `kwargs` (e.g. tick_bus, tenant_id) are accepted for forward-compat with
    callers like TenantRuntime but are not yet consumed by either adapter.
    """
    from trading_platform.data.upstox_feed import UpstoxDataAdapter

    source = settings.data_source_priority[0] if settings.data_source_priority else "angel_one"
    if source == "upstox":
        return UpstoxDataAdapter(settings)
    raise NotImplementedError(
        f"get_market_adapter: source={source!r} has no MarketDataAdapter implementation yet. "
        "Angel One's live feed (trading_platform/data/live_feed.py) is the wired-in default "
        "and does not implement this newer MarketDataAdapter interface — use LiveTickFeed directly."
    )