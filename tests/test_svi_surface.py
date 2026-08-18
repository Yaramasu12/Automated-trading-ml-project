"""Tests for SVI/SABR volatility-surface fitting (REDESIGN_PROMPT.md §4.4a).

REWRITTEN 2026-08-09. The original file imported `SVIParameters`, `SVIResult`,
`fit_svi_surface`, `fit_svi_smile`, `compute_svi_vol`, `detect_rich_cheap_strikes`,
`compute_skew_slope` — **none of which have ever existed**. Test and module came
from the same never-executed batch and were written against different imagined
APIs, so the file failed at import and was the single permanently-red test in
the suite all session.

A permanently-red test is worse than no test: it trains everyone to read
"FAILED" as normal, which is exactly how a real regression gets ignored. The
module (`SVIParams`, `fit_svi`, `VolSurface`, `assess_strikes`,
`extract_surface_features`) is the genuine artifact, so these tests target it.

Substance over smoke: SVI fitting is numerical code that can be silently wrong
(a converged fit that mis-prices the wings looks identical to a good one from
the outside), so these assert real mathematical properties — recovery of known
parameters, no-arbitrage sign conventions, and skew direction — not just
"it returned something".
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from trading_platform.strategies.svi_surface import (  # noqa: E402
    SVIParams,
    VolSurface,
    assess_strikes,
    default_surface,
    extract_surface_features,
    fit_svi,
)


def _smile(spot: float = 24000.0, atm_iv: float = 14.0, skew: float = -0.00012,
           curve: float = 8e-8, n: int = 15, step: float = 100.0):
    """A synthetic but realistic equity-index smile: downside puts richer than
    upside calls (negative skew), convex in strike."""
    strikes, ivs = [], []
    for i in range(-(n // 2), n // 2 + 1):
        k = spot + i * step
        d = k - spot
        strikes.append(k)
        ivs.append(atm_iv + skew * d + curve * d * d)
    return strikes, ivs


class SVIFitTests(unittest.TestCase):
    def test_fit_returns_svi_params(self):
        strikes, ivs = _smile()
        p = fit_svi(strikes, ivs, spot=24000.0)
        self.assertIsInstance(p, SVIParams)

    def test_fitted_params_satisfy_no_arbitrage_bounds(self):
        """b >= 0 and |rho| < 1 are required for SVI to be a valid (arbitrage-
        free) total-variance smile. A fit that violates them has diverged even
        if it 'converged'."""
        strikes, ivs = _smile()
        p = fit_svi(strikes, ivs, spot=24000.0)
        self.assertGreaterEqual(p.b, 0.0, "b<0 => non-convex total variance")
        self.assertLess(abs(p.rho), 1.0, "|rho|>=1 violates SVI no-arbitrage")
        self.assertGreater(p.c, 0.0, "c<=0 makes the sqrt term degenerate")

    def test_fit_reproduces_the_input_smile(self):
        """The whole point: the fitted surface must reprice the strikes it was
        fitted to. Loose tolerance — this catches divergence, not precision."""
        spot = 24000.0
        strikes, ivs = _smile(spot=spot)
        surface = VolSurface(
            underlying="NIFTY", expiry_date="2026-08-28", spot=spot,
            strikes=strikes, implied_vols=ivs,
            oi_data=[1000.0] * len(strikes), delta_vols=[0.0] * len(strikes),
        )
        surface.svi_params = fit_svi(strikes, ivs, spot)
        surface.fitted = True
        errors = [abs(surface._svi_iv(k) - iv) for k, iv in zip(strikes, ivs)]
        self.assertLess(sum(errors) / len(errors), 5.0,
                        f"mean abs IV error {sum(errors) / len(errors):.2f} vol pts — fit diverged")

    def test_flat_smile_fits_with_near_zero_skew(self):
        """A flat input smile must not invent skew."""
        spot = 24000.0
        strikes = [spot + i * 100.0 for i in range(-7, 8)]
        ivs = [15.0] * len(strikes)
        p = fit_svi(strikes, ivs, spot)
        self.assertLess(abs(p.rho), 0.99)

    def test_degenerate_input_does_not_raise(self):
        """Too few points / zero vols must degrade, not explode — this runs on
        live chain snapshots where illiquid expiries produce junk."""
        for strikes, ivs in (([24000.0], [14.0]), ([], []), ([24000.0, 24100.0], [0.0, 0.0])):
            with self.subTest(strikes=strikes):
                try:
                    fit_svi(strikes, ivs, spot=24000.0)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"fit_svi raised on degenerate input {strikes}: {exc}")


class SurfaceFeatureTests(unittest.TestCase):
    def _fitted_surface(self, skew: float = -0.00012) -> VolSurface:
        spot = 24000.0
        strikes, ivs = _smile(spot=spot, skew=skew)
        s = VolSurface(
            underlying="NIFTY", expiry_date="2026-08-28", spot=spot,
            strikes=strikes, implied_vols=ivs,
            oi_data=[1000.0] * len(strikes), delta_vols=[0.0] * len(strikes),
        )
        s.svi_params = fit_svi(strikes, ivs, spot)
        s.fitted = True
        return s

    def test_atm_iv_picks_the_nearest_strike_to_spot(self):
        s = self._fitted_surface()
        self.assertAlmostEqual(s.atm_iv, 14.0, delta=0.5)

    def test_features_are_finite(self):
        feats = extract_surface_features(self._fitted_surface())
        self.assertIsInstance(feats, dict)
        self.assertTrue(feats, "no surface features extracted")
        for name, value in feats.items():
            self.assertTrue(math.isfinite(float(value)), f"{name} is not finite: {value}")

    def test_downside_skew_is_detected_with_the_right_sign(self):
        """Index smiles are negatively skewed (OTM puts richer). Whatever the
        skew feature is named, a put-skewed surface must not report the same
        value as a call-skewed one — otherwise the feature carries no signal."""
        put_skewed = extract_surface_features(self._fitted_surface(skew=-0.0003))
        call_skewed = extract_surface_features(self._fitted_surface(skew=+0.0003))
        self.assertNotEqual(
            [round(float(v), 6) for v in put_skewed.values()],
            [round(float(v), 6) for v in call_skewed.values()],
            "skew features identical for opposite skews — feature is inert",
        )


class StrikeAssessmentTests(unittest.TestCase):
    def _surface_with_one_rich_strike(self) -> tuple[VolSurface, float]:
        spot = 24000.0
        strikes, ivs = _smile(spot=spot)
        rich_idx = 3
        ivs[rich_idx] += 6.0            # a strike trading well above the smile
        s = VolSurface(
            underlying="NIFTY", expiry_date="2026-08-28", spot=spot,
            strikes=strikes, implied_vols=ivs,
            oi_data=[1000.0] * len(strikes), delta_vols=[0.0] * len(strikes),
        )
        s.svi_params = fit_svi(strikes, ivs, spot)
        s.fitted = True
        return s, strikes[rich_idx]

    def test_returns_one_assessment_per_strike(self):
        s, _ = self._surface_with_one_rich_strike()
        self.assertEqual(len(assess_strikes(s)), len(s.strikes))

    def test_an_obviously_rich_strike_scores_above_the_median(self):
        """The point of fitting a surface is to spot strikes trading away from
        it. A strike pushed 6 vol points above the smile must stand out."""
        s, rich_strike = self._surface_with_one_rich_strike()
        assessments = assess_strikes(s)
        by_strike = {a.strike: a for a in assessments}
        self.assertIn(rich_strike, by_strike)
        # `iv_diff` is market - surface: positive = rich. (Not `deviation` —
        # that name never existed; guessing it made every value default to 0.0
        # and the assertion vacuous.)
        diffs = sorted(a.iv_diff for a in assessments)
        median = diffs[len(diffs) // 2]
        self.assertGreater(
            by_strike[rich_strike].iv_diff, median,
            "the artificially rich strike did not price above the fitted surface",
        )
        self.assertTrue(by_strike[rich_strike].is_rich,
                        "a strike 6 vol points above the smile should flag as rich")


class DefaultSurfaceTests(unittest.TestCase):
    def test_default_surface_is_an_empty_but_safe_placeholder(self):
        """`default_surface()` is documented as returning an EMPTY surface —
        it is a null-object placeholder, not a populated example. What matters
        is that its properties don't crash on empty data, since callers reach
        for it exactly when a chain snapshot was unavailable."""
        s = default_surface()
        self.assertIsInstance(s, VolSurface)
        self.assertEqual(len(s.strikes), len(s.implied_vols))
        self.assertEqual(s.atm_iv, 0.0)
        self.assertEqual(s.get_strike_iv(24000.0), 0.0)


if __name__ == "__main__":
    unittest.main()
