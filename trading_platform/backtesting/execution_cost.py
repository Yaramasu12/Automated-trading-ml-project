"""Realistic India intraday-equity execution-cost model (2026-10-13).

Built for the gap-signal go/no-go: the gap backtest previously subtracted a flat
round-trip % from every trade. That hides the real frictions of entering ~34
names at the open and exiting at the close. This models them explicitly, per
trade, as a FRACTION of the order notional, so the gap gates run against costs a
real account would actually pay — not an idealized flat number.

Round-trip cost = brokerage + statutory charges + spread/slippage + market impact.

Assumptions are explicit and conservative (opens are less liquid than mid-session):
  * Brokerage: discount-broker style — min(0.03% of turnover, Rs 20) PER leg
    (two legs round-trip). On small orders the Rs-20 floor dominates, so the %
    cost RISES as per-name size falls — exactly the real penalty of spreading
    one account across many names.
  * Statutory (intraday equity): STT 0.025% on the SELL leg, exchange txn
    0.00297% both legs, SEBI 0.0001% both legs, stamp 0.003% on the BUY leg,
    GST 18% on (brokerage + exchange txn). These are fixed by regulation.
  * Spread + slippage: a liquidity-tiered effective half-spread per side, keyed
    off the name's average daily TURNOVER (ADV in rupees), multiplied by an
    open-auction premium because the first prints are wider than mid-session.
  * Market impact: square-root law, k * daily_vol * sqrt(notional / ADV) per
    side — negligible for small orders in liquid names, real for thin ones.

Intraday long OR short needs no borrow (NSE MIS squares off same day), so there
is no overnight-borrow term; that is correct for an open-to-close strategy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class IntradayCostModel:
    brokerage_pct: float = 0.0003        # 0.03% of turnover per leg...
    brokerage_cap: float = 20.0          # ...capped at Rs 20 per leg
    stt_sell: float = 0.00025            # 0.025% on sell turnover (intraday equity)
    exch_txn: float = 0.0000297          # per leg
    sebi: float = 0.000001               # per leg
    stamp_buy: float = 0.00003           # 0.003% on buy turnover
    gst_rate: float = 0.18               # on brokerage + exchange txn
    impact_k: float = 0.1                # square-root impact coefficient
    open_auction_mult: float = 1.5       # opens are wider than mid-session

    def half_spread(self, adv_rupees: float) -> float:
        """Effective half-spread per side (fraction), tiered by daily turnover.
        Mid-session reference; the open-auction multiplier is applied on top."""
        adv_cr = max(adv_rupees, 0.0) / 1e7  # rupees -> crore
        if adv_cr >= 100:
            base = 0.0003    # mega-cap, very liquid
        elif adv_cr >= 20:
            base = 0.0006
        elif adv_cr >= 5:
            base = 0.0012
        elif adv_cr >= 1:
            base = 0.0025
        else:
            base = 0.0050    # thin name — wide, hard to fill at a good price
        return base * self.open_auction_mult

    def roundtrip_cost_fraction(
        self, notional: float, adv_rupees: float, daily_vol: float,
    ) -> float:
        """Total round-trip cost as a fraction of notional for one name."""
        notional = max(float(notional), 1.0)
        adv_rupees = max(float(adv_rupees), 1.0)
        daily_vol = max(float(daily_vol), 0.0)

        brokerage = 2.0 * min(self.brokerage_pct * notional, self.brokerage_cap)
        exch = self.exch_txn * 2.0 * notional
        statutory_rupees = (
            self.stt_sell * notional
            + exch
            + self.sebi * 2.0 * notional
            + self.stamp_buy * notional
            + self.gst_rate * (brokerage + exch)
        )
        slippage_frac = 2.0 * self.half_spread(adv_rupees)           # both sides
        impact_frac = 2.0 * self.impact_k * daily_vol * math.sqrt(notional / adv_rupees)

        fixed_frac = (brokerage + statutory_rupees) / notional
        return fixed_frac + slippage_frac + impact_frac
