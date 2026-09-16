"""Tests for research/vol_surface_diagnostics.py — bridges the real, live
option-chain history options_chain_collector.py has been accumulating in
production since 2026-07-29 to strategies/svi_surface.py's SVI fitter,
which had zero production callers until this (2026-09-16, Section 11 audit
of the implementation prompt).

Found while wiring it up: fit_svi's own 5-parameter SVI model is not well
identified across the moneyness range options_chain_collector.capture()'s
default strikes_each_side=5 actually captures for NIFTY/BANKNIFTY in this
deployment's real history (under ~2% log-moneyness either side) — the
optimizer lands with `b` pinned near its lower bound despite a real,
visible skew in the underlying market IVs. test_svi_surface.py's own
synthetic smile uses a much wider range (n=15, step=100 around a 24000
spot, ~±3% moneyness) and was never exercised against anything this
narrow. These tests guard the honesty check added to flag that case,
using the same synthetic-smile pattern test_svi_surface.py already
established (wide vs. narrow), so this doesn't require live data or
network access.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from trading_platform.data.options_chain_collector import OptionsChainCollector
from trading_platform.research.vol_surface_diagnostics import fit_latest_smile


def _synthetic_rows(
    date: str, spot: float, atm_iv: float, n: int, step: float,
    skew: float = -0.00012, curve: float = 8e-8,
) -> list[dict]:
    """Identical smile formula to test_svi_surface.py's own `_smile()` — a
    realistic negative-skew, convex equity-index smile — as flat CSV rows
    matching what OptionsChainCollector actually persists (iv as a decimal
    fraction, not vol points)."""
    rows = []
    for i in range(-(n // 2), n // 2 + 1):
        strike = spot + i * step
        d = strike - spot
        iv_points = atm_iv + skew * d + curve * d * d
        rows.append({
            "date": date, "underlying": "NIFTY", "expiry": "2100-01-01", "dte": 10,
            "option_type": "CE", "strike": strike, "spot": spot, "ltp": 100.0,
            "iv": iv_points / 100.0,  # vol points -> decimal fraction, as stored
            "delta": 0.5,
        })
    return rows


class VolSurfaceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmpdir.name)
        self.collector = OptionsChainCollector(SimpleNamespace(), out_dir=str(self.out_dir))

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_unavailable_when_no_qualifying_snapshot_exists(self):
        result = fit_latest_smile(self.collector, "NIFTY")

        self.assertFalse(result["available"])
        self.assertIn("reason", result)

    def test_wide_realistic_range_fits_with_high_confidence(self):
        # Mirrors test_svi_surface.py's own synthetic smile width (~±3% moneyness).
        rows = _synthetic_rows("2026-08-01", spot=24000.0, atm_iv=14.0, n=15, step=100.0)
        self.collector._append_csv("NIFTY", rows)

        result = fit_latest_smile(self.collector, "NIFTY")

        self.assertTrue(result["available"])
        self.assertFalse(result["low_confidence_fit"])
        self.assertIsNone(result["low_confidence_reason"])
        self.assertAlmostEqual(result["atm_iv"], 14.0, delta=1.0)

    def test_narrow_real_world_range_is_flagged_low_confidence(self):
        # Mirrors this deployment's real NIFTY capture width (strikes_each_side=5,
        # 50-point strikes around a ~23000-24000 spot => under 2% moneyness).
        rows = _synthetic_rows("2026-08-01", spot=24000.0, atm_iv=14.0, n=10, step=50.0)
        self.collector._append_csv("NIFTY", rows)

        result = fit_latest_smile(self.collector, "NIFTY")

        self.assertTrue(result["available"])
        self.assertTrue(result["low_confidence_fit"])
        self.assertIsNotNone(result["low_confidence_reason"])

    def test_response_always_carries_the_research_only_caveat(self):
        rows = _synthetic_rows("2026-08-01", spot=24000.0, atm_iv=14.0, n=15, step=100.0)
        self.collector._append_csv("NIFTY", rows)

        result = fit_latest_smile(self.collector, "NIFTY")

        self.assertIn("not a trading signal", result["caveat"])

    def test_per_strike_richness_labels_are_present(self):
        rows = _synthetic_rows("2026-08-01", spot=24000.0, atm_iv=14.0, n=15, step=100.0)
        self.collector._append_csv("NIFTY", rows)

        result = fit_latest_smile(self.collector, "NIFTY")

        self.assertEqual(len(result["strikes"]), 15)
        for row in result["strikes"]:
            self.assertIn(row["richness"], {"rich", "cheap", "fair"})


if __name__ == "__main__":
    unittest.main()
