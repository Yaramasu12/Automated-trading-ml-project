"""
Fractional differentiation of price series — López de Prado AFML suite.

Provides stationarity without memory loss, as described in "Advances in
Financial Machine Learning" (López de Prado, 2018), Chapter 6.

Unlike differencing (which loses all long-memory) or raw prices (which
are non-stationary), fractional d ∈ (0, 1) preserves maximal signal
while achieving approximate stationarity.

Design principles:
- Pure NumPy implementation — no external dependency beyond scipy (optional)
- Vectorized for batch processing
- Used as a feature transform before supervised learning (§4.4b)
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from scipy import special

logger = logging.getLogger(__name__)


def _binomial_coefficient(a: float, k: int) -> np.ndarray:
    """
    Compute binomial coefficients C(a, k) for fractional a.
    C(a, k) = a * (a-1) * ... * (a-k+1) / k!
    """
    coeffs = np.ones(k + 1)
    for i in range(1, k + 1):
        coeffs[i] = coeffs[i - 1] * (a - i + 1) / i
    return coeffs


def compute_weights(
    d: float,
    max_lag: int = 500,
    tol: float = 1e-10,
) -> np.ndarray:
    """
    Compute fractional differentiation weights.

    The weights w_k = C(d, k) decay as k^(-d-1), ensuring long-memory
    is preserved while the series becomes approximately stationary.

    Args:
        d: Fractional differentiation order (0 < d < 1)
        max_lag: Maximum lag for weight truncation
        tol: Convergence tolerance — stop when |w_k| < tol

    Returns:
        Array of weights [w_0, w_1, ..., w_{max_lag-1}]
    """
    if not (0 < d < 1):
        raise ValueError(f"d must be in (0, 1), got {d}")

    weights = _binomial_coefficient(d, max_lag)

    # Truncate small weights
    active = np.where(np.abs(weights) > tol)[0]
    if len(active) == 0:
        return np.array([1.0])

    return weights[: active[-1] + 1]


def fractional_diff(
    prices: np.ndarray,
    d: float,
    max_lag: int = 500,
    mode: str = "direct",
) -> np.ndarray:
    """
    Apply fractional differentiation to a price series.

    The result is approximately stationary while preserving maximal
    long-memory structure — unlike plain differencing which destroys
    all signal beyond lag 1.

    Args:
        prices: 1D array of prices (OHLCV close, or any price-like series)
        d: Fractional differentiation order (0 = no diff, 1 = plain diff,
           0.5 = half-diff — the sweet spot per López de Prado)
        max_lag: Maximum lag for weight truncation
        mode: "direct" (full convolution) or "truncated" (fast, limited lag)

    Returns:
        Fractionally differentiated series (same length as input,
        with NaN prefix for the first max_lag-1 elements)
    """
    if len(prices) < 2:
        return prices

    prices = np.asarray(prices, dtype=np.float64)

    if d == 0.0:
        return prices

    if d == 1.0:
        # Plain differencing
        diffed = np.empty_like(prices)
        diffed[0] = np.nan
        diffed[1:] = prices[1:] - prices[:-1]
        return diffed

    # Compute weights
    weights = compute_weights(d, max_lag)
    w_len = len(weights)

    # Work on log prices (more stable for financial data)
    log_prices = np.log(np.maximum(prices, 1e-10))

    if mode == "direct":
        # Full convolution
        fd = np.full_like(log_prices, np.nan, dtype=np.float64)
        for i in range(w_len - 1, len(log_prices)):
            fd[i] = np.sum(weights * log_prices[i - w_len + 1 : i + 1][::-1])
        return fd

    elif mode == "truncated":
        # Fast truncated version — only use first `trunc` weights
        trunc = min(w_len, max(10, len(prices) // 4))
        w_trunc = weights[:trunc][::-1]
        fd = np.full_like(log_prices, np.nan, dtype=np.float64)

        for i in range(trunc - 1, len(log_prices)):
            fd[i] = np.sum(w_trunc * log_prices[i - trunc + 1 : i + 1])
        return fd

    else:
        raise ValueError(f"Unknown mode: {mode}. Use 'direct' or 'truncated'.")


def fractional_diff_return(
    prices: np.ndarray,
    d: float = 0.5,
    max_lag: int = 500,
) -> np.ndarray:
    """
    Convenience function: fractional diff of log prices, then annualize.

    Returns the fractionally differentiated returns:
        fd_return = exp(fd) - 1 ≈ log(fd) for small values

    This is the primary feature transform used in meta-labeling (§4.3).
    """
    fd = fractional_diff(prices, d, max_lag)
    # Convert back to return space
    fd_returns = np.empty_like(fd)
    fd_returns[0] = np.nan
    fd_returns[1:] = np.exp(fd[1:] - np.roll(fd, 1)[1:]) - 1
    return fd_returns


def find_optimal_d(
    prices: np.ndarray,
    d_range: tuple = (0.05, 0.95),
    steps: int = 19,
    target_hurst: float = 0.5,
) -> float:
    """
    Search for the d that makes the series closest to Hurst = 0.5
    (Brownian motion = maximum randomness = stationarity).

    Uses Rescaled Range (R/S) analysis to estimate Hurst exponent.

    Args:
        prices: Price series
        d_range: (min_d, max_d) to search
        steps: Number of d values to try
        target_hurst: Target Hurst exponent (0.5 = stationary)

    Returns:
        Optimal d value
    """
    from scipy.stats import linregress

    d_values = np.linspace(d_range[0], d_range[1], steps)
    hurst_scores: list[tuple[float, float]] = []

    for d in d_values:
        fd = fractional_diff(prices, d, max_lag=min(200, len(prices) // 4))
        fd = fd[~np.isnan(fd)]

        if len(fd) < 100:
            continue

        # R/S analysis — split into blocks
        n = len(fd)
        block_size = max(10, n // 20)
        n_blocks = n // block_size

        means = np.mean(fd[: n_blocks * block_size].reshape(n_blocks, block_size), axis=1)
        stds = np.std(fd[: n_blocks * block_size].reshape(n_blocks, block_size), axis=1, ddof=1)
        stds = np.maximum(stds, 1e-10)
        ranges = (
            np.max(fd[: n_blocks * block_size].reshape(n_blocks, block_size), axis=1)
            - np.min(fd[: n_blocks * block_size].reshape(n_blocks, block_size), axis=1)
        )

        # Log(log(R/S)) vs log(block_size)
        rs = ranges / stds
        valid = rs > 0
        if np.sum(valid) < 3:
            continue

        log_rs = np.log(rs[valid])
        log_n = np.log(np.full_like(rs, block_size, dtype=np.float64)[valid])

        slope, _, r2, _, _ = linregress(log_n, log_rs)
        hurst_est = slope / 2.0  # R/S Hurst estimate

        # Score: how close to target?
        score = -np.abs(hurst_est - target_hurst)
        hurst_scores.append((score, d))

    if not hurst_scores:
        logger.warning("Could not find optimal d — returning default 0.5")
        return 0.5

    # Best d = highest score (closest to target Hurst)
    optimal_d = max(hurst_scores, key=lambda x: x[0])[1]
    return round(float(optimal_d), 4)