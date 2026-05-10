from __future__ import annotations

"""Transforms raw market data into normalized neural feature batches."""

import math
from typing import Any

from trading_platform.neural.schemas import NeuralFeatureBatch


def _safe_normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / max(1, len(values))
    std = math.sqrt(variance) if variance > 0 else 1.0
    return [(v - mean) / std for v in values]


def build_feature_batch(
    symbol: str,
    bars: list[dict],
    extra: dict[str, Any] | None = None,
) -> NeuralFeatureBatch:
    """Build a NeuralFeatureBatch from OHLCV bars and optional extra features.

    Deterministic baseline; no deep learning required.
    """
    if not bars:
        return NeuralFeatureBatch(symbol=symbol, feature_ids=[], values=[])

    closes = [b.get("close", 0.0) for b in bars]
    volumes = [float(b.get("volume", 0)) for b in bars]
    highs = [b.get("high", 0.0) for b in bars]
    lows = [b.get("low", 0.0) for b in bars]

    # Returns
    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        if i > 0 and closes[i - 1] > 0 else 0.0
        for i in range(len(closes))
    ]

    # Rolling features
    n = len(closes)
    sma5 = sum(closes[-5:]) / min(5, n) if n > 0 else 0.0
    sma20 = sum(closes[-20:]) / min(20, n) if n > 0 else 0.0
    current = closes[-1] if closes else 0.0
    realized_vol = math.sqrt(sum(r ** 2 for r in returns[-20:]) / max(1, min(20, n)))

    high_range = max(highs[-20:]) - min(lows[-20:]) if len(highs) >= 2 else 0.0
    avg_volume = sum(volumes[-10:]) / max(1, min(10, len(volumes)))

    raw_values = [
        current / max(sma5, 1e-9) - 1.0,
        current / max(sma20, 1e-9) - 1.0,
        realized_vol,
        high_range / max(current, 1e-9),
        volumes[-1] / max(avg_volume, 1.0) - 1.0 if volumes else 0.0,
        returns[-1] if returns else 0.0,
        returns[-5] if len(returns) >= 5 else 0.0,
    ]

    feature_ids = [
        "price_vs_sma5", "price_vs_sma20", "realized_vol_20",
        "high_low_range", "volume_relative", "return_1bar", "return_5bar",
    ]

    if extra:
        for k, v in extra.items():
            if isinstance(v, (int, float)):
                feature_ids.append(k)
                raw_values.append(float(v))

    return NeuralFeatureBatch(symbol=symbol, feature_ids=feature_ids, values=raw_values)
