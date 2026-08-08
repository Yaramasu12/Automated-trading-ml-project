"""
L1 — Data Ingestion Layer

Market-data adapters → normalizer → Redpanda event bus → Polars feature pipeline.
"""

from ingestion.adapters.base import MarketDataAdaptor
from ingestion.adapters.mock_replay import MockReplayAdaptor
from ingestion.normalizer import Normalizer
from ingestion.features import FeaturePipeline

__all__ = [
    "MarketDataAdaptor",
    "MockReplayAdaptor",
    "Normalizer",
    "FeaturePipeline",
]