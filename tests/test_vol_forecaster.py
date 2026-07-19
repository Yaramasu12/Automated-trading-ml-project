"""Tests for the GARCH volatility forecaster that feeds the short-vol condor VRP.

Volatility (unlike returns) is genuinely forecastable, so on a volatility-
clustered series GARCH should beat the naive trailing-realized baseline. The
forecaster is only trusted when it demonstrably does — the 'earn deployment' gate."""
from __future__ import annotations

import math
import unittest

import numpy as np

from trading_platform.neural.vol_forecaster import VolatilityForecaster
from trading_platform.strategies.short_vol import ShortVolStrategy


def _garch_series(n=600, seed=7):
    rng = np.random.default_rng(seed)
    var, rets = 0.0001, []
    for _ in range(n):
        var = 0.000002 + 0.12 * (rets[-1] ** 2 if rets else 0) + 0.86 * var
        rets.append(float(rng.normal(0, math.sqrt(var))))
    return [24000 * math.exp(sum(rets[: i + 1])) for i in range(n)]


class VolForecasterTests(unittest.TestCase):
    def setUp(self):
        self.vf = VolatilityForecaster()

    def test_forecast_reasonable(self):
        v = self.vf.forecast_pct(_garch_series())
        self.assertTrue(3.0 < v < 100.0, v)

    def test_forecast_zero_when_insufficient_data(self):
        self.assertEqual(self.vf.forecast_pct([24000, 24010, 24005]), 0.0)

    def test_garch_beats_naive_on_clustered_vol(self):
        v = self.vf.validate_beats_naive(_garch_series(), horizon=5, window=20)
        self.assertGreater(v.n, 100)
        self.assertTrue(v.beats_naive, f"naive={v.naive_rmse:.2f} garch={v.garch_rmse:.2f}")

    def test_validation_needs_enough_samples(self):
        v = self.vf.validate_beats_naive([24000, 24010, 24020, 24015], horizon=5, window=20)
        self.assertFalse(v.beats_naive)  # too little data -> not trusted


class VrpForecastWiringTests(unittest.TestCase):
    """The strategy must use the forecast as the VRP reference when supplied."""

    def test_forecast_overrides_trailing_realized(self):
        s = ShortVolStrategy()
        closes = _garch_series(n=60)
        vix = 16.0
        # With a supplied forecast of 10%, VRP = 16 - 10 = 6
        self.assertAlmostEqual(s.vrp(vix, closes, forecast_vol=10.0), 6.0, places=6)
        # Without a forecast it falls back to trailing realized (!= 6 in general)
        self.assertNotEqual(round(s.vrp(vix, closes), 4), 6.0)

    def test_zero_forecast_falls_back(self):
        s = ShortVolStrategy()
        closes = _garch_series(n=60)
        self.assertEqual(s.vrp(16.0, closes, forecast_vol=0.0),
                         s.vrp(16.0, closes))


if __name__ == "__main__":
    unittest.main()
