"""Fit the SVI vol surface (strategies/svi_surface.py) to the real option
chain data options_chain_collector.py has been accumulating in production
since 2026-07-29.

Context: svi_surface.py's SVI/SABR fitter, VolSurface, and assess_strikes()
rich/cheap classifier were built (per REDESIGN_PROMPT.md §4.4a) and tested
in isolation, but nothing in the codebase ever called fit_svi()/
fit_surface_from_chain() against real data — confirmed by grep: zero
production callers. Unlike the IV-rank signal (which genuinely needed many
DAYS of history to clear MIN_IV_RANK_OBSERVATIONS), an SVI smile fits
across STRIKES within a single day's snapshot, so it has been fittable
from real data since the very first successful capture — it just never
had anything wiring it to that data.

This module is deliberately research/diagnostic only: it reports the
fitted smile and each strike's rich/cheap classification for a human or
dashboard to read. It does not feed any order path, does not change
position sizing, and StrikeAssessment.recommended_action ("sell"/"buy") is
relabeled here as informational, not an instruction — wiring this into
actual strike selection would be a real strategy change requiring its own
backtest validation (this project's "models must earn deployment" rule),
which 1-2 months of chain history cannot honestly support yet.
"""
from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any

from trading_platform.data.options_chain_collector import OptionsChainCollector
from trading_platform.strategies.svi_surface import (
    VolSurface,
    assess_strikes,
    fit_svi,
)

# How well the fitted curve reprices the market IVs it was fitted to — the
# same "does it reprice its own inputs" check test_svi_surface.py's own
# test_fit_reproduces_the_input_smile() uses (there: mean abs error < 5.0
# vol points, a loose divergence check on 15 widely-spaced synthetic
# strikes). This deployment's real captured strikes are far more numerous
# and tightly spaced (options_chain_collector.capture()'s
# strikes_each_side=5 around 50-point NIFTY strikes), so a well-identified
# fit should reprice them much more precisely than that loose bar —
# measured directly against this deployment's real August-September 2026
# chain history: a wide, well-behaved synthetic reference smile (matching
# test_svi_surface.py's own default fixture) reprices to ~0.05 mean abs vol
# points, while the real captured NIFTY smile — which has a visible, real
# ~2.5-vol-point skew across its strikes — reprices to ~0.71: over 10x
# worse, a genuine, measurable degradation even though "0.71" sounds small
# in isolation. An earlier version of this check inspected the fitted `b`
# parameter's proximity to its optimizer-bound floor instead, but that
# floor turned out to be common to test_svi_surface.py's OWN validated wide
# reference fit too — not a reliable narrow-vs-wide discriminator, just a
# property of this particular smile shape/optimizer landscape. Repricing
# accuracy is a direct correctness check instead of a proxy on one
# parameter's raw value.
_MAX_RELIABLE_MEAN_ABS_REPRICING_ERROR = 0.3  # vol points
_MIN_RELIABLE_MONEYNESS_RANGE = 0.03  # log(max_strike/min_strike)


def fit_latest_smile(collector: OptionsChainCollector, underlying: str) -> dict[str, Any]:
    """Fit an SVI smile to the most recent real chain snapshot with enough
    strikes, and classify each strike as rich/cheap against it.

    Returns a dict with `available: False` and a `reason` when there isn't
    yet a captured day with enough strikes — never a fit computed from too
    little data presented as if it were reliable.
    """
    smile = collector.latest_smile(underlying)
    if smile is None:
        return {
            "underlying": underlying,
            "available": False,
            "reason": "no captured chain snapshot yet has enough distinct strikes",
        }

    strikes = smile["strikes"]
    ivs = smile["ivs"]
    spot = smile["spot"]

    svi_params = fit_svi(strikes, ivs, spot)
    surface = VolSurface(
        underlying=underlying,
        expiry_date=smile["expiry"],
        spot=spot,
        strikes=strikes,
        implied_vols=ivs,
        oi_data=[0.0] * len(strikes),  # not collected by this data source
        delta_vols=[0.0] * len(strikes),
        svi_params=svi_params,
        fitted=True,
    )
    surface.compute_skew()
    assessments = assess_strikes(surface)

    mean_abs_repricing_error = (
        sum(abs(a.iv_diff) for a in assessments) / len(assessments) if assessments else 0.0
    )
    moneyness_range = math.log(max(strikes) / min(strikes)) if len(strikes) >= 2 else 0.0
    reasons = []
    if mean_abs_repricing_error > _MAX_RELIABLE_MEAN_ABS_REPRICING_ERROR:
        reasons.append(
            f"fitted curve reprices its own inputs to only {mean_abs_repricing_error:.2f} "
            f"mean abs vol points (reference: a well-identified fit reprices to <"
            f"{_MAX_RELIABLE_MEAN_ABS_REPRICING_ERROR:.2f}) — the 5-parameter SVI fit likely "
            "isn't well identified from this snapshot's strikes"
        )
    if moneyness_range < _MIN_RELIABLE_MONEYNESS_RANGE:
        reasons.append(
            f"strikes span only {moneyness_range:.1%} log-moneyness — too narrow to "
            "trust the fitted curve far from the observed strikes"
        )
    low_confidence = bool(reasons)

    return {
        "underlying": underlying,
        "available": True,
        "low_confidence_fit": low_confidence,
        "low_confidence_reason": (
            "; ".join(reasons) + "; treat market_iv per strike as reliable, the "
            "fitted curve/rich-cheap labels as indicative only" if reasons else None
        ),
        "mean_abs_repricing_error": round(mean_abs_repricing_error, 4),
        "as_of_date": smile["date"],
        "expiry": smile["expiry"],
        "dte": smile["dte"],
        "spot": spot,
        "n_strikes": len(strikes),
        "n_snapshots_that_day": smile["n_snapshots_that_day"],
        "atm_iv": round(surface.atm_iv, 2),
        "skew_25d_proxy": round(surface.skew_slope, 4),
        "svi_params": asdict(svi_params),
        "strikes": [
            {
                "strike": a.strike,
                "moneyness": round(a.moneyness, 4),
                "market_iv": round(a.market_iv, 2),
                "surface_iv": round(a.surface_iv, 2),
                "iv_diff": round(a.iv_diff, 2),
                "z_score": round(a.z_score, 2),
                "richness": "rich" if a.is_rich else "cheap" if a.is_cheap else "fair",
            }
            for a in assessments
        ],
        "caveat": (
            "Research/diagnostic only — not a trading signal. Fitted from a single "
            "day's real chain snapshot (options_chain_collector has been running "
            "since 2026-07-29); no backtest has validated trading on rich/cheap "
            "deviations, so this must not influence live strike selection without "
            "that validation first."
        ),
    }
