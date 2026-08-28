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
from trading_platform.domain.enums import OrderStatus, OrderType, Side
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
        # Resting LIMIT orders that haven't crossed yet: broker_order_id -> intent.
        # Lets paper mode genuinely exercise ExecutionScheduler's ACKNOWLEDGED ->
        # poll -> chase-to-market path, not just always-immediate-fill — a
        # LIMIT order here only fills when the simulated price actually
        # crosses it, exactly like a real resting order would.
        self._pending: dict[str, OrderIntent] = {}

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

        # Prefer live Angel One price; fall back to signal price. This is the
        # MARKET's reference price, distinct from intent.limit_price (the
        # order's own requested price) below.
        live_px = self._live_price(intent.instrument.symbol)
        market_price = live_px or intent.signal.price

        if intent.order_type == OrderType.LIMIT and intent.limit_price:
            if not self._crosses(intent.signal.side, market_price, intent.limit_price):
                # Resting, not filled: ACKNOWLEDGED + a broker order id is
                # ACK-compatible with ExecutionScheduler._submit_to_broker
                # (spawns _track_order_until_terminal on any ACKNOWLEDGED/
                # SUBMITTED result carrying a broker_order_id) -- the exact
                # same path a live order takes. Without this branch, paper
                # mode could never exercise chase-to-market: every order used
                # to fill synchronously regardless of order_type.
                order_id = f"SIM-LMT-{len(self.submitted):06d}"
                self._pending[order_id] = intent
                return BrokerResult(
                    status=OrderStatus.ACKNOWLEDGED,
                    broker_order_id=order_id,
                    average_price=None,
                    submitted_at=submitted_at,
                    acknowledged_at=acknowledged_at,
                    message="simulated_resting_limit",
                    raw={
                        "mode": "paper_sim",
                        "limit_price": intent.limit_price,
                        "market_price": market_price,
                        "side": intent.signal.side.value,
                    },
                )
            # Already crosses at submission time -- behaves like a marketable
            # order, filled through the same slippage model as MARKET.
            reference_price = market_price
        else:
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

    def order_status(self, order_id: str) -> dict | None:
        """Poll-compatible with ExecutionScheduler._track_order_until_terminal:
        re-checks whether the simulated market price has now crossed the
        resting limit. Fills exactly at the limit price (no extra slippage) --
        a resting limit order's whole guarantee is never filling worse than
        its own limit."""
        intent = self._pending.get(order_id)
        if intent is None:
            return None
        live_px = self._live_price(intent.instrument.symbol)
        market_price = live_px or intent.signal.price
        if not self._crosses(intent.signal.side, market_price, intent.limit_price):
            return {"state": "open", "average_price": 0.0, "filled_units": 0, "message": "resting"}
        del self._pending[order_id]
        return {
            "state": "complete",
            "average_price": intent.limit_price,
            "filled_units": intent.quantity * intent.instrument.lot_size,
            "message": "",
        }

    def cancel_order(self, broker_order_id: str) -> bool:
        return self._pending.pop(broker_order_id, None) is not None

    @staticmethod
    def _crosses(side: Side, market_price: float, limit_price: float) -> bool:
        """True once the market has reached a price the limit order would
        fill at: BUY fills when the market has fallen to/through the limit;
        SELL fills when it has risen to/through the limit."""
        if side == Side.BUY:
            return market_price <= limit_price
        return market_price >= limit_price

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
