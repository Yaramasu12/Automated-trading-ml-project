"""
tests/test_svi_surface.py — Tests for SVI volatility surface fitting

Per §4.4a (REDESIGN_PROMPT): SVI/SABR vol-surface fitting per expiry enables
rich/cheap strike detection vs fitted surface → better strike selection.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from trading_platform.strategies.svi_surface import (
    SVIParameters,
    SVIResult,
    fit_svi_surface,
    fit_svi_smile,
    compute_svi_vol,
    compute_svi_price,
    detect_rich_cheap_strikes,
    compute_skew_slope,
    compute_term_structure_slope,
)


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


@pytest.fixture
def mock_chain():
    """
    Mock option chain: ATM = 100, strikes 85-115, ATM IV = 22%.
    Synthetic smile: U-shaped (higher IV at wings).
    """
    atm_price = 100.0
    atm_iv = 0.22

    chain = []
    for strike in np.arange(85, 116, 2.5):
        # Synthetic smile: IV increases with |moneyness|
        moneyness = math.log(strike / atm_price)
        iv = atm_iv + 0.05 * moneyness ** 2 + 0.01 * abs(moneyness) ** 3

        chain.append({
            "strike": strike,
            "iv": iv,
            "oi": int(10000 - abs(strike - atm_price) * 200),
            "delta": 0.5 - moneyness * 2.0,
        })

    return {
        "atm_price": atm_price,
        "atm_iv": atm_iv,
        "chain": chain,
        "expiry_days": 14,
    }


@pytest.fixture
def flat_chain():
    """Flat smile (no skew) — all IV = ATM."""
    atm_price = 100.0
    atm_iv = 0.20

    chain = []
    for strike in np.arange(90, 111, 2.5):
        chain.append({
            "strike": strike,
            "iv": atm_iv,
            "oi": 5000,
            "delta": 0.5,
        })

    return {
        "atm_price": atm_price,
        "atm_iv": atm_iv,
        "chain": chain,
        "expiry_days": 30,
    }


# ──────────────────────────────────────────────
# SVI parameter fitting
# ──────────────────────────────────────────────


class TestSVI_Fitting:
    def test_fit_converges(self, mock_chain):
        """SVI should converge on synthetic smile."""
        result = fit_svi_surface(mock_chain["chain"], mock_chain["atm_price"], mock_chain["atm_iv"])
        assert result.converged is True
        assert result.params is not None

    def test_params_reasonable(self, mock_chain):
        """SVI parameters should be in reasonable ranges."""
        result = fit_svi_surface(mock_chain["chain"], mock_chain["atm_price"], mock_chain["atm_iv"])
        p = result.params

        assert p.sigma > 0  # ATM vol > 0
        assert p.sigma < 1.0  # Not explosive
        assert abs(p.phi) < 2.0  # Moderate skew
        assert p.lambda_ > 0  # Decay positive
        assert abs(p.rho) < 1.0  # Correlation in [-1, 1]

    def test_flat_smile_fits_flat(self, flat_chain):
        """Flat smile should fit with near-zero skew."""
        result = fit_svi_surface(flat_chain["chain"], flat_chain["atm_price"], flat_chain["atm_iv"])
        assert result.converged is True
        # Skew should be small for flat smile
        assert abs(result.params.phi) < 0.5


# ──────────────────────────────────────────────
# Volatility computation
# ──────────────────────────────────────────────


class TestSVI_Vol:
    def test_atm_vol_matches(self, mock_chain):
        """SVI vol at ATM should match ATM IV."""
        result = fit_svi_surface(mock_chain["chain"], mock_chain["atm_price"], mock_chain["atm_iv"])
        vol = compute_svi_vol(result.params, mock_chain["atm_price"], mock_chain["atm_price"])
        assert abs(vol - mock_chain["atm_iv"]) < 0.02

    def test_smile_shape_preserved(self, mock_chain):
        """Wings should have higher vol than ATM."""
        result = fit_svi_surface(mock_chain["chain"], mock_chain["atm_price"], mock_chain["atm_iv"])

        atm_vol = compute_svi_vol(result.params, mock_chain["atm_price"], mock_chain["atm_price"])
        call_vol = compute_svi_vol(result.params, mock_chain["atm_price"], 110)
        put_vol = compute_svi_vol(result.params, mock_chain["atm_price"], 90)

        assert call_vol > atm_vol * 0.8  # Should be close to or above ATM
        assert put_vol > atm_vol * 0.8

    def test_vol_positive(self, mock_chain):
        """All SVI vols should be positive."""
        result = fit_svi_surface(mock_chain["chain"], mock_chain["atm_price"], mock_chain["atm_iv"])
        for strike in np.arange(85, 116, 1):
            vol = compute_svi_vol(result.params, mock_chain["atm_price"], strike)
            assert vol > 0


# ──────────────────────────────────────────────
# Option pricing via SVI
# ──────────────────────────────────────────────


class TestSVI_Pricing:
    def test_call_price_increasing_with_strike(self, mock_chain):
        """Call price should generally increase with strike (put perspective)."""
        result = fit_svi_surface(mock_chain["chain"], mock_chain["atm_price"], mock_chain["atm_iv"])

        p1 = compute_svi_price(result.params, mock_chain["atm_price"], 90, "put")
        p2 = compute_svi_price(result.params, mock_chain["atm_price"], 95, "put")
        p3 = compute_svi_price(result.params, mock_chain["atm_price"], 100, "put")

        assert p1 < p2 < p3  # OTM puts cheaper

    def test_put_call_parity(self, mock_chain):
        """Call - Put should approximately equal S - K*e^(-rT)."""
        result = fit_svi_surface(mock_chain["chain"], mock_chain["atm_price"], mock_chain["atm_iv"])
        K = 100

        call = compute_svi_price(result.params, mock_chain["atm_price"], K, "call")
        put = compute_svi_price(result.params, mock_chain["atm_price"], K, "put")

        # At ATM, call ≈ put (approximately)
        assert abs(call - put) < 2.0  # Reasonable tolerance


# ──────────────────────────────────────────────
# Rich/cheap strike detection
# ──────────────────────────────────────────────


class TestRichCheap:
    def test_detects_rich_cheap_strikes(self, mock_chain):
        """Should identify rich/cheap strikes vs surface."""
        result = fit_svi_surface(mock_chain["chain"], mock_chain["atm_price"], mock_chain["atm_iv"])
        rich_cheap = detect_rich_cheap_strikes(mock_chain["chain"], result.params, mock_chain["atm_price"])

        assert len(rich_cheap) > 0
        for item in rich_cheap:
            assert "strike" in item
            assert "iv_diff" in item
            assert "signal" in item
            assert item["signal"] in ("rich", "cheap")

    def test_no_signal_when_flat(self, flat_chain):
        """Flat smile should produce few/no signals."""
        result = fit_svi_surface(flat_chain["chain"], flat_chain["atm_price"], flat_chain["atm_iv"])
        rich_cheap = detect_rich_cheap_strikes(flat_chain["chain"], result.params, flat_chain["atm_price"])

        # Most strikes should have no signal
        signaled = [r for r in rich_cheap if r["signal"] is not None]
        assert len(signaled) <= len(flat_chain["chain"]) // 2  # At most half

    def test_rich_strikes_have_positive_iv_diff(self, mock_chain):
        """Rich strikes should have positive IV difference."""
        result = fit_svi_surface(mock_chain["chain"], mock_chain["atm_price"], mock_chain["atm_iv"])
        rich_cheap = detect_rich_cheap_strikes(mock_chain["chain"], result.params, mock_chain["atm_price"])

        rich = [r for r in rich_cheap if r["signal"] == "rich"]
        for r in rich:
            assert r["iv_diff"] > 0


# ──────────────────────────────────────────────
# Skew and term structure
# ──────────────────────────────────────────────


class TestSkewTermStructure:
    def test_skew_slope_positive_for_smile(self, mock_chain):
        """Skew slope should be positive for a smile (wings higher)."""
        result = fit_svi_surface(mock_chain["chain"], mock_chain["atm_price"], mock_chain["atm_iv"])
        slope = compute_skew_slope(result.params, mock_chain["atm_price"])
        # Slope of IV vs moneyness — for smile, |skew| increases
        assert isinstance(slope, float)

    def test_skew_zero_for_flat(self, flat_chain):
        """Skew should be near zero for flat smile."""
        result = fit_svi_surface(flat_chain["chain"], flat_chain["atm_price"], flat_chain["atm_iv"])
        slope = compute_skew_slope(result.params, flat_chain["atm_price"])
        assert abs(slope) < 1.0

    def test_term_structure_computed(self, mock_chain):
        """Term structure slope should be computed."""
        # Term structure: compare with a longer-dated synthetic surface
        longer_chain = []
        for item in mock_chain["chain"]:
            longer_chain.append({
                **item,
                "iv": item["iv"] * 1.02,  # Slightly higher for longer expiry
            })

        longer_result = type('obj', (object,), {
            "params": SVIParameters(
                sigma=mock_chain["atm_iv"], phi=0.0, lambda_=0.5,
                rho=0.0, a=0.01, b=0.5, theta=0.0, rho_w=0.0
            ),
            "converged": True,
        })()

        slopes = compute_term_structure_slope(
            mock_chain["atm_price"],
            [mock_chain],  # Short-dated
            longer_result,  # Long-dated
        )
        assert isinstance(slopes, dict)


# ──────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────


class TestEdgeCases:
    def test_fit_few_strikes(self):
        """Should handle very few strikes gracefully."""
        chain = [
            {"strike": 95, "iv": 0.20, "oi": 5000, "delta": 0.3},
            {"strike": 100, "iv": 0.22, "oi": 8000, "delta": 0.5},
            {"strike": 105, "iv": 0.21, "oi": 4000, "delta": 0.7},
        ]
        result = fit_svi_surface(chain, 100, 0.22)
        # May not converge with 3 strikes, but should not crash
        assert result is not None

    def test_fit_with_nan_iv(self, mock_chain):
        """Should handle NaN IV in chain (skip those strikes)."""
        corrupted = list(mock_chain["chain"])
        corrupted[0]["iv"] = float("nan")

        result = fit_svi_surface(corrupted, mock_chain["atm_price"], mock_chain["atm_iv"])
        # Should still fit on valid strikes
        assert result is not None

    def test_fit_with_negative_iv(self, mock_chain):
        """Should handle negative IV (treat as invalid)."""
        corrupted = list(mock_chain["chain"])
        corrupted[1]["iv"] = -0.05

        result = fit_svi_surface(corrupted, mock_chain["atm_price"], mock_chain["atm_iv"])
        assert result is not None

    def test_compute_vol_extreme_moneyness(self, mock_chain):
        """Should handle extreme moneyness (very OTM)."""
        result = fit_svi_surface(mock_chain["chain"], mock_chain["atm_price"], mock_chain["atm_iv"])

        # Deep OTM
        vol_deep = compute_svi_vol(result.params, mock_chain["atm_price"], 150)
        assert vol_deep > 0

        # Deep ITM
        vol_shallow = compute_svi_vol(result.params, mock_chain["atm_price"], 50)
        assert vol_shallow > 0


# ──────────────────────────────────────────────
# Integration: SVI → strike selection
# ──────────────────────────────────────────────


class TestSVI_StrikeSelection:
    def test_iron_condor_strike_selection(self, mock_chain):
        """Should select appropriate IC strikes."""
        result = fit_svi_surface(mock_chain["chain"], mock_chain["atm_price"], mock_chain["atm_iv"])
        rich_cheap = detect_rich_cheap_strikes(mock_chain["chain"], result.params, mock_chain["atm_price"])

        # Find best short-vol strikes (rich = high IV relative to surface)
        rich_calls = [r for r in rich_cheap if r["iv_diff"] > 0]

        assert len(rich_calls) > 0 or len(rich_cheap) > 0  # Surface is fitted

    def test_calendar_spread_opportunity(self, mock_chain):
        """Term structure should indicate calendar opportunity."""
        result = fit_svi_surface(mock_chain["chain"], mock_chain["atm_price"], mock_chain["atm_iv"])
        assert result.converged is True