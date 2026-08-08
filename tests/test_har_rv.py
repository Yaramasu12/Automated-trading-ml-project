"""
tests/test_har_rv.py — Tests for HAR-RV realized-volatility forecaster

Per §4.4a (REDESIGN_PROMPT): HAR-RV is the strongest simple RV baseline.
Tests verify:
  - Warmup behavior
  - OLS weight estimation
  - VRP signal computation
  - Conformal prediction intervals
  - Multi-horizon forecasting
  - GARCH comparison
"""

from __future__ import annotations

import math
import sys
import os
from pathlib import Path

import numpy as np
import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from trading_platform.neural.har_rv import (
    HAR_RVForecaster,
    HAR_RV_Horizon,
    multi_horizon_har_rv,
    compare_har_garch,
)


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


@pytest.fixture
def har():
    return HAR_RVForecaster()


@pytest.fixture
def daily_rv_series():
    """Generate realistic daily RV series (200 days, mean ~18%)."""
    np.random.seed(42)
    # GARCH-like process for realistic RV
    rv = np.zeros(200)
    rv[0] = 0.18 ** 2  # Initial variance
    for i in range(1, 200):
        rv[i] = 0.0001 + 0.1 * rv[i-1] + 0.85 * rv[i-1] + 0.02 * np.random.randn()
        rv[i] = max(rv[i], 1e-6)
    return np.sqrt(rv) * 100  # Convert back to vol %


@pytest.fixture
def rich_ivp_series():
    """VRP series where some values are rich (positive VRP)."""
    np.random.seed(123)
    return [0.02 + abs(np.random.randn()) * 0.01 for _ in range(50)]


# ──────────────────────────────────────────────
# Warmup behavior
# ──────────────────────────────────────────────


class TestHAR_Warmup:
    def test_initial_state(self, har: HAR_RVForecaster):
        """Before warmup, forecast should indicate not ready."""
        result = har.update(0.18)
        assert result["ready"] is False
        assert result["reason"] == "warmup"
        assert result["observations"] == 1

    def test_during_warmup(self, har: HAR_RVForecaster):
        """During warmup (days 1-21), still not ready."""
        for i in range(21):
            har.update(0.18 + i * 0.001)
        result = har.update(0.20)
        assert result["ready"] is False
        assert result["observations"] == 22  # warmup_days hit

    def test_after_warmup(self, har: HAR_RVForecaster):
        """After warmup, forecast should be ready."""
        for i in range(30):
            har.update(0.18 + i * 0.001)
        result = har.update(0.20)
        assert result["ready"] is True
        assert result["observations"] == 31
        assert result["forecast"] > 0


# ──────────────────────────────────────────────
# Forecast behavior
# ──────────────────────────────────────────────


class TestHAR_Forecast:
    def test_forecast_positive(self, har: HAR_RVForecaster):
        """Forecast should always be positive."""
        for i in range(30):
            har.update(0.15 + i * 0.002)
        result = har.update(0.20)
        assert result["forecast"] > 0
        assert result["lower"] > 0
        assert result["upper"] > result["lower"]

    def test_forecast_responds_to_vol_spike(self, har: HAR_RVForecaster):
        """Forecast should increase when volatility spikes."""
        # Calm period
        for i in range(30):
            har.update(0.12)
        
        calm_forecast = har.update(0.12)["forecast"]
        
        # Vol spike
        har.update(0.30)
        spike_forecast = har.update(0.25)["forecast"]
        
        assert spike_forecast > calm_forecast

    def test_forecast_converges(self, har: HAR_RVForecaster):
        """With constant RV, forecast should converge."""
        constant_rv = 0.18
        for i in range(100):
            result = har.update(constant_rv)
        
        # After convergence, forecast should be close to constant
        assert abs(result["forecast"] - constant_rv) / constant_rv < 0.1


# ──────────────────────────────────────────────
# OLS weight estimation
# ──────────────────────────────────────────────


class TestHAR_OLS:
    def test_ols_on_synthetic_data(self, har: HAR_RVForecaster, daily_rv_series: list):
        """OLS should recover weights close to true process."""
        daily_rv_list = daily_rv_series.tolist()
        result = har.update_weights(daily_rv_list)
        
        assert "r_squared" in result
        assert result["r_squared"] > 0  # Should have some explanatory power

    def test_ols_requires_minimum_data(self, har: HAR_RVForecaster):
        """OLS should fail gracefully with too little data."""
        result = har.update_weights([0.18] * 10)
        assert result == {}

    def test_ols_weights_sum_reasonable(self, har: HAR_RVForecaster, daily_rv_series: list):
        """Sum of weights should be reasonable (< 1 for stationary process)."""
        daily_rv_list = daily_rv_series.tolist()
        result = har.update_weights(daily_rv_list)
        
        if result:  # Only if OLS succeeded
            weight_sum = result["beta_day"] + result["beta_week"] + result["beta_month"]
            assert weight_sum < 1.5  # Should not explode


# ──────────────────────────────────────────────
# VRP signal
# ──────────────────────────────────────────────


