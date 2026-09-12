"""Tests for OptionStrategyPlan._effective_lot_size error path."""
from __future__ import annotations

from datetime import date

import pytest

from trading_platform.derivatives.option_legs import OptionLeg, OptionStrategyPlan
from trading_platform.domain.enums import OptionType, Side


def _make_leg(**overrides) -> OptionLeg:
    defaults = dict(
        underlying="NIFTY",
        expiry=date(2026, 10, 30),
        strike=25000.0,
        option_type=OptionType.CE,
        side=Side.BUY,
        quantity=1,
        lot_size=75,
        reference_premium=120.0,
    )
    defaults.update(overrides)
    return OptionLeg(**defaults)


def test_effective_lot_size_raises_when_no_leg_has_lot_size() -> None:
    """_effective_lot_size must raise ValueError when no leg defines lot_size."""
    leg_a = _make_leg(lot_size=None, option_type=OptionType.CE, side=Side.BUY)
    leg_b = _make_leg(lot_size=None, option_type=OptionType.PE, side=Side.SELL)
    plan = OptionStrategyPlan(
        strategy_name="test_no_lot",
        structure="straddle",
        underlying="NIFTY",
        legs=(leg_a, leg_b),
        spot_at_creation=25000.0,
    )
    with pytest.raises(ValueError, match="no leg with a defined lot_size"):
        plan._effective_lot_size()


def test_effective_lot_size_returns_first_defined() -> None:
    """Sanity: when a leg *does* have lot_size, it is returned."""
    leg_a = _make_leg(lot_size=75)
    leg_b = _make_leg(lot_size=None, option_type=OptionType.PE, side=Side.SELL)
    plan = OptionStrategyPlan(
        strategy_name="test_with_lot",
        structure="straddle",
        underlying="NIFTY",
        legs=(leg_a, leg_b),
        spot_at_creation=25000.0,
    )
    assert plan._effective_lot_size() == 75
