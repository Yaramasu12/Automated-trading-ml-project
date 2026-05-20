"""Simulated broker with realistic slippage and market-impact modelling.

Previously this client filled at exactly `intent.signal.price` (or
`limit_price`), which produced optimistically perfect backtest fills. The
README also incorrectly claimed backtests modelled slippage when they did
not. This rewrite adds:

  - A configurable bid/ask half-spread (in basis points of price), applied
    against the trader: BUY pays the ask, SELL hits the bid.
  - A square-root market-impact term proportional to participation in a
    notional capacity (notional / impact_capacity), tunable via
    `impact_bps_per_unit`. This is the standard Almgren-Chriss-style
    impact shape used in simple execution models.
  - A small additive noise term so identical orders do not always fill at
    identical prices.

All slippage moves the fill *away from* the trader, never toward them. The
applied slippage is recorded in `BrokerResult.raw["slippage_pct"]` so tests
and analysis can verify direction and magnitude.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from trading_platform.broker.base import BrokerClient, BrokerResult
from trading_platform.domain.enums import OrderStatus, Side
from trading_platform.domain.models import OrderIntent


class SimulatedBrokerClient(BrokerClient):
    """Simulated broker that fills at live Angel One tick prices when available.

    When a live feed is wired in via set_live_feed(), every fill uses the
    current real market price as the reference — so P&L and slippage are
    computed against actual Angel One prices, not stale signal prices.
    No orders are ever sent to Angel One.
    """

    name = "SIMULATED"

    def __init__(
        self,
        latency_ms: int = 12,
        spread_bps: float = 4.0,
        impact_bps_per_unit: float = 6.0,
        impact_capacity_notional: float = 5_000_000.0,
        noise_bps: float = 1.0,
        seed: int | None = 42,
    ):
        self.latency_ms = latency_ms
        self.spread_bps = spread_bps
        self.impact_bps_per_unit = impact_bps_per_unit
        self.impact_capacity_notional = max(impact_capacity_notional, 1.0)
        self.noise_bps = noise_bps
        self.submitted: list[OrderIntent] = []
        self._rng = random.Random(seed)
        self._live_feed = None   # set via set_live_feed() after runtime init

    def set_live_feed(self, live_feed) -> None:
        """Wire in the Angel One live tick feed for real-time fill prices."""
        self._live_feed = live_feed

    def _live_price(self, symbol: str) -> float | None:
        """Return the latest Angel One live tick price for a symbol, or None."""
        if self._live_feed is None:
            return None
        try:
            tick = self._live_feed.latest_tick(symbol)
            if tick and getattr(tick, "last_price", 0) > 0:
                return float(tick.last_price)
        except Exception:
            pass
        return None

    def is_ready(self) -> bool:
        return True

    def submit_order(self, intent: OrderIntent) -> BrokerResult:
        submitted_at = datetime.now(timezone.utc)
        acknowledged_at = submitted_at + timedelta(milliseconds=self.latency_ms)
        self.submitted.append(intent)

        # Prefer live Angel One price; fall back to signal price
        live_px = self._live_price(intent.instrument.symbol)
        reference_price = live_px or intent.limit_price or intent.signal.price
        fill_price, slippage_pct = self._apply_slippage(intent, reference_price)
        return BrokerResult(
            status=OrderStatus.FILLED,
            broker_order_id=f"SIM-{len(self.submitted):06d}",
            average_price=fill_price,
            submitted_at=submitted_at,
            acknowledged_at=acknowledged_at,
            message="simulated_fill",
            raw={
                "mode": "paper_sim",
                "reference_price": reference_price,
                "live_price_used": live_px is not None,
                "signal_price": intent.signal.price,
                "slippage_pct": slippage_pct,
                "side": intent.signal.side.value,
            },
        )

    def positions(self) -> list[dict]:
        return []

    def _apply_slippage(self, intent: OrderIntent, reference_price: float) -> tuple[float, float]:
        if reference_price <= 0:
            return reference_price, 0.0

        # Half-spread: half of the configured bid/ask spread, applied against
        # the trader's direction.
        half_spread_pct = (self.spread_bps / 2.0) / 10_000.0

        # Square-root market impact: bps per sqrt(participation).
        notional = abs(reference_price * intent.quantity * intent.instrument.lot_size)
        participation = notional / self.impact_capacity_notional
        impact_pct = (self.impact_bps_per_unit * math.sqrt(max(0.0, participation))) / 10_000.0

        # Symmetric mean-zero microstructure noise (in bps), but *bounded so it
        # cannot overwhelm the deterministic adverse component*.
        noise_pct = (self._rng.uniform(-self.noise_bps, self.noise_bps)) / 10_000.0

        adverse_pct = half_spread_pct + impact_pct
        if intent.signal.side == Side.BUY:
            slippage_pct = adverse_pct + max(0.0, noise_pct)  # noise only ever hurts
            fill_price = reference_price * (1.0 + slippage_pct)
        else:
            slippage_pct = adverse_pct + max(0.0, -noise_pct)
            fill_price = reference_price * (1.0 - slippage_pct)
        # Clamp to a sensible non-negative price.
        fill_price = max(0.01, fill_price)
        return fill_price, slippage_pct