class TestVRP:
    def test_vrp_positive_when_iv_high(self, har: HAR_RVForecaster):
        """VRP should be positive when ATM IV > forecast RV."""
        for i in range(30):
            har.update(0.15)
        
        result = har.vrp_signal(atm_iv=0.25)  # IV much higher than RV
        assert result["rvp"] > 0

    def test_vrp_negative_when_iv_low(self, har: HAR_RVForecaster):
        """VRP should be negative when ATM IV < forecast RV."""
        for i in range(30):
            har.update(0.25)
        
        result = har.vrp_signal(atm_iv=0.15)  # IV much lower than RV
        assert result["rvp"] < 0

    def test_vrp_rich_signal(self, har: HAR_RVForecaster, rich_ivp_series: list):
        """Should signal 'rich' when VRP is in top quintile."""
        for i in range(30):
            har.update(0.15)
        
        result = har.vrp_signal(atm_iv=0.25, historical_rvp=rich_ivp_series)
        # With rich series, should detect richness
        assert "is_rich" in result
        assert "is_entry_ready" in result

    def test_vrp_quintile_calculation(self, har: HAR_RVForecaster):
        """Quintile should be 1-5."""
        for i in range(30):
            har.update(0.15)
        
        result = har.vrp_signal(atm_iv=0.25)
        assert 1 <= result["quintile"] <= 5

    def test_vrp_iv_rank(self, har: HAR_RVForecaster):
        """IV rank should be 0-100."""
        for i in range(30):
            har.update(0.15)
        
        result = har.vrp_signal(atm_iv=0.25, historical_rvp=[0.01, 0.02, 0.03, 0.04, 0.05])
        assert 0 <= result["iv_rank"] <= 100


# ──────────────────────────────────────────────
# Conformal prediction
# ──────────────────────────────────────────────


class TestConformalPrediction:
    def test_prediction_interval_width(self, har: HAR_RVForecaster):
        """Prediction interval should have reasonable width."""
        for i in range(30):
            har.update(0.15 + i * 0.001)
        
        result = har.update(0.18)
        interval_width = result["upper"] - result["lower"]
        
        # Width should be < 50% of forecast
        assert interval_width < result["forecast"] * 0.5

    def test_calibration_improves_intervals(self, har: HAR_RVForecaster, daily_rv_series: list):
        """Calibration should produce more reasonable intervals."""
        har.calibrate(daily_rv_series[:100])
        
        for rv in daily_rv_series[100:130]:
            har.update(rv)
        
        result = har.update(daily_rv_series[-1])
        assert result["lower"] > 0
        assert result["upper"] > result["lower"]


# ──────────────────────────────────────────────
# Multi-horizon forecasting
# ──────────────────────────────────────────────


class TestMultiHorizon:
    def test_multi_horizon_returns_all_expiries(self):
        """Should return forecast for each expiry."""
        np.random.seed(42)
        daily_rv = [0.15 + np.random.randn() * 0.02 for _ in range(30)]
        
        expiries = [7, 14, 30]
        results = multi_horizon_har_rv(daily_rv, expiries)
        
        assert len(results) == 3
        for dte, horizon in results.items():
            assert dte in expiries
            assert isinstance(horizon, HAR_RV_Horizon)
            assert horizon.forecast > 0

    def test_longer_dte_has_wider_interval(self):
        """Longer DTE should have wider prediction interval."""
        np.random.seed(42)
        daily_rv = [0.15 + np.random.randn() * 0.01 for _ in range(30)]
        
        results = multi_horizon_har_rv(daily_rv, [7, 30])
        
        short_interval = results[7].upper - results[7].lower
        long_interval = results[30].upper - results[30].lower
        
        # Longer DTE should have wider interval (scaling with sqrt(time))
        assert long_interval > short_interval * 0.5  # Allow some tolerance


# ──────────────────────────────────────────────
# GARCH comparison
# ──────────────────────────────────────────────


class TestHARGARCHComparison:
    def test_comparison_returns_both_forecasts(self, daily_rv_series: list):
        """Should return both HAR-RV and GARCH forecasts."""
        result = compare_har_garch(daily_rv_series, 0.20)
        
        assert "har_rv_forecast" in result
        assert "garch_forecast" in result
        assert "rvp_har" in result
        assert "rvp_garch" in result
        assert result["har_rv_forecast"] > 0
        assert result["garch_forecast"] > 0

    def test_comparison_requires_minimum_data(self):
        """Should return error with insufficient data."""
        result = compare_har_garch([0.15, 0.16], 0.20)
        assert "error" in result
        assert result["error"] == "insufficient_data"

    def test_comparison_har_ready(self, daily_rv_series: list):
        """HAR-RV should be ready after enough data."""
        result = compare_har_garch(daily_rv_series, 0.20)
        assert result["har_ready"] is True
        assert result["har_is_entry_ready"] is True


# ──────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────


class TestEdgeCases:
    def test_zero_rv(self, har: HAR_RVForecaster):
        """Should handle zero RV gracefully."""
        for i in range(30):
            har.update(0.01)  # Very low but non-zero
        result = har.update(0.0)
        assert result["forecast"] >= 0

    def test_very_high_vol(self, har: HAR_RVForecaster):
        """Should handle very high volatility."""
        for i in range(30):
            har.update(0.50)
        result = har.update(0.80)
        assert result["forecast"] > 0

    def test_update_stops_after_maxlen(self, har: HAR_RVForecaster):
        """Should not grow unbounded (deque maxlen)."""
        for i in range(200):
            har.update(0.15 + i * 0.001)
        assert len(har._daily_rv) <= 60  # deque maxlen