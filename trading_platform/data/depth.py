"""
Depth snapshot models — top-of-book and Depth-20 order book.

Used by TickV2.depth field and by the microstructure feature
pipeline (§3.1 of REDESIGN_PROMPT.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True, slots=True)
class DepthLevel:
    """A single price level in the order book."""
    price: Decimal
    qty: int
    orders: int = 0  # number of orders at this level (if available)


@dataclass(frozen=True, slots=True)
class DepthSnapshot:
    """
    Full depth snapshot (Depth-20 or top-5 depending on source).

    Angel One Depth-20: up to 20 bid + 20 ask levels.
    Mode-3 snap-quote: only best bid/ask (DepthSnapshot with 1 level each).
    TrueData: full Depth-20 where available.
    """
    symbol_id: str
    timestamp: any  # datetime
    bids: list[DepthLevel] = ()  # descending price
    asks: list[DepthLevel] = ()  # ascending price

    # --- helpers ---

    @property
    def best_bid(self) -> Optional[DepthLevel]:
        """Top bid (highest price)."""
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[DepthLevel]:
        """Top ask (lowest price)."""
        return self.asks[0] if self.asks else None

    @property
    def mid_price(self) -> Optional[Decimal]:
        """Mid of best bid/ask."""
        bb = self.best_bid
        ba = self.best_ask
        if bb and ba:
            return (bb.price + ba.price) / 2
        return None

    @property
    def spread_bps(self) -> Optional[float]:
        """Bid-ask spread in basis points."""
        mid = self.mid_price
        if mid is None or mid == 0:
            return None
        bb = self.best_bid
        ba = self.best_ask
        if bb is None or ba is None:
            return None
        return float((ba.price - bb.price) * 10000 / mid)

    @property
    def depth_imbalance(self) -> Optional[float]:
        """
        Order-book imbalance: (ask_qty - bid_qty) / (ask_qty + bid_qty).
        Positive = more depth on ask side (resistance).
        Negative = more depth on bid side (support).
        """
        bid_total = sum(b.qty for b in self.bids)
        ask_total = sum(a.qty for a in self.asks)
        total = bid_total + ask_total
        if total == 0:
            return None
        return (ask_total - bid_total) / total

    @property
    def depth_slope(self) -> Optional[float]:
        """
        Slope of depth vs price (linear regression approximation).
        Positive = depth increases away from center (stable book).
        Negative = depth concentrates near center (thin book).
        """
        if len(self.bids) < 2 or len(self.asks) < 2:
            return None
        bid_prices = [b.price for b in self.bids]
        bid_qtys = [b.qty for b in self.bids]
        ask_prices = [a.price for a in self.asks]
        ask_qtys = [a.qty for a in self.asks]
        # Simple slope approximation: (last - first) / (price_range)
        bid_slope = (bid_qtys[-1] - bid_qtys[0]) / max(float(bid_prices[0] - bid_prices[-1]), 1)
        ask_slope = (ask_qtys[-1] - ask_qtys[0]) / max(float(ask_prices[-1] - ask_prices[0]), 1)
        return (bid_slope + ask_slope) / 2

    def to_dict(self) -> dict:
        """Serialize for transport."""
        return {
            "symbol_id": self.symbol_id,
            "timestamp": self.timestamp.isoformat() if hasattr(self.timestamp, "isoformat") else str(self.timestamp),
            "bids": [{"price": b.price, "qty": b.qty, "orders": b.orders} for b in self.bids],
            "asks": [{"price": a.price, "qty": a.qty, "orders": a.orders} for a in self.asks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DepthSnapshot":
        """Deserialize from transport."""
        bids = [
            DepthLevel(price=Decimal(b["price"]), qty=b["qty"], orders=b.get("orders", 0))
            for b in d.get("bids", [])
        ]
        asks = [
            DepthLevel(price=Decimal(a["price"]), qty=a["qty"], orders=a.get("orders", 0))
            for a in d.get("asks", [])
        ]
        return cls(
            symbol_id=d["symbol_id"],
            timestamp=d["timestamp"],
            bids=bids,
            asks=asks,
        )

    def __repr__(self) -> str:
        return (
            f"DepthSnapshot(sym={self.symbol_id}, "
            f"bids={len(self.bids)}, asks={len(self.asks)})"
        )


# Alias for backward compatibility with tick_bus.py imports
TickDepthV2 = DepthSnapshot
