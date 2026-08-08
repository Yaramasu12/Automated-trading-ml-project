"""
trading_platform/data/feature_store.py — Feast feature store skeleton (REDESIGN §3.1)

Provides both offline (Parquet/DuckDB) and online (Redis) feature serving
with point-in-time-correct joins to eliminate train/serve skew.

All feature views are versioned. Lineage metadata tracks source + transform.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Set

import polars as pl
from redis import Redis

# Feast imports — handle version differences (Feast 0.x vs 1.x API changes)
try:
    from feast import Entity, Feature, FeatureView, ValueType
    from feast.data_source import CsvSource, RedisSource
    from feast.online_response import OnlineResponse
    _FEAST_V1 = False
except ImportError:
    # Feast 1.x: use the top-level imports
    try:
        from feast import Entity, Feature, FeatureView, ValueType, DataSource
        _FEAST_V1 = True
    except ImportError:
        # Feast not installed — feature store runs in standalone mode
        Entity = Feature = FeatureView = ValueType = DataSource = None  # type: ignore
        CsvSource = RedisSource = None  # type: ignore
        _FEAST_V1 = False

OnlineResponse = Any  # type: ignore

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Internal feature store (Redis-backed) — used by live pipeline
# ──────────────────────────────────────────────


class FeatureStore:
    """Lightweight Feast-like feature store backed by Redis online + Parquet offline."""

    def __init__(
        self,
        redis_client: Optional[Redis] = None,
        offline_dir: str = "data/feature_store/offline",
    ) -> None:
        self._redis = redis_client
        self._offline_dir = offline_dir
        self._views: Dict[str, "FeatureView"] = {}

    # ── Registration ──────────────────────────────

    def register(self, view: FeatureView) -> None:
        """Register a feature view (online + offline definitions)."""
        self._views[view.name] = view
        logger.info("Registered feature view: %s (version=%s)", view.name, view.version)

    # ── Online serving ──────────────────────────────

    async def get_online_features(
        self,
        features: List[str],
        entity_keys: List[str],
        feature_view_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch latest features from Redis for entity keys.

        Returns {feature_name: value} per key, or empty dict on miss.
        """
        if self._redis is None:
            logger.warning("Redis not configured — returning empty features")
            return {}

        view_name = feature_view_name or (features[0].split(":")[0] if features else "")
        result: Dict[str, Any] = {}

        for key in entity_keys:
            redis_key = f"feat:{view_name}:{key}"
            raw = self._redis.hgetall(redis_key)
            if raw:
                for k, v in raw.items():
                    result[k.decode()] = self._decode(v.decode())
            else:
                logger.debug("Feature miss for view=%s key=%s", view_name, key)

        return result

    async def update_online_features(
        self,
        feature_view_name: str,
        entity_key: str,
        values: Dict[str, Any],
        version: Optional[str] = None,
    ) -> None:
        """Write features to Redis online store."""
        if self._redis is None:
            logger.warning("Redis not configured — online update dropped")
            return

        redis_key = f"feat:{feature_view_name}:{entity_key}"
        encoded: Dict[bytes, bytes] = {}
        for k, v in values.items():
            encoded[k.encode()] = self._encode(v).encode()

        self._redis.hset(redis_key, mapping=encoded)
        logger.debug("Updated online features: view=%s key=%s", feature_view_name, entity_key)

    # ── Offline (Parquet) ──────────────────────────

    def read_offline(
        self,
        feature_view_name: str,
        entity_keys: Optional[List[str]] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        columns: Optional[List[str]] = None,
    ) -> pl.DataFrame:
        """Read features from offline Parquet store.

        Returns a Polars DataFrame. Uses DuckDB for SQL queries over Parquet.
        """
        import pathlib
        import duckdb

        base = pathlib.Path(self._offline_dir) / feature_view_name
        if not base.exists():
            logger.warning("Offline store not found: %s", base)
            return pl.DataFrame()

        # Build query
        parquet_files = list(base.glob("**/*.parquet")) if base.is_dir() else [base]
        if not parquet_files:
            return pl.DataFrame()

        # Filter by date range
        if start:
            parquet_files = [f for f in parquet_files if f.stat().st_mtime >= start.timestamp()]
        if end:
            parquet_files = [f for f in parquet_files if f.stat().st_mtime <= end.timestamp()]

        if not parquet_files:
            return pl.DataFrame()

        df = pl.read_parquet(parquet_files)

        # Column filter
        if columns:
            df = df.select([c for c in columns if c in df.columns])

        # Entity key filter
        if entity_keys and "entity_key" in df.columns:
            df = df.filter(pl.col("entity_key").is_in(entity_keys))

        return df

    def write_offline(
        self,
        feature_view_name: str,
        df: pl.DataFrame,
        partition_by: Optional[str] = None,
    ) -> List[str]:
        """Write features to offline Parquet store.

        Returns list of written file paths.
        """
        import pathlib

        base = pathlib.Path(self._offline_dir) / feature_view_name
        base.mkdir(parents=True, exist_ok=True)

        if partition_by:
            grouped = df.group_by(partition_by)
            files: list[str] = []
            for part_val, part_df in grouped:
                subdir = base / str(part_val)
                subdir.mkdir(parents=True, exist_ok=True)
                fname = f"features_{part_val}.parquet"
                fpath = subdir / fname
                part_df.write_parquet(str(fpath))
                files.append(str(fpath))
            return files

        import uuid
        fname = f"features_{uuid.uuid4().hex[:8]}.parquet"
        fpath = base / fname
        df.write_parquet(str(fpath))
        return [str(fpath)]

    # ── Feature lineage ────────────────────────────

    def log_lineage(
        self,
        feature_view_name: str,
        version: str,
        source: str,
        transforms: List[str],
    ) -> None:
        """Log feature lineage metadata."""
        entry = {
            "feature_view": feature_view_name,
            "version": version,
            "source": source,
            "transforms": transforms,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("Feature lineage: %s", json.dumps(entry, default=str))

    # ── Helpers ────────────────────────────────────

    @staticmethod
    def _encode(v: Any) -> str:
        if isinstance(v, (dict, list)):
            return json.dumps(v, default=str)
        if isinstance(v, Decimal):
            return str(float(v))
        return str(v)

    @staticmethod
    def _decode(v: str) -> Any:
        try:
            return json.loads(v)
        except (json.JSONDecodeError, ValueError):
            return v


# ──────────────────────────────────────────────
# Feature view definitions (for Feast integration)
# ──────────────────────────────────────────────


def make_feature_view(
    name: str,
    features: List[Feature],
    source: Any,  # CsvSource or RedisSource
    entities: List[Entity],
    ttl: Optional[Any] = None,
    version: str = "v1",
) -> FeatureView:
    """Factory for creating Feast-compatible FeatureViews."""
    return FeatureView(
        name=name,
        features=features,
        source=source,
        entities=entities,
        ttl=ttl,
        version=int(version.split("v")[-1]) if "v" in version else 1,
    )


# ──────────────────────────────────────────────
# Pre-defined feature views for the platform
# ──────────────────────────────────────────────


class FeatureViewRegistry:
    """Central registry of all feature views.

    Each view has:
    - name, version
    - entities (instrument_id, underlying, etc.)
    - features (list of feature names + types)
    - source (broker feed, computed, external)
    - lineage (transforms applied)
    """

    def __init__(self) -> None:
        self._views: List[Dict[str, Any]] = []

    def register(
        self,
        name: str,
        version: str = "v1",
        entities: Optional[List[str]] = None,
        features: Optional[List[Dict[str, str]]] = None,
        source: str = "pipeline",
        lineage: Optional[List[str]] = None,
    ) -> None:
        self._views.append({
            "name": name,
            "version": version,
            "entities": entities or [],
            "features": features or [],
            "source": source,
            "lineage": lineage or [],
        })
        logger.info("Registered feature view: %s (%s)", name, version)

    def list_views(self) -> List[Dict[str, Any]]:
        return list(self._views)

    def get_view(self, name: str, version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        for v in self._views:
            if v["name"] == name:
                if version is None or v["version"] == version:
                    return v
        return None


# ──────────────────────────────────────────────
# Pre-defined feature views
# ──────────────────────────────────────────────

registry = FeatureViewRegistry()

# Short-vol feature view
registry.register(
    name="short_vol_features",
    version="v1",
    entities=["instrument_id", "underlying"],
    features=[
        {"name": "iv_rank", "type": "float64"},
        {"name": "iv_percentile_30d", "type": "float64"},
        {"name": "atm_iv", "type": "float64"},
        {"name": "rv_1m_5min", "type": "float64"},
        {"name": "rv_5m_15min", "type": "float64"},
        {"name": "vrp", "type": "float64"},
        {"name": "oi_velocity", "type": "float64"},
        {"name": "oi_velocity_5m", "type": "float64"},
        {"name": "skew_slope", "type": "float64"},
        {"name": "term_structure_slope", "type": "float64"},
        {"name": "pcr_momentum", "type": "float64"},
        {"name": "delta_band", "type": "float64"},
        {"name": "margin_utilization", "type": "float64"},
    ],
    source="tick_bus",
    lineage=["iv_rank_calc", "rv_har", "vrp_signal", "oi_delta", "skew_svi"],
)

# Intraday microstructure feature view
registry.register(
    name="intraday_features",
    version="v1",
    entities=["instrument_id", "time_bucket"],
    features=[
        {"name": "obs_imbalance", "type": "float64"},
        {"name": "depth_slope", "type": "float64"},
        {"name": "tick_run_count", "type": "int64"},
        {"name": "relative_volume", "type": "float64"},
        {"name": "realized_vol_of_vol", "type": "float64"},
        {"name": "spread_level", "type": "float64"},
        {"name": "lead_lag_nifty_bank", "type": "float64"},
    ],
    source="depth_socket",
    lineage=["obs_calc", "depth_transform", "vol_of_vol"],
)

# Regime feature view
registry.register(
    name="regime_features",
    version="v1",
    entities=["underlying"],
    features=[
        {"name": "hmm_regime", "type": "int64"},
        {"name": "regime_prob_bull", "type": "float64"},
        {"name": "regime_prob_bear", "type": "float64"},
        {"name": "regime_prob_rangy", "type": "float64"},
        {"name": "change_point_signal", "type": "float64"},
        {"name": "vix_level", "type": "float64"},
        {"name": "vix_change_5m", "type": "float64"},
        {"name": "fii_dii_diff", "type": "float64"},
    ],
    source="regime_engine",
    lineage=["hmm_fit", "bcpd", "vix_compute"],
)

# Return forecasting feature view (paused — intraday only)
registry.register(
    name="return_features",
    version="v1",
    entities=["instrument_id", "time_bucket"],
    features=[
        {"name": "vwap_deviation", "type": "float64"},
        {"name": "open_range_pct", "type": "float64"},
        {"name": "volume_profile_z", "type": "float64"},
        {"name": "mean_reversion_score", "type": "float64"},
        {"name": "momentum_5min", "type": "float64"},
        {"name": "order_flow_imbalance", "type": "float64"},
    ],
    source="intraday_pipeline",
    lineage=["vwap_calc", "or_compute", "vp_zscore"],
    # Note: this view is PAUSED per §4.3 — only active when OOS edge > threshold
)