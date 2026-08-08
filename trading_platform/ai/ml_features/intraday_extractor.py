"""
trading_platform/ai/features/intraday_extractor.py — Intraday feature extraction

Per §4.3 and §3.1: daily-bar TA features have zero OOS edge (AUC ≈ 0.50).
New directional attempt uses:
- 1m/5m bars from tick feed
- Microstructure features: spread, tick-run, relative volume
- Option-flow features: ΔOI, PCR shifts, IV skew changes
- Meta-labeling with LightGBM

All features carry lineage metadata for P&L attribution.
Every feature is validated via walk-forward before touching order flow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from numpy.typing import NDArray
import polars as pl

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Feature groups
# ──────────────────────────────────────────────


class FeatureGroup:
    """Feature group identifiers for attribution."""
    PRICE = "price"
    VOLUME = "volume"
    SPREAD = "spread"
    MICROSTRUCTURE = "microstructure"
    OPTION_FLOW = "option_flow"
    IV_SKEW = "iv_skew"
    LEAD_LAG = "lead_lag"
    REGIME = "regime"


# ──────────────────────────────────────────────
# Feature metadata
# ──────────────────────────────────────────────


@dataclass
class FeatureMeta:
    """Metadata for a single feature."""
    name: str
    group: str
    version: int = 1
    source: str = ""
    transform: str = "raw"
    drift_threshold: float = 0.1  # Z-score threshold for drift alert
    importance_baseline: float = 0.0  # Baseline SHAP importance


# ──────────────────────────────────────────────
# 1m bar builder from ticks
# ──────────────────────────────────────────────


def build_1m_bars_from_ticks(
    ticks: List[Dict[str, Any]],
    symbol: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> pl.DataFrame:
    """
    Build 1-minute OHLCV bars from raw tick data.

    Parameters:
        ticks: list of tick dicts with keys: timestamp, price, volume, bid, ask
        symbol: instrument symbol
        start_time: optional start timestamp filter (ISO string)
        end_time: optional end timestamp filter (ISO string)

    Returns:
        Polars DataFrame with columns:
        bar_start, open, high, low, close, volume, num_ticks, vwap
    """
    if not ticks:
        return pl.DataFrame()

    df = pl.DataFrame(ticks).with_columns(
        pl.col("timestamp").cast(pl.Datetime)
    )

    if start_time:
        df = df.filter(pl.col("timestamp") >= pl.col(start_time))
    if end_time:
        df = df.filter(pl.col("timestamp") <= pl.col(end_time))

    if len(df) == 0:
        return pl.DataFrame()

    # Resample to 1-minute bars
    bars = df.resample(
        df=pl.col("timestamp"),
        every="1m",
        offset="0m",
    ).agg([
        pl.col("price").first().alias("open"),
        pl.col("price").max().alias("high"),
        pl.col("price").min().alias("low"),
        pl.col("price").last().alias("close"),
        pl.col("volume").sum().alias("volume"),
        pl.len().alias("num_ticks"),
        (pl.col("price") * pl.col("volume")).sum() / pl.col("volume").sum().alias("vwap"),
    ])

    bars = bars.with_columns(
        pl.col("timestamp").alias("bar_start")
    ).drop("timestamp")

    return bars


# ──────────────────────────────────────────────
# Microstructure features
# ──────────────────────────────────────────────


def extract_microstructure_features(
    ticks: List[Dict[str, Any]],
    bars: pl.DataFrame,
    window: int = 50,
) -> Dict[str, NDArray[np.float64]]:
    """
    Extract microstructure features from tick-level data.

    Features per §3.1:
    - Order-book imbalance
    - Depth slope
    - Trade-sign runs
    - Realized-vol-of-vol
    - Relative volume

    Returns:
        Dict mapping feature name → numpy array (aligned with bars)
    """
    if bars.is_empty() or len(ticks) < window:
        return {}

    features: Dict[str, NDArray[np.float64]] = {}
    n_bars = len(bars)

    # 1. Order-book imbalance: (bid_qty - ask_qty) / (bid_qty + ask_qty)
    #    Computed from depth snapshots
    bid_qty_arr = np.array([t.get("bid_qty", 0) for t in ticks])
    ask_qty_arr = np.array([t.get("ask_qty", 0) for t in ticks])

    # Rolling imbalance (aligned to bars via sampling)
    sample_step = max(1, len(ticks) // n_bars)
    imbalance = _rolling_imbalance(bid_qty_arr, ask_qty_arr, window, sample_step)
    features[f"obi_{window}"] = imbalance  # Order-book imbalance

    # 2. Depth slope: regression slope of depth vs price
    bid_price_arr = np.array([t.get("bid", 0) for t in ticks])
    depth_slope = _rolling_depth_slope(bid_price_arr, bid_qty_arr, window, sample_step)
    features[f"depth_slope_{window}"] = depth_slope

    # 3. Trade-sign run count: consecutive same-sign ticks
    trade_prices = np.array([t.get("price", 0) for t in ticks])
    if len(trade_prices) > 1:
        tick_direction = np.diff(trade_prices)
        tick_signs = np.sign(tick_direction)
        # Count runs of same sign
        sign_changes = np.diff(tick_signs) != 0
        run_lengths = _compute_run_lengths(sign_changes)
        avg_run = _rolling_mean(run_lengths, window)
        avg_run = np.pad(avg_run, (window, 0), constant_values=avg_run[0])
        features[f"avg_run_length_{window}"] = avg_run[:n_bars]
    else:
        features[f"avg_run_length_{window}"] = np.zeros(n_bars)

    # 4. Realized-vol-of-vol: rolling std of rolling realized vol
    close_prices = bars["close"].to_numpy()
    if len(close_prices) > window * 2:
        rv = np.diff(np.log(close_prices))  # Log returns
        rv_squared = rv ** 2
        rv_vol = _rolling_std(rv_squared, window) ** 0.5
        rv_of_vol = _rolling_std(rv_vol, window)
        features[f"rvol_of_vol_{window}"] = np.pad(
            rv_of_vol, (window, 0), constant_values=rv_of_vol[0]
        )[:n_bars]
    else:
        features[f"rvol_of_vol_{window}"] = np.zeros(n_bars)

    # 5. Relative volume: current bar volume / rolling mean volume
    volumes = bars["volume"].to_numpy()
    mean_vol = _rolling_mean(volumes, window)
    rel_volume = np.where(mean_vol > 0, volumes / mean_vol, 1.0)
    features[f"rel_volume_{window}"] = rel_volume

    return features


def _rolling_imbalance(
    bid_qty: NDArray[np.float64],
    ask_qty: NDArray[np.float64],
    window: int,
    step: int,
) -> NDArray[np.float64]:
    """Rolling order-book imbalance."""
    total = bid_qty + ask_qty
    total = np.where(total == 0, 1e-10, total)
    imbalance = (bid_qty - ask_qty) / total

    result = []
    for i in range(0, len(imbalance), step):
        start = max(0, i - window)
        window_data = imbalance[start:i]
        result.append(float(np.mean(window_data)) if len(window_data) > 0 else 0.0)
    return np.array(result)


def _rolling_depth_slope(
    prices: NDArray[np.float64],
    quantities: NDArray[np.float64],
    window: int,
    step: int,
) -> NDArray[np.float64]:
    """Rolling slope of depth (quantity) vs price via OLS."""
    result = []
    for i in range(0, len(prices), step):
        start = max(0, i - window)
        x = prices[start:i]
        y = quantities[start:i]
        if len(x) < 3:
            result.append(0.0)
            continue
        # OLS slope: Σ(x-x̄)(y-ȳ) / Σ(x-x̄)²
        x_mean = np.mean(x)
        y_mean = np.mean(y)
        num = np.sum((x - x_mean) * (y - y_mean))
        den = np.sum((x - x_mean) ** 2)
        slope = num / den if den > 0 else 0.0
        result.append(slope)
    return np.array(result)


def _compute_run_lengths(sign_changes: NDArray[np.int64]) -> NDArray[np.float64]:
    """Compute run lengths from sign-change boolean array."""
    runs = []
    current_run = 0
    for change in sign_changes:
        if change:
            runs.append(current_run)
            current_run = 0
        else:
            current_run += 1
    runs.append(current_run)
    return np.array(runs, dtype=float)


def _rolling_mean(data: NDArray[np.float64], window: int) -> NDArray[np.float64]:
    """Rolling mean with minimum window."""
    result = np.zeros_like(data, dtype=float)
    for i in range(window - 1, len(data)):
        result[i] = float(np.mean(data[i - window + 1:i + 1]))
    return result


def _rolling_std(data: NDArray[np.float64], window: int) -> NDArray[np.float64]:
    """Rolling standard deviation with minimum window."""
    result = np.zeros_like(data, dtype=float)
    for i in range(window - 1, len(data)):
        result[i] = float(np.std(data[i - window + 1:i + 1], ddof=1))
    return result


# ──────────────────────────────────────────────
# Option-flow features
# ──────────────────────────────────────────────


@dataclass
class OptionChainSnapshot:
    """A single option chain snapshot."""
    timestamp: int
    symbol: str
    expiry: str
    strikes: List[Dict[str, Any]] = field(default_factory=list)
    # Per-strike: {strike, oi, delta_iv, d_oi, iv, call_put}


def extract_option_flow_features(
    snapshots: List[OptionChainSnapshot],
    window: int = 20,
) -> Dict[str, NDArray[np.float64]]:
    """
    Extract option-flow features from chain snapshots.

    Features per §3.1:
    - ΔOI velocity per strike
    - IV-skew slope changes
    - PCR momentum
    - Cross-underlying lead-lag

    Returns:
        Dict mapping feature name → numpy array (aligned with snapshots)
    """
    if len(snapshots) < window:
        return {}

    n = len(snapshots)
    features: Dict[str, NDArray[np.float64]] = {}

    # 1. PCR (Put-Call Ratio) from OI
    pcr_values = []
    for snap in snapshots:
        calls_oi = sum(s.get("oi", 0) for s in snap.strikes if s.get("call_put") == "C")
        puts_oi = sum(s.get("oi", 0) for s in snap.strikes if s.get("call_put") == "P")
        pcr = puts_oi / calls_oi if calls_oi > 0 else 1.0
        pcr_values.append(pcr)

    pcr_arr = np.array(pcr_values)
    pcr_velocity = _rolling_diff(pcr_arr, 1)
    features["pcr_velocity"] = pcr_velocity

    # 2. PCR momentum (rolling change in PCR)
    pcr_momentum = _rolling_diff(pcr_arr, window // 2)
    features[f"pcr_momentum_{window}"] = pcr_momentum

    # 3. IV-skew slope: regression of IV on moneyness (K/S) for each snapshot
    skew_changes = []
    for i in range(1, n):
        prev_skew = _compute_iv_skew(snapshots[i - 1])
        curr_skew = _compute_iv_skew(snapshots[i])
        skew_changes.append(curr_skew - prev_skew)

    skew_arr = np.array(skew_changes)
    skew_velocity = _rolling_mean(skew_arr, window // 2)
    features["iv_skew_velocity"] = skew_velocity

    # 4. ΔOI velocity per major strikes (ATM ± 2%)
    strikes_to_track = _get_key_strikes(snapshots)
    for k in strikes_to_track:
        d_oi_series = []
        for snap in snapshots:
            strike_data = [s for s in snap.strikes if abs(s.get("strike", 0) - k) < 1]
            total_oi = sum(s.get("oi", 0) for s in strike_data)
            d_oi_series.append(total_oi)
        d_oi_arr = np.array(d_oi_series)
        d_oi_vel = _rolling_diff(d_oi_arr, 1)
        features[f"doi_{k}"] = d_oi_vel

    return features


def _compute_iv_skew(snapshot: OptionChainSnapshot) -> float:
    """Compute IV skew: IV(K/S - 5%) - IV(K/S + 5%)."""
    iv_by_moneyness: Dict[float, float] = {}
    spot = _get_spot_from_chain(snapshot)
    if spot <= 0:
        return 0.0

    for s in snapshot.strikes:
        k = s.get("strike", 0)
        if k > 0 and spot > 0:
            moneyness = k / spot
            iv = s.get("iv", 0)
            if abs(moneyness - 0.95) < 0.03:
                iv_by_moneyness[0.95] = iv
            elif abs(moneyness - 1.05) < 0.03:
                iv_by_moneyness[1.05] = iv

    iv_95 = iv_by_moneyness.get(0.95, 0)
    iv_105 = iv_by_moneyness.get(1.05, 0)
    return iv_95 - iv_105


def _get_key_strikes(snapshots: List[OptionChainSnapshot]) -> List[float]:
    """Extract key strikes (ATM ± 2% ± 4%) from chain snapshots."""
    strikes = set()
    for snap in snapshots[:5]:
        spot = _get_spot_from_chain(snap)
        if spot > 0:
            for pct in [-4, -2, 0, 2, 4]:
                strikes.add(round(spot * (1 + pct / 100), 0))
    return sorted(strikes)


def _get_spot_from_chain(snapshot: OptionChainSnapshot) -> float:
    """Estimate spot from option chain (ATM strike as proxy)."""
    atms = [s.get("strike", 0) for s in snapshot.strikes
            if abs(s.get("delta_iv", 0) - 50) < 5]
    return float(np.mean(atms)) if atms else 0.0


def _rolling_diff(arr: NDArray[np.float64], n: int) -> NDArray[np.float64]:
    """Rolling difference."""
    result = np.diff(arr, n)
    result = np.pad(result, (n, 0), constant_values=result[-1] if len(result) > 0 else 0.0)
    return result


# ──────────────────────────────────────────────
# Lead-lag features (cross-underlying)
# ──────────────────────────────────────────────


def extract_lead_lag_features(
    primary_returns: NDArray[np.float64],
    leading_returns: NDArray[np.float64],
    lag: int = 3,
) -> NDArray[np.float64]:
    """
    Cross-underlying lead-lag feature.

    Per §3.1: NIFTY↔BANKNIFTY lead-lag relationships.

    Parameters:
        primary_returns: returns for the primary underlying
        leading_returns: returns for the leading underlying
        lag: lag in bars (positive = leading, negative = lagging)

    Returns:
        Correlation-based lead-lag feature array
    """
    if len(primary_returns) < lag + 10:
        return np.zeros(len(primary_returns))

    # Rolling correlation at specified lag
    window = max(20, lag * 5)
    result = np.zeros(len(primary_returns))

    for i in range(lag, len(primary_returns)):
        x = primary_returns[max(0, i - window):i]
        y = leading_returns[max(0, i - window + lag):i + lag] if lag > 0 else \
            leading_returns[max(0, i):i + lag] if lag < 0 else leading_returns[i:i + 1]

        if len(x) < 5 or len(y) < 5:
            continue

        x_mean = np.mean(x)
        y_mean = np.mean(y)
        num = np.sum((x - x_mean) * (y - y_mean))
        den_x = np.sum((x - x_mean) ** 2) ** 0.5
        den_y = np.sum((y - y_mean) ** 2) ** 0.5

        if den_x > 0 and den_y > 0:
            result[i] = num / (den_x * den_y)

    return result


# ──────────────────────────────────────────────
# Regime features
# ──────────────────────────────────────────────


def extract_regime_features(
    returns: NDArray[np.float64],
    volumes: NDArray[np.float64],
    windows: List[int] = None,
) -> Dict[str, NDArray[np.float64]]:
    """
    Extract regime features for regime detection.

    Features:
    - Realized vol at multiple horizons
    - Breadth (advancing/declining ratio proxy)
    - Trend strength
    - Volume regime

    Returns:
        Dict mapping feature name → numpy array
    """
    if windows is None:
        windows = [5, 20, 50]

    features: Dict[str, NDArray[np.float64]] = {}
    n = len(returns)

    for w in windows:
        if n < w:
            continue
        # Realized vol
        rv = _rolling_std(returns, w)
        features[f"rv_{w}"] = rv

        # Annualized vol
        features[f"rv_ann_{w}"] = rv * (252 ** 0.5)

    # Trend strength: |mean(returns)| / std(returns) over window
    for w in [20, 50]:
        if n < w:
            continue
        means = _rolling_mean(returns, w)
        stds = _rolling_std(returns, w)
        trend = np.where(stds > 0, np.abs(means) / stds, 0.0)
        features[f"trend_{w}"] = trend

    # Volume regime: volume z-score
    vol_mean = float(np.mean(volumes))
    vol_std = float(np.std(volumes))
    if vol_std > 0:
        vol_z = (volumes - vol_mean) / vol_std
    else:
        vol_z = np.zeros_like(volumes)
    features["vol_z"] = vol_z

    return features


# ──────────────────────────────────────────────
# Feature metadata registry
# ──────────────────────────────────────────────


def get_feature_meta() -> List[FeatureMeta]:
    """Return the full feature metadata registry."""
    return [
        FeatureMeta("obi_50", FeatureGroup.MICROSTRUCTURE, 1, "depth", "rolling_mean", 0.15),
        FeatureMeta("depth_slope_50", FeatureGroup.MICROSTRUCTURE, 1, "depth", "ols_slope", 0.15),
        FeatureMeta("avg_run_length_50", FeatureGroup.MICROSTRUCTURE, 1, "tick", "run_count", 0.10),
        FeatureMeta("rvol_of_vol_50", FeatureGroup.MICROSTRUCTURE, 1, "close", "rolling_std", 0.10),
        FeatureMeta("rel_volume_50", FeatureGroup.VOLUME, 1, "volume", "z_score", 0.20),
        FeatureMeta("pcr_velocity", FeatureGroup.OPTION_FLOW, 1, "oi", "diff", 0.10),
        FeatureMeta("pcr_momentum_20", FeatureGroup.OPTION_FLOW, 1, "oi", "rolling_diff", 0.10),
        FeatureMeta("iv_skew_velocity", FeatureGroup.IV_SKEW, 1, "iv", "diff", 0.10),
        FeatureMeta("rv_5", FeatureGroup.REGIME, 1, "returns", "rolling_std", 0.15),
        FeatureMeta("rv_20", FeatureGroup.REGIME, 1, "returns", "rolling_std", 0.15),
        FeatureMeta("rv_50", FeatureGroup.REGIME, 1, "returns", "rolling_std", 0.15),
        FeatureMeta("trend_20", FeatureGroup.REGIME, 1, "returns", "trend_strength", 0.10),
        FeatureMeta("trend_50", FeatureGroup.REGIME, 1, "returns", "trend_strength", 0.10),
        FeatureMeta("vol_z", FeatureGroup.VOLUME, 1, "volume", "z_score", 0.20),
    ]


def extract_all_intraday_features(
    ticks: List[Dict[str, Any]],
    bars: pl.DataFrame,
    snapshots: List[OptionChainSnapshot],
    primary_returns: Optional[NDArray[np.float64]] = None,
    leading_returns: Optional[NDArray[np.float64]] = None,
) -> Tuple[Dict[str, NDArray[np.float64]], List[FeatureMeta]]:
    """
    Extract all intraday features from available data sources.

    Returns:
        (features_dict, feature_meta_list) — features keyed by name,
        with corresponding metadata for drift monitoring and attribution.
    """
    all_features: Dict[str, NDArray[np.float64]] = {}

    # Microstructure features
    micro = extract_microstructure_features(ticks, bars)
    all_features.update(micro)

    # Option-flow features
    optflow = extract_option_flow_features(snapshots)
    all_features.update(optflow)

    # Regime features
    close_prices = bars["close"].to_numpy() if not bars.is_empty() else np.array([])
    if len(close_prices) > 1:
        returns = np.diff(np.log(close_prices + 1e-10))
        volumes = bars["volume"].to_numpy() if not bars.is_empty() else np.zeros(len(close_prices))
        regime = extract_regime_features(returns, volumes)
        all_features.update(regime)

        # Lead-lag features
        if primary_returns is not None and leading_returns is not None:
            ll = extract_lead_lag_features(primary_returns, leading_returns, lag=3)
            all_features["lead_lag_3"] = ll
            all_features["lead_lag_5"] = extract_lead_lag_features(primary_returns, leading_returns, lag=5)

    meta = get_feature_meta()
    return all_features, meta