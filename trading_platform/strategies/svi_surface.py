"""
trading_platform/strategies/svi_surface.py — SVI/SABR vol-surface fitting

Per §4.4a (REDESIGN_PROMPT): Fit a volatility surface per expiry from chain snapshots.
Rich/cheap strikes vs the fitted surface → better strike selection for condors/strangles.
Skew and term-structure slopes as regime features.

Uses scipy-based optimization (free, local). No paid dependencies.

References:
  - SVI: Gatheral & Wilmott, "The Volatility Surface"
  - SABR: Hagan et al. (2002)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy.optimize import minimize
from scipy import stats

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Vol surface models
# ──────────────────────────────────────────────


@dataclass
class SVIParams:
    """SVI model parameters.
    
    V(k) = a + b * [rho * (k - m) + sqrt((k - m)^2 + c)]
    where:
        V(k) = total variance at log-moneyness k
        a: asymptotic variance
        b: total variance scale
        rho: correlation (skew parameter, -1 < rho < 1)
        m: center of the smile
        c: width parameter
    """
    a: float = 0.04  # 2% vol²
    b: float = 0.02
    rho: float = -0.3
    m: float = 0.0
    c: float = 0.01


@dataclass
class SABRParams:
    """SABR model parameters.
    
    σ(K, T) = (F^(β) / (1 + β * K^β)) * 
              ((1 / F^(1-β)) * (1 / K^(1-β))) *
              log(F/K) / sqrt((F*K)^(2β) * (1 - ρ)² * log²(F/K) + α²)
    
    α: volatility of volatility
    β: elasticity parameter (0 = normal, 1 = Black-Scholes)
    ρ: correlation between spot and vol
    ν: vol-of-vol
    """
    alpha: float = 0.3
    beta: float = 0.5
    rho: float = -0.3
    nu: float = 0.4


@dataclass
class VolSurface:
    """Reconstructed volatility surface from chain snapshots."""
    underlying: str
    expiry_date: str
    spot: float
    strikes: List[float]
    implied_vols: List[float]
    oi_data: List[float]  # Open interest per strike
    delta_vols: List[float]  # ΔOI per strike
    svi_params: Optional[SVIParams] = None
    sab_params: Optional[SABRParams] = None
    fitted: bool = False
    skew_slope: float = 0.0  # First derivative of vol w.r.t. moneyness
    term_structure: float = 0.0  # Term structure slope (if multi-expiry)

    @property
    def atm_iv(self) -> float:
        """ATM implied volatility."""
        if not self.strikes:
            return 0.0
        idx = int(np.argmin(np.abs(np.array(self.strikes) - self.spot)))
        return self.implied_vols[idx]

    @property
    def moneyness(self) -> np.ndarray:
        """Log-moneyness relative to ATM."""
        strikes = np.array(self.strikes)
        return np.log(strikes / self.spot)

    def get_strike_iv(self, strike: float) -> float:
        """Get implied vol for a specific strike (fitted or interpolated)."""
        if self.svi_params and self.fitted:
            return self._svi_iv(strike)
        
        strikes = np.array(self.strikes)
        vols = np.array(self.implied_vols)
        
        if strike in strikes:
            idx = np.where(strikes == strike)[0][0]
            return float(vols[idx])
        
        # Linear interpolation
        return float(np.interp(strike, strikes, vols))

    def get_moneyness_iv(self, moneyness: float) -> float:
        """Get implied vol for a specific log-moneyness."""
        if self.svi_params and self.fitted:
            k = np.array([moneyness])
            return self._svi_total_var(k) ** 0.5
        
        return self.atm_iv

    def _svi_total_var(self, k: np.ndarray) -> float:
        """Compute SVI total variance."""
        p = self.svi_params
        if p is None:
            return 0.0
        return p.a + p.b * (p.rho * (k - p.m) + np.sqrt((k - p.m) ** 2 + p.c))

    def _svi_iv(self, strike: float) -> float:
        """Compute implied vol from SVI params for a strike."""
        k = np.log(strike / self.spot)
        total_var = self._svi_total_var(np.array([k]))
        # Simplified: assume 30 days to expiry
        t = 30 / 365.0
        if total_var <= 0 or t <= 0:
            return self.atm_iv
        return float(np.sqrt(total_var / t) * 100)

    def compute_skew(self) -> float:
        """Compute skew as vol change between 0.95 and 1.05 moneyness."""
        k_otm_put = np.log(0.95)
        k_otm_call = np.log(1.05)
        iv_put = self.get_moneyness_iv(k_otm_put) if self.fitted else self._interp_iv(k_otm_put)
        iv_call = self.get_moneyness_iv(k_otm_call) if self.fitted else self._interp_iv(k_otm_call)
        self.skew_slope = (iv_call - iv_put) / (k_otm_call - k_otm_put)
        return self.skew_slope

    def _interp_iv(self, moneyness: float) -> float:
        """Fallback linear interpolation."""
        m = self.moneyness
        v = np.array(self.implied_vols)
        if len(m) < 2:
            return self.atm_iv
        return float(np.interp(moneyness, m, v))


# ──────────────────────────────────────────────
# SVI fitting
# ──────────────────────────────────────────────


def fit_svi(
    strikes: List[float],
    ivs: List[float],
    spot: float,
    initial_params: Optional[SVIParams] = None,
    max_iter: int = 1000,
) -> SVIParams:
    """
    Fit SVI parameters to observed IVs.
    
    Minimizes: sum((iv_model(k) - iv_observed(k))²)
    
    Args:
        strikes: Option strikes
        ivs: Observed implied volatilities (in percent)
        spot: Underlying spot price
        initial_params: Starting values (default: conservative)
        max_iter: Max optimization iterations
    
    Returns:
        Fitted SVIParams
    """
    if not strikes or len(strikes) < 3:
        return initial_params or SVIParams()

    log_moneyness = np.array([np.log(K / spot) for K in strikes])
    vols = np.array([v / 100.0 for v in ivs])  # Convert to decimal

    params = initial_params or SVIParams()
    x0 = [params.a, params.b, params.rho, params.m, params.c]

    # Bounds: (a > 0, b > 0, -1 < rho < 1, m free, c > 0)
    bounds = [(1e-6, 0.5), (1e-6, 0.5), (-0.99, 0.99), (-0.5, 0.5), (1e-6, 0.1)]

    def objective(p):
        a, b, rho, m, c = p
        total_var = a + b * (rho * log_moneyness + np.sqrt(log_moneyness ** 2 + c))
        # Variance must be positive
        total_var = np.maximum(total_var, 1e-6)
        model_vols = np.sqrt(total_var)
        return np.sum((model_vols - vols) ** 2)

    result = minimize(
        objective, x0, method='L-BFGS-B', bounds=bounds,
        options={'maxiter': max_iter, 'ftol': 1e-12}
    )

    if not result.success:
        logger.warning(f"SVI fit failed: {result.message}")

    fitted = SVIParams(
        a=max(result.x[0], 1e-6),
        b=max(result.x[1], 1e-6),
        rho=np.clip(result.x[2], -0.99, 0.99),
        m=result.x[3],
        c=max(result.x[4], 1e-6),
    )

    return fitted


# ──────────────────────────────────────────────
# SABR fitting
# ──────────────────────────────────────────────


def fit_sabrs(
    strikes: List[float],
    ivs: List[float],
    spot: float,
    time_to_expiry: float = 30 / 365.0,
    initial_params: Optional[SABRParams] = None,
    max_iter: int = 500,
) -> SABRParams:
    """
    Fit SABR parameters to observed IVs.
    
    Uses Hagan's asymptotic formula for Black-Scholes vol.
    
    Args:
        strikes: Option strikes
        ivs: Observed implied volatilities (in percent)
        spot: Forward/spot price
        time_to_expiry: Time to expiry in years
        initial_params: Starting values
        max_iter: Max optimization iterations
    
    Returns:
        Fitted SABRParams
    """
    if not strikes or len(strikes) < 4:
        return initial_params or SABRParams()

    vols = np.array([v / 100.0 for v in ivs])
    K = np.array(strikes)

    params = initial_params or SABRParams()
    x0 = [params.alpha, params.beta, params.rho, params.nu]

    # Bounds
    bounds = [(1e-6, 1.0), (0.0, 1.0), (-0.99, 0.99), (1e-6, 2.0)]

    def sabr_vol(K_arr):
        """Hagan SABR formula."""
        beta = x0[1]
        f = spot ** beta
        k = K_arr ** beta
        fk = f * k
        log_fk = np.log(f / k) + 1e-10
        term1 = f / (1 + beta * k)
        term2 = f ** (1 - beta) / k ** (1 - beta)
        denom = np.sqrt(fk ** (1 - beta) * (1 - x0[2]) ** 2 * log_fk ** 2 + x0[0] ** 2)
        return term1 * term2 * np.log(f / k) / denom

    def objective(p):
        alpha, beta, rho, nu = p
        if alpha <= 0 or nu <= 0 or beta <= 0 or beta >= 1:
            return 1e10
        model_vols = sabr_vol(K)
        # Add vol-of-vol adjustment (simplified)
        adjusted = model_vols * (1 + nu * time_to_expiry * log_fk / 24.0)
        return np.sum((adjusted - vols) ** 2)

    result = minimize(
        objective, x0, method='L-BFGS-B', bounds=bounds,
        options={'maxiter': max_iter, 'ftol': 1e-10}
    )

    if not result.success:
        logger.warning(f"SABR fit failed: {result.message}")

    fitted = SABRParams(
        alpha=max(result.x[0], 1e-6),
        beta=np.clip(result.x[1], 0.01, 0.99),
        rho=np.clip(result.x[2], -0.99, 0.99),
        nu=max(result.x[3], 1e-6),
    )

    return fitted


# ──────────────────────────────────────────────
# Strike rich/cheap identification
# ──────────────────────────────────────────────


@dataclass
class StrikeAssessment:
    """Assessment of a single strike's richness/cheapness."""
    strike: float
    moneyness: float
    market_iv: float
    surface_iv: float
    iv_diff: float  # market - surface (positive = rich, negative = cheap)
    z_score: float  # How many std devs from mean
    is_rich: bool  # IV > surface by threshold
    is_cheap: bool  # IV < surface by threshold
    recommended_action: str  # "sell", "buy", "neutral"


def assess_strikes(
    surface: VolSurface,
    rich_threshold: float = 1.0,  # std devs above surface
    cheap_threshold: float = -1.0,  # std devs below surface
) -> List[StrikeAssessment]:
    """
    Identify rich/cheap strikes vs the fitted vol surface.
    
    Per §4.4a: Rich/cheap strikes vs the fitted surface → better strike selection.
    
    Args:
        surface: Fitted VolSurface
        rich_threshold: Z-score threshold for "rich"
        cheap_threshold: Z-score threshold for "cheap"
    
    Returns:
        List of StrikeAssessment per strike
    """
    if not surface.fitted or not surface.svi_params:
        logger.warning("Cannot assess strikes: SVI not fitted")
        return []

    strikes = np.array(surface.strikes)
    market_ivs = np.array(surface.implied_vols)
    surface_ivs = np.array([surface._svi_iv(K) for K in strikes])
    iv_diffs = market_ivs - surface_ivs

    # Compute z-scores from IV differences
    if len(iv_diffs) > 1:
        std_diff = np.std(iv_diffs)
        z_scores = (iv_diffs - np.mean(iv_diffs)) / std_diff if std_diff > 0 else np.zeros_like(iv_diffs)
    else:
        z_scores = np.zeros_like(iv_diffs)

    assessments = []
    for i, K in enumerate(strikes):
        moneyness = np.log(K / surface.spot)
        is_rich = z_scores[i] > rich_threshold
        is_cheap = z_scores[i] < cheap_threshold

        if is_rich:
            action = "sell"  # Rich → sell premium
        elif is_cheap:
            action = "buy"  # Cheap → buy protection
        else:
            action = "neutral"

        assessments.append(StrikeAssessment(
            strike=K,
            moneyness=moneyness,
            market_iv=float(market_ivs[i]),
            surface_iv=float(surface_ivs[i]),
            iv_diff=float(iv_diffs[i]),
            z_score=float(z_scores[i]),
            is_rich=is_rich,
            is_cheap=is_cheap,
            recommended_action=action,
        ))

    return assessments


# ──────────────────────────────────────────────
# Surface feature extraction for regime features
# ──────────────────────────────────────────────


def extract_surface_features(
    surface: VolSurface,
) -> Dict[str, float]:
    """
    Extract surface features for regime detection and feature store.
    
    Per §3.1: Skew and term-structure slopes as regime features.
    
    Returns:
        Dict of feature name → value
    """
    features = {}
    
    # ATM IV
    features["atm_iv"] = surface.atm_iv
    
    # Skew (computed if not already)
    if surface.skew_slope == 0.0:
        surface.compute_skew()
    features["skew_slope"] = surface.skew_slope
    
    # Skew magnitude (absolute)
    features["skew_magnitude"] = abs(surface.skew_slope)
    
    # IV rank relative to surface
    if surface.implied_vols:
        features["iv_min"] = min(surface.implied_vols)
        features["iv_max"] = max(surface.implied_vols)
        features["iv_range"] = features["iv_max"] - features["iv_min"]
        features["iv_std"] = float(np.std(surface.implied_vols))
    
    # OI-weighted metrics
    if surface.oi_data and sum(surface.oi_data) > 0:
        oi_arr = np.array(surface.oi_data, dtype=float)
        features["max_oi_strike"] = surface.strikes[int(np.argmax(oi_arr))]
        features["total_oi"] = float(np.sum(oi_arr))
    
    # ΔOI velocity
    if surface.delta_vols:
        features["oi_velocity"] = float(np.sum(np.abs(surface.delta_vols)))
        features["put_oi_velocity"] = float(np.sum([d for d in surface.delta_vols if d < 0]))
        features["call_oi_velocity"] = float(np.sum([d for d in surface.delta_vols if d > 0]))
    
    # Put/Call OI ratio
    n = len(surface.strikes)
    if n > 0:
        mid = n // 2
        put_oi = sum(surface.oi_data[:mid]) if mid > 0 else 0
        call_oi = sum(surface.oi_data[mid:]) if mid < n else 0
        features["pc_oi_ratio"] = put_oi / call_oi if call_oi > 0 else 0.0
    
    return features


