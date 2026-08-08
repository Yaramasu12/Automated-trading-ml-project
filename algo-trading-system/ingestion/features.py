"""
Polars feature pipeline — vectorized batch features for research.

The same feature code runs in both batch (research) and online (live) modes
to avoid train/serve skew.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import polars as pl


@dataclass
class FeatureConfig:
    """Feature pipeline configuration."""
    rolling_windows: list[int] = (20, 50, 200)
    rsi_period: int = 14
    bollinger_std: int = 2
    keltner_multiplier: float = 2.0
    adx_period: int = 14
    atr_period: int = 14
    enable_volatility: bool = True
    enable_momentum: bool = True
    enable_trend: bool = True
    enable_volume: bool = True
    enable_orderflow: bool = True


class FeatureEngine:
    """
    Vectorized feature computation using Polars.

    Supports:
    - Volatility features (rolling std, Bollinger Bands, ATR)
    - Momentum features (RSI, rate of change)
    - Trend features (EMA crosses, Keltner Channels)
    - Volume features (volume MA, OBV)
    - Order-flow features (tick imbalance, VPIN proxy)
    """

    def __init__(self, config: Optional[FeatureConfig] = None) -> None:
        self._config = config or FeatureConfig()

    def compute_all(self, df: pl.DataFrame) -> pl.DataFrame:
        """Compute all features for a DataFrame."""
        result = df.clone()

        if self._config.enable_volatility:
            result = self._add_volatility_features(result)
        if self._config.enable_momentum:
            result = self._add_momentum_features(result)
        if self._config.enable_trend:
            result = self._add_trend_features(result)
        if self._config.enable_volume:
            result = self._add_volume_features(result)
        if self._config.enable_orderflow:
            result = self._add_orderflow_features(result)

        return result

    def _add_volatility_features(self, df: pl.DataFrame) -> pl.DataFrame:
        """Add volatility features: rolling std, Bollinger, ATR."""
        windows = self._config.rolling_windows

        # Rolling volatility
        for w in windows:
            col_name = f"vol_{w}"
            df = df.with_columns(
                pl.col("close").rolling_std(window_size=w).alias(col_name)
            )

        # Bollinger Bands
        mid = pl.col("close").rolling_mean(window_size=windows[0])
        std = pl.col("close").rolling_std(window_size=windows[0])
        df = df.with_columns([
            (mid + self._config.bollinger_std * std).alias("bollinger_upper"),
            mid.alias("bollinger_mid"),
            (mid - self._config.bollinger_std * std).alias("bollinger_lower"),
            ((pl.col("close") - mid) / (self._config.bollinger_std * std + 1e-10)).alias("bollinger_pct"),
        ])

        # ATR (Average True Range)
        high = pl.col("high")
        low = pl.col("low")
        prev_close = pl.col("close").shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        atr = pl.concat([tr1, tr2, tr3], how="horizontal").max(axis=1).rolling_mean(window_size=self._config.atr_period)

        df = df.with_columns(atr.alias("atr"))

        return df

    def _add_momentum_features(self, df: pl.DataFrame) -> pl.DataFrame:
        """Add momentum features: RSI, rate of change."""
        period = self._config.rsi_period

        # RSI
        change = pl.col("close") - pl.col("close").shift(1)
        gain = change.clip(lower_bound=0.0)
        loss = (-change).clip(lower_bound=0.0)
        avg_gain = gain.rolling_mean(window_size=period)
        avg_loss = loss.rolling_mean(window_size=period)
        rs = avg_gain / (avg_loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))
        df = df.with_columns(rsi.alias("rsi"))

        # Rate of Change (ROC)
        for roc_period in [5, 10, 21]:
            close = pl.col("close")
            roc = ((close / close.shift(roc_period) - 1) * 100).alias(f"roc_{roc_period}")
            df = df.with_columns(roc)

        return df

    def _add_trend_features(self, df: pl.DataFrame) -> pl.DataFrame:
        """Add trend features: EMA crosses, Keltner Channels."""
        windows = [20, 50, 200]

        # EMAs
        emas = {}
        for w in windows:
            ema = pl.col("close").rolling_mean(window_size=w)
            ema_col = f"ema_{w}"
            emas[ema_col] = ema
            df = df.with_columns(ema)

        # EMA crosses
        df = df.with_columns(
            (emas["ema_20"] - emas["ema_50"]).alias("ema_cross_20_50"),
            (emas["ema_50"] - emas["ema_200"]).alias("ema_cross_50_200"),
        )

        # Keltner Channels
        atr = pl.col("atr")
        mid = pl.col("close").rolling_mean(window_size=windows[0])
        df = df.with_columns([
            (mid + self._config.keltner_multiplier * atr).alias("keltner_upper"),
            mid.alias("keltner_mid"),
            (mid - self._config.keltner_multiplier * atr).alias("keltner_lower"),
        ])

        return df

    def _add_volume_features(self, df: pl.DataFrame) -> pl.DataFrame:
        """Add volume features: volume MA, OBV."""
        windows = [20, 50]

        # Volume MA
        for w in windows:
            vol_ma = pl.col("volume").rolling_mean(window_size=w)
            df = df.with_columns(
                (pl.col("volume") / (vol_ma + 1e-10)).alias(f"vol_ratio_{w}")
            )

        # OBV (On-Balance Volume)
        sign = pl.sign(pl.col("close") - pl.col("close").shift(1))
        obv = (sign * pl.col("volume")).cum_sum()
        df = df.with_columns(obv.alias("obv"))

        return df

    def _add_orderflow_features(self, df: pl.DataFrame) -> pl.DataFrame:
        """Add order-flow features: tick imbalance, VPIN proxy."""
        # Tick imbalance (signed tick ratio)
        tick_dir = pl.col("tick_direction")
        signed_tick = tick_dir * pl.col("last_size")

        # Rolling tick imbalance
        for w in [10, 50]:
            total_size = pl.col("last_size").rolling_sum(window_size=w)
            signed_sum = signed_tick.rolling_sum(window_size=w)
            imbalance = (signed_sum / (total_size + 1e-10)).alias(f"tick_imbalance_{w}")
            df = df.with_columns(imbalance)

        # VPIN proxy (Volume-Synchronized Imbalance)
        bucket_size = 1000
        volume_bucket = (pl.col("volume") / bucket_size).floor().alias("vol_bucket")
        vpin = (
            (pl.col("last_size") * pl.col("tick_direction"))
            .group_by("vol_bucket")
            .agg(
                (pl.col("last_size") * pl.col("tick_direction")).sum().alias("vpin_num"),
                pl.col("last_size").sum().alias("vpin_den"),
            )
        )
        df = df.with_columns(vpin)

        return df

    def compute_online(self, prev_state: dict, tick: dict) -> dict:
        """
        Compute features for a single tick (online mode).

        Maintains running state for incremental computation.
        """
        state = dict(prev_state)

        # Update price history
        if "prices" not in state:
            state["prices"] = []
            state["volumes"] = []
            state["tick_dirs"] = []

        state["prices"].append(tick.get("last_price", 0))
        state["volumes"].append(tick.get("last_size", 0))
        state["tick_dirs"].append(tick.get("tick_direction", 0))

        # Trim history
        max_len = max(self._config.rolling_windows) * 2
        if len(state["prices"]) > max_len:
            state["prices"] = state["prices"][-max_len:]
            state["volumes"] = state["volumes"][-max_len:]
            state["tick_dirs"] = state["tick_dirs"][-max_len:]

        # Compute features from history
        features = {}
        prices = state["prices"]
        volumes = state["volumes"]

        if len(prices) >= 2:
            # Current volatility
            returns = [
                (prices[i] - prices[i - 1]) / prices[i - 1]
                for i in range(1, len(prices))
            ]
            features["realized_vol"] = sum(r ** 2 for r in returns) ** 0.5

            # Tick imbalance
            total_tick = sum(state["tick_dirs"])
            features["tick_imbalance"] = total_tick / (len(state["tick_dirs"]) + 1e-10)

        state["_features"] = features
        return state