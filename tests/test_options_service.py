"""Tests for the extracted OptionsService (Phase 2 runtime decomposition)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trading_platform.api.options_service import OptionsService


class _Calendar:
    def expiries(self, underlying, today): return [date(2026, 1, 29), date(2026, 2, 26)]
    def nearest(self, underlying, as_of): return date(2026, 1, 29)


@dataclass
class _Greeks:
    delta: float = 0.5
    gamma: float = 0.1
    theta: float = -0.2
    vega: float = 0.3
    rho: float = 0.05
    price: float = 10.0


class _GreeksCalc:
    def calculate(self, **kwargs): return _Greeks()


def _svc() -> OptionsService:
    return OptionsService(
        expiry_calendar=_Calendar(),
        option_chain_builder=None,
        live_feed=None,
        greeks_calculator=_GreeksCalc(),
        iv_surface_builder=None,
    )


def test_expiries_shape():
    out = _svc().expiries("nifty")
    assert out["underlying"] == "NIFTY"
    assert out["count"] == 2
    assert len(out["expiries"]) == 2
    assert "nearest" in out


def test_calculate_greeks_serializes():
    out = _svc().calculate_greeks({
        "spot_price": 100, "strike": 100, "days_to_expiry": 7,
        "volatility": 0.2, "option_type": "CE",
    })
    assert out["delta"] == 0.5
    assert out["price"] == 10.0
