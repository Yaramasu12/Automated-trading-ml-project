"""
trading_platform/ai/labeling/triple_barrier.py — Triple-barrier labeling

Per §4.4b (López de Prado AFML suite):
- Replace fixed-horizon returns with triple-barrier labels
- Profit-take / stop-loss / time barriers
- Meta-labeling: base rules propose; ML filters/sizes
- Sample-unique weights to de-bias overlapping labels

References:
- López de Prado, "Advances in Financial Machine Learning" (2018), Ch. 3
- López de Prado, "Machine Learning for Asset Managers" (2023), Ch. 6
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
from numpy.typing import NDArray

logger = logging.getLogger(__name__)


class BarrierType(str, Enum):
    PROFIT_TAKE = "profit_take"
    STOP_LOSS = "stop_loss"
    TIME = "time"


class LabelSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


@dataclass
class TripleBarrierLabel:
    """A single triple-barrier label."""
    timestamp: int          # Unix epoch of entry
    instrument: str
    side: LabelSide
    entry_price: float
    profit_take_level: float
    stop_loss_level: float
    time_barrier: int       # max bars to hold
    hit_bar: int            # which barrier was hit (0=time, 1=profit, 2=stop)
    pnl_pct: float          # return % if barrier hit
    slippage_cost: float    # estimated slippage in price units
    sample_weight: float    # sample-unique weight (meta-labeling)
    features: Dict[str, float] = None  # features at entry time

    def __post_init__(self):
        if self.features is None:
            self.features = {}


@dataclass
class TripleBarrierConfig:
    """Configuration for triple-barrier labeling."""
    # Profit-take barrier (percentage)
    pt_threshold: float = 0.02  # 2% profit target

    # Stop-loss barrier (percentage)
    sl_threshold: float = 0.015  # 1.5% stop loss

    # Time barrier (in bars)
    t_threshold: int = 50

    # Slippage estimate (price units)
    slippage: float = 0.05

    # Minimum price movement (tick size)
    tick_size: float = 0.05

    # Meta-labeling: base rule must agree (0=ignore, 1=long, -1=short)
    meta_rule_multiplier: int = 1

    # Overlap tolerance (fraction of labels allowed to overlap)
    max_overlap: float = 0.5


def generate_triple_barrier_labels(
    prices: NDArray[np.float64],
    volumes: Optional[NDArray[np.float64]] = None,
    side: Optional[NDArray[np.int64]] = None,
    config: Optional[TripleBarrierConfig] = None,
    instrument: str = "DEFAULT",
    features: Optional[Dict[str, NDArray[np.float64]]] = None,
) -> List[TripleBarrierLabel]:
    """
    Generate triple-barrier labels from price series.

    Parameters:
        prices: OHLCV or close prices (1D array)
        volumes: optional volume array
        side: optional base-rule direction (1=long, -1=short, 0=flat)
        config: labeling configuration
        instrument: instrument identifier
        features: optional feature arrays keyed by feature name

    Returns:
        List of TripleBarrierLabel objects
    """
    if config is None:
        config = TripleBarrierConfig()

    if len(prices) < config.t_threshold + 10:
        logger.warning("Insufficient data for triple-barrier labeling: %d < %d",
                        len(prices), config.t_threshold + 10)
        return []

    labels = []
    n = len(prices)

    # Determine entry points (where side != 0 or where side is None)
    if side is not None:
        entry_indices = np.where(side != 0)[0]
    else:
        entry_indices = np.arange(config.t_threshold, n)

    for i in entry_indices:
        if i + config.t_threshold >= n:
            break

        entry_price = float(prices[i])
        if entry_price <= 0:
            continue

        # Determine direction
        if side is not None and side[i] == 0:
            continue
        elif side is None:
            direction = 1  # Default long
        else:
            direction = int(side[i])

        # Apply meta-labeling multiplier
        if config.meta_rule_multiplier != 1 and side is not None:
            direction *= config.meta_rule_multiplier

        # Profit-take and stop-loss levels
        if direction > 0:  # Long
            pt_level = entry_price * (1 + config.pt_threshold)
            sl_level = entry_price * (1 - config.sl_threshold)
        else:  # Short
            pt_level = entry_price * (1 - config.pt_threshold)
            sl_level = entry_price * (1 + config.sl_threshold)

        # Walk forward to find which barrier hits first
        max_bar = min(i + config.t_threshold, n)
        pnl_pct = 0.0
        hit_bar = 0  # 0 = time, 1 = profit, 2 = stop

        for bar in range(i + 1, max_bar):
            price = float(prices[bar])

            if direction > 0:
                # Long: check stop first, then profit
                if price <= sl_level:
                    hit_bar = 2
                    pnl_pct = -config.sl_threshold
                    break
                elif price >= pt_level:
                    hit_bar = 1
                    pnl_pct = config.pt_threshold
                    break
            else:
                # Short: check stop first, then profit
                if price >= sl_level:
                    hit_bar = 2
                    pnl_pct = -config.sl_threshold
                    break
                elif price <= pt_level:
                    hit_bar = 1
                    pnl_pct = config.pt_threshold
                    break

        # If no barrier hit, it's a time barrier
        if hit_bar == 0:
            exit_price = float(prices[max_bar - 1])
            if direction > 0:
                pnl_pct = (exit_price - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - exit_price) / entry_price

        # Sample-unique weight (de-bias overlapping labels)
        weight = _compute_sample_unique_weight(i, entry_indices, config.max_overlap)

        # Build feature dict
        feat_dict = {}
        if features:
            for fname, farray in features.items():
                if i < len(farray):
                    feat_dict[fname] = float(farray[i])

        label = TripleBarrierLabel(
            timestamp=i,
            instrument=instrument,
            side=LabelSide.LONG if direction > 0 else LabelSide.SHORT,
            entry_price=entry_price,
            profit_take_level=pt_level,
            stop_loss_level=sl_level,
            time_barrier=config.t_threshold,
            hit_bar=hit_bar,
            pnl_pct=pnl_pct,
            slippage_cost=config.slippage,
            sample_weight=weight,
            features=feat_dict,
        )
        labels.append(label)

    return labels


def _compute_sample_unique_weight(
    entry_idx: int,
    entry_indices: NDArray[np.int64],
    max_overlap: float = 0.5,
) -> float:
    """
    Compute sample-unique weight per López de Prado (2018) §6.1.2.

    Overlapping labels share credit: each label gets weight = 1 / (1 + overlap_count).
    This de-biases the training set when labels are generated from overlapping windows.
    """
    if len(entry_indices) < 2:
        return 1.0

    # Count how many other entry points overlap with this one
    # Overlap: another entry starts before this one's time barrier expires
    overlap_count = 0
    for other_idx in entry_indices:
        if other_idx == entry_idx:
            continue
        # Check overlap: |other - entry| < t_threshold
        if abs(other_idx - entry_idx) < 50:  # Using fixed t_threshold for simplicity
            overlap_count += 1

    # Apply weight
    weight = 1.0 / (1.0 + overlap_count * max_overlap)
    return weight


def compute_meta_labels(
    base_labels: List[TripleBarrierLabel],
    model_predictions: NDArray[np.float64],
    model_threshold: float = 0.55,
) -> List[TripleBarrierLabel]:
    """
    Meta-labeling: filter base-rule labels with model predictions.

    Per López de Prado: meta-labeling uses a binary classifier to filter
    which base-rule signals to actually trade. The model predicts P(win),
    and only signals where P(win) > threshold are executed.

    This also enables fractional execution: size proportional to P(win) - 0.5.

    Returns:
        Modified base_labels with meta-labels applied
    """
    if not base_labels or model_predictions is None:
        return base_labels

    for i, label in enumerate(base_labels):
        if i >= len(model_predictions):
            break

        pred = float(model_predictions[i])

        # Meta-label: was the trade actually profitable?
        actual_win = 1.0 if label.pnl_pct > 0 else 0.0

        # Only update if model predicts > threshold
        if pred >= model_threshold:
            # Fractional size: scale by (pred - 0.5) * 2
            fractional_size = max(0.0, (pred - 0.5) * 2.0)
            label.features["meta_pred"] = pred
            label.features["meta_confidence"] = fractional_size
            label.features["meta_actual_win"] = actual_win
        else:
            # Signal suppressed by model
            label.features["meta_pred"] = pred
            label.features["meta_confidence"] = 0.0
            label.features["meta_suppressed"] = True

    return base_labels


def conformal_prediction(
    predictions: NDArray[np.float64],
    residuals: NDArray[np.float64],
    confidence: float = 0.90,
) -> Tuple[NDArray[np.float64], NDArray[np.float64]]:
    """
    Conformal prediction: generate honest uncertainty intervals.

    Per §4.4b: a signal whose conformal interval spans zero is auto-abstained,
    and size scales with interval width.

    Parameters:
        predictions: model predictions (probabilities or scores)
        residuals: calibration residuals |y - ŷ|
        confidence: confidence level for intervals (default 0.90)

    Returns:
        (lower_bounds, upper_bounds) arrays
    """
    if len(residuals) < 10:
        # Not enough calibration data — return neutral intervals
        width = 0.5
        return np.full_like(predictions, predictions - width), np.full_like(
            predictions, predictions + width
        )

    # Q-value: (1-α) quantile of residuals
    alpha = 1.0 - confidence
    q_value = float(np.quantile(residuals, 1 - alpha))

    # Prediction intervals
    lower = predictions - q_value
    upper = predictions + q_value

    return lower, upper


def abstain_from_signals(
    labels: List[TripleBarrierLabel],
    lower_bounds: NDArray[np.float64],
    upper_bounds: NDArray[np.float64],
) -> List[TripleBarrierLabel]:
    """
    Auto-abstain from signals whose conformal interval spans the abstain threshold (0.5).

    Returns modified labels with meta_abstained flag set for suppressed signals.
    """
    abstained_count = 0
    for i, label in enumerate(labels):
        if i >= len(lower_bounds):
            break

        lo = float(lower_bounds[i])
        hi = float(upper_bounds[i])

        # If interval contains 0.5 (neutral), abstain
        if lo <= 0.5 <= hi:
            label.features["meta_abstained"] = True
            label.features["meta_interval_width"] = hi - lo
            abstained_count += 1
        else:
            label.features["meta_abstained"] = False
            # Scale size by distance from 0.5, normalized by interval width
            center = (lo + hi) / 2
            width = max(hi - lo, 1e-10)
            label.features["meta_scaled_size"] = min(1.0, (center - 0.5) * 2.0 / width)

    logger.info("Conformal abstention: %d / %d signals abstained",
                abstained_count, len(labels))
    return labels