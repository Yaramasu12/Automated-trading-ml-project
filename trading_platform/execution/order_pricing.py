"""Limit-at-touch price estimation for entry orders.

Real limit-at-touch (pricing exactly at the best bid/ask) is not achievable
in this codebase today: Angel One's live feed only ever delivers mode-3
"snap quote" ticks, which never populate bid/ask (see
trading_platform/data/live_feed.py's LiveTickFeed._parse docstring — those
fields default to None on every real tick). The only reliable price signal
on the live path is last-traded-price.

So the estimate here is LTP +/- half of an assumed bid/ask spread, not a
real order-book read. It is deliberately labelled as such rather than
presented as if it were touch pricing — fabricating order-book awareness
this codebase does not have would be exactly the kind of inert-component-
presented-as-intelligence failure the project exists to avoid.
"""
from __future__ import annotations

import os

from trading_platform.domain.enums import Side

# Mirrors SimulatedBrokerClient's own default spread_bps (broker/simulated.py)
# so paper and live orders are priced against the same assumption.
_DEFAULT_LIMIT_SPREAD_BPS = 4.0


def limit_spread_bps() -> float:
    return float(os.getenv("ORDER_LIMIT_SPREAD_BPS", str(_DEFAULT_LIMIT_SPREAD_BPS)))


def limit_price_at_touch(reference_price: float, side: Side, spread_bps: float | None = None) -> float:
    """Estimate a limit price near the touch from a reference price (LTP).

    BUY prices slightly above the reference, SELL slightly below, so the
    order rests close to the likely near-touch level rather than paying the
    full spread a MARKET order would. Rounded to paise (2dp), matching NSE's
    price-format convention elsewhere in this codebase.
    """
    if reference_price <= 0:
        return reference_price
    bps = spread_bps if spread_bps is not None else limit_spread_bps()
    half_spread_pct = (bps / 2.0) / 10_000.0
    if side == Side.BUY:
        return round(reference_price * (1.0 + half_spread_pct), 2)
    return round(reference_price * (1.0 - half_spread_pct), 2)
