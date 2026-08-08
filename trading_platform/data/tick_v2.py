"""
Tick v2 model — the canonical tick schema for the entire platform.

Replaces the legacy Tick (LTP-only) model.  Every source adapter
(Angel One, Upstox, TrueData) normalizes to this schema before
publishing onto the Redis Streams tick bus.

See REDESIGN_PROMPT.md §3.2 for the design rationale.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from trading_platform.data.depth import DepthSnapshot  # optional, see below


class FeedSource(str, enum.Enum):
    """Which source produced this tick."""
    ANGEL_ONE = "angel_one"
    UPSTOX = "upstox"
    TRUEDATA = "truedata"
    SIMULATED = "simulated"
    REPLAY = "replay"  # deterministic replay from Timescale tick table


class TickSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class TickV2:
    """
    Canonical tick — every adapter normalizes to this schema.

    Required fields (always present):
        symbol_id: internal instrument key (matches instrument_master)
        segment: NSE_FO | NSE_CM | BSE_CM | MCX
        price: last traded price (LTP)
        qty: quantity at last price
        timestamp: UTC datetime of the tick
        source: FeedSource enum
        exchange_timestamp: exchange-provided timestamp (may be Naive)

    Optional fields (may be None if source doesn't provide):
        bid / ask: best available prices (None → mode-3 snap-quote / vendor gap)
        bid_qty / ask_qty: top-of-book quantities
        oi: open interest (F&O only)
        oi_change: change in OI vs previous snapshot
        volume: cumulative volume
        vwap: session VWAP
        depth: top-5 depth snapshot (full order book)
        trade_id: exchange trade ID (dedup)
        correlation_id: per-WS-connection UUID (debuggability)
        staleness_reason: why this tick is stale / degraded
    """

    symbol_id: str
    segment: str
    price: Decimal
    qty: int
    timestamp: Any  # datetime (aware, UTC)
    source: FeedSource

    # Best bid/ask (optional — None when source doesn't provide depth)
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    bid_qty: Optional[int] = None
    ask_qty: Optional[int] = None

    # F&O fields (optional)
    oi: Optional[int] = None
    oi_change: Optional[int] = None

    # Aggregated fields (optional)
    volume: Optional[int] = None
    vwap: Optional[Decimal] = None

    # Full depth (optional — Depth-20 socket or vendor only)
    depth: Optional[DepthSnapshot] = None

    # Metadata
    trade_id: Optional[str] = None
    correlation_id: Optional[str] = None
    staleness_reason: Optional[str] = None

    # --- helpers ---

    @property
    def mid_price(self) -> Optional[Decimal]:
        """Mid price if bid/ask available, else just price."""
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return None

    @property
    def spread_bps(self) -> Optional[float]:
        """Bid-ask spread in basis points vs mid or price."""
        mid = self.mid_price
        if mid is None or mid == 0:
            return None
        spread = self.ask - self.bid if (self.ask and self.bid) else Decimal("0")
        return float(spread * 10000 / mid)

    @property
    def is_stale(self) -> bool:
        """True if staleness_reason is set."""
        return self.staleness_reason is not None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for Redis Streams / JSON transport."""
        import json
        from dataclasses import fields as dataclass_fields

        result: dict[str, Any] = {}
        for f in dataclass_fields(self):
            val = getattr(self, f.name)
            # datetime → ISO string
            if hasattr(val, "isoformat"):
                val = val.isoformat()
            # Enum → str
            elif isinstance(val, enum.Enum):
                val = val.value
            # Decimal → float
            elif isinstance(val, Decimal):
                val = float(val)
            # depth → dict
            elif val is not None and hasattr(val, "to_dict"):
                val = val.to_dict()
            result[f.name] = val
        return result

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TickV2":
        """Deserialize from Redis Streams / JSON transport."""
        import json
        from dataclasses import fields as dataclass_fields
        from datetime import datetime, timezone

        kwargs: dict[str, Any] = {}
        for f in dataclass_fields(cls):
            val = d.get(f.name)
            if val is None:
                continue
            # ISO string → datetime
            if f.type == Any and isinstance(val, str) and "T" in val:
                try:
                    val = datetime.fromisoformat(val).astimezone(timezone.utc)
                except (ValueError, TypeError):
                    pass
            # float → Decimal
            elif f.type == Decimal and isinstance(val, (int, float)):
                val = Decimal(str(val))
            # str → enum
            elif hasattr(f.type, "value") and isinstance(val, str):
                for member in f.type:
                    if member.value == val:
                        val = member
                        break
            kwargs[f.name] = val
        return cls(**kwargs)

    def __repr__(self) -> str:
        return (
            f"TickV2(symbol={self.symbol_id}, seg={self.segment}, "
            f"p={self.price}, q={self.qty}, src={self.source.value}, "
            f"t={self.timestamp})"
        )


def make_tick_v2(tick: Any) -> Optional["TickV2"]:
    """
    Convert a legacy ``Tick`` (from live_feed) into a TickV2.

    Handles the Angel One field mapping:
        symbol  → symbol_id
        token   → correlation_id (debug)
        exchange → segment
        last_price → price
        open/high/low/close → derived OHLC for bar builder
        bid/ask/bid_qty/ask_qty → depth fields
        volume → volume

    Returns None for zero-price ticks (rejected ticks).
    """
    if tick is None:
        return None

    # Reject zero-price ticks (bad feed data)
    last_price = getattr(tick, "last_price", None)
    if last_price is not None and (isinstance(last_price, (int, float, Decimal)) and last_price == 0):
        return None

    # Extract bid/ask if available (Depth-20 or vendor only)
    bid = getattr(tick, "bid", None)
    ask = getattr(tick, "ask", None)
    bid_qty = getattr(tick, "bid_qty", None)
    ask_qty = getattr(tick, "ask_qty", None)

    # Fallback: if legacy Tick has bid/ask as separate attrs
    if bid is None:
        bid_val = getattr(tick, "bid_price", None)
        if bid_val is not None:
            bid = Decimal(str(bid_val)) if not isinstance(bid_val, Decimal) else bid_val
    if ask is None:
        ask_val = getattr(tick, "ask_price", None)
        if ask_val is not None:
            ask = Decimal(str(ask_val)) if not isinstance(ask_val, Decimal) else ask_val
    if bid_qty is None:
        bid_qty_val = getattr(tick, "bid_quantity", None)
        if bid_qty_val is not None:
            bid_qty = int(bid_qty_val)
    if ask_qty is None:
        ask_qty_val = getattr(tick, "ask_quantity", None)
        if ask_qty_val is not None:
            ask_qty = int(ask_qty_val)

    # Timestamp normalization to UTC
    ts = getattr(tick, "timestamp", None)
    if ts is not None:
        if not hasattr(ts, "tzinfo") or ts.tzinfo is None:
            # Assume UTC if naive
            from datetime import timezone
            ts = ts.replace(tzinfo=timezone.utc)

    # Exchange/segment mapping
    exchange = getattr(tick, "exchange", None)
    segment_map = {
        "NFO": "NSE_FO",
        "NSE": "NSE_CM",
        "BFO": "BSE_CM",
        "BSE": "BSE_CM",
        "MCX": "MCX",
    }
    segment = segment_map.get(exchange, exchange) if exchange else "NSE_CM"

    # OI fields (option chain snapshots)
    oi = getattr(tick, "oi", None)
    oi_change = getattr(tick, "oi_change", None)

    # Depth snapshot (full order book)
    depth = getattr(tick, "depth", None)

    # Trade ID for dedup
    trade_id = getattr(tick, "trade_id", None)

    return TickV2(
        symbol_id=getattr(tick, "symbol", None),
        segment=segment,
        price=Decimal(str(last_price)) if last_price is not None and not isinstance(last_price, Decimal) else Decimal(str(last_price or 0)),
        qty=int(getattr(tick, "quantity", getattr(tick, "qty", 0))),
        timestamp=ts,
        source=FeedSource.ANGEL_ONE,  # default; caller overrides per-adapter
        bid=bid,
        ask=ask,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        oi=oi,
        oi_change=oi_change,
        volume=int(getattr(tick, "volume", 0)),
        vwap=getattr(tick, "vwap", None),
        depth=depth,
        trade_id=str(trade_id) if trade_id else None,
        correlation_id=str(getattr(tick, "token", "")) if getattr(tick, "token", None) else None,
    )