# ──────────────────────────────────────────────
# Surface fitting pipeline
# ──────────────────────────────────────────────


def fit_surface_from_chain(
    underlying: str,
    expiry_date: str,
    spot: float,
    chain_snapshots: List[Dict[str, Any]],
    model: str = "svi",
) -> VolSurface:
    """
    Fit a vol surface from option chain snapshots.
    
    Args:
        underlying: Underlying symbol (NIFTY, BANKNIFTY, etc.)
        expiry_date: Expiry date string
        spot: Current spot price
        chain_snapshots: List of chain snapshot dicts with "strike", "iv", "oi" keys
        model: "svi" or "sabr"
    
    Returns:
        Fitted VolSurface
    """
    if not chain_snapshots:
        return VolSurface(
            underlying=underlying,
            expiry_date=expiry_date,
            spot=spot,
            strikes=[],
            implied_vols=[],
            oi_data=[],
            delta_vols=[],
        )

    # Aggregate: use latest IV from most recent snapshot per strike
    strike_iv_map: Dict[float, List[float]] = {}
    strike_oi_map: Dict[float, List[float]] = {}
    for snap in chain_snapshots:
        for opt in snap.get("options", []):
            K = opt.get("strike", 0.0)
            iv = opt.get("iv", 0.0)
            oi = opt.get("oi", 0.0)
            if K not in strike_iv_map:
                strike_iv_map[K] = []
                strike_oi_map[K] = []
            strike_iv_map[K].append(iv)
            strike_oi_map[K].append(oi)

    strikes = sorted(strike_iv_map.keys())
    ivs = [np.mean(strike_iv_map[K]) for K in strikes]
    oi_data = [np.mean(strike_oi_map[K]) for K in strikes]
    
    # Compute ΔOI (difference between last and mean)
    delta_vols = [strike_oi_map[K][-1] - np.mean(strike_oi_map[K]) for K in strikes]

    surface = VolSurface(
        underlying=underlying,
        expiry_date=expiry_date,
        spot=spot,
        strikes=strikes,
        implied_vols=ivs,
        oi_data=oi_data,
        delta_vols=delta_vols,
    )

    # Fit model
    if model == "svi":
        surface.svi_params = fit_svi(strikes, ivs, spot)
    elif model == "sabr":
        surface.sab_params = fit_sabrs(strikes, ivs, spot)
    
    surface.fitted = True
    surface.compute_skew()

    return surface


# ──────────────────────────────────────────────
# Module-level convenience
# ──────────────────────────────────────────────


def default_surface() -> VolSurface:
    """Return a default empty surface."""
    return VolSurface(
        underlying="",
        expiry_date="",
        spot=0.0,
        strikes=[],
        implied_vols=[],
        oi_data=[],
        delta_vols=[],
    )