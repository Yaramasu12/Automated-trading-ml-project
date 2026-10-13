"""India intraday-equity execution-cost model (2026-10-13 gap go/no-go).

Locks in the shape that matters for the gap verdict: cost falls with liquidity,
rises as per-name order size shrinks (the Rs-20 brokerage floor), and lands in a
realistic band for liquid large-caps at the open.
"""
from __future__ import annotations

from trading_platform.backtesting.execution_cost import IntradayCostModel

M = IntradayCostModel()
CR = 1e7  # one crore in rupees


def test_liquid_largecap_small_order_is_realistic_band():
    # ~Rs 29k order in a Rs 200cr/day name, 1.5% daily vol.
    c = M.roundtrip_cost_fraction(notional=29_000, adv_rupees=200 * CR, daily_vol=0.015)
    # Brokerage floor dominates on a small order; expect ~0.15%-0.45% round-trip.
    assert 0.0012 < c < 0.0050, c


def test_thinner_name_costs_more_than_liquid():
    liquid = M.roundtrip_cost_fraction(29_000, 200 * CR, 0.015)
    thin = M.roundtrip_cost_fraction(29_000, 2 * CR, 0.03)
    assert thin > liquid


def test_smaller_order_costs_more_pct_via_brokerage_floor():
    big = M.roundtrip_cost_fraction(500_000, 200 * CR, 0.015)
    small = M.roundtrip_cost_fraction(10_000, 200 * CR, 0.015)
    assert small > big  # Rs-20 floor is a bigger % of a small order


def test_impact_grows_with_order_vs_adv():
    small_rel = M.roundtrip_cost_fraction(100_000, 500 * CR, 0.03)
    large_rel = M.roundtrip_cost_fraction(100_000, 2 * CR, 0.03)
    assert large_rel > small_rel  # same order, thinner ADV -> more impact + spread


def test_open_auction_multiplier_raises_spread():
    base = IntradayCostModel(open_auction_mult=1.0)
    wide = IntradayCostModel(open_auction_mult=2.0)
    assert (wide.roundtrip_cost_fraction(50_000, 50 * CR, 0.02)
            > base.roundtrip_cost_fraction(50_000, 50 * CR, 0.02))


def test_cost_is_positive_and_finite_on_degenerate_inputs():
    c = M.roundtrip_cost_fraction(notional=0, adv_rupees=0, daily_vol=0)
    assert c > 0 and c < 1.0
