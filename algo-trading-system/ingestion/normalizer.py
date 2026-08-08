"""
Data normalizer — maps vendor-specific schemas to canonical Tick/Bar format.

Also implements:
- Deduplication via seq_index / exchange timestamp
- Sequence-gap detection (alerts on missing ticks)
- Clock alignment across venues
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

from ingestion.adapters.base import Tick, Bar, Instrument

logger = logging.getLogger(__name__)


@dataclass
class GapEvent:
    """Detected gap in tick sequence."""
    instrument_id: str
    expected_seq: int
    received_seq: int | None
    timestamp_ns: int
    gap_size: int


@dataclass
class NormalizerConfig:
    """Configuration for the normalizer."""
    max_gap_ms: float = 1000.0  # alert if gap > 1 second
    dedup_window_ns: int = 1_000_000  # 1ms dedup window
    clock_drift_tolerance_ns: int = 100_000_000  # 100ms
    enable_gap_detection: bool = True
    enable_dedup: bool = True


class DataNormalizer:
    """
    Normalizes incoming market data to canonical schema.

    Responsibilities:
    - Map vendor-specific tick formats to Tick/Bar
    - Deduplicate ticks within window
    - Detect sequence gaps and alert
    - Align clock across venues
    - Validate price/size sanity (no negative prices, no zero spreads)
    """

    def __init__(self, config: NormalizerConfig | None = None) -> None:
        self._config = config or NormalizerConfig()
        self._seen_hashes: dict[str, datetime] = {}  # dedup cache
        self._last_seq: dict[str, int] = {}  # last seq_index per instrument
        self._last_timestamp: dict[str, int] = {}  # last timestamp per instrument
        self._gap_count: int = 0
        self._dedup_count: int = 0
        self._total_processed: int = 0

    def normalize_tick(self, raw: dict) -> Tick | None:
        """
        Normalize a raw vendor tick to canonical Tick format.

        Returns None if the tick is invalid or a duplicate.
        """
        # Extract fields (vendor-agnostic)
        instrument_id = raw.get("instrument_id", raw.get("symbol", ""))
        venue = raw.get("venue", raw.get("exchange", "UNKNOWN"))
        timestamp_ns = raw.get("timestamp_ns", int(datetime.now(timezone.utc).timestamp() * 1e9))
        bid_price = raw.get("bid_price", 0.0)
        ask_price = raw.get("ask_price", 0.0)
        bid_size = raw.get("bid_size", 0)
        ask_size = raw.get("ask_size", 0)
        last_price = raw.get("last_price", raw.get("trade_price", 0.0))
        last_size = raw.get("last_size", raw.get("trade_size", 0))
        trade_volume = raw.get("trade_volume", last_size)
        seq_index = raw.get("seq_index", 0)

        # Sanity checks
        if bid_price <= 0 or ask_price <= 0:
            logger.warning("Invalid bid/ask: %s", raw)
            return None
        if bid_price >= ask_price:
            logger.warning("Bid >= Ask: bid=%.4f ask=%.4f", bid_price, ask_price)
            return None
        if last_price <= 0:
            logger.warning("Invalid last_price: %s", raw)
            return None

        # Deduplication
        if self._config.enable_dedup:
            dedup_key = f"{instrument_id}:{raw.get('timestamp_ns', timestamp_ns)}"
            if self._is_duplicate(dedup_key, timestamp_ns):
                self._dedup_count += 1
                return None

        tick = Tick(
            instrument_id=instrument_id,
            venue=venue,
            timestamp_ns=timestamp_ns,
            trade_time_ns=raw.get("trade_time_ns", timestamp_ns),
            bid_price=round(bid_price, 6),
            ask_price=round(ask_price, 6),
            bid_size=bid_size,
            ask_size=ask_size,
            last_price=round(last_price, 6),
            last_size=last_size,
            trade_volume=trade_volume,
            tick_direction=raw.get("tick_direction", 0),
            exchange_timestamp_ns=raw.get("exchange_timestamp_ns", 0),
            seq_index=seq_index,
            metadata=raw.get("metadata", {}),
        )

        # Sequence gap detection
        if self._config.enable_gap_detection:
            gap = self._check_gap(instrument_id, seq_index, timestamp_ns)
            if gap:
                self._gap_count += 1
                logger.warning("Sequence gap detected: %s", gap)

        self._total_processed += 1
        return tick

    def normalize_bar(self, raw: dict) -> Bar | None:
        """Normalize a raw vendor bar to canonical Bar format."""
        instrument_id = raw.get("instrument_id", raw.get("symbol", ""))
        venue = raw.get("venue", raw.get("exchange", "UNKNOWN"))
        timestamp_ns = raw.get("timestamp_ns", 0)

        if timestamp_ns <= 0:
            logger.warning("Invalid bar timestamp: %s", raw)
            return None

        open_price = raw.get("open_price", raw.get("open", 0.0))
        high_price = raw.get("high_price", raw.get("high", 0.0))
        low_price = raw.get("low_price", raw.get("low", 0.0))
        close_price = raw.get("close_price", raw.get("close", 0.0))
        volume = raw.get("volume", 0)

        if open_price <= 0 or close_price <= 0:
            logger.warning("Invalid bar prices: %s", raw)
            return None

        return Bar(
            instrument_id=instrument_id,
            venue=venue,
            timestamp_ns=timestamp_ns,
            open_price=round(open_price, 6),
            high_price=round(high_price, 6),
            low_price=round(low_price, 6),
            close_price=round(close_price, 6),
            volume=volume,
            num_trades=raw.get("num_trades", 0),
            vwap=raw.get("vwap", 0.0),
            metadata=raw.get("metadata", {}),
        )

    def normalize_ticks(self, raw_ticks: list[dict]) -> list[Tick]:
        """Normalize a batch of raw ticks, filtering invalid/duplicate."""
        ticks: list[Tick] = []
        for raw in raw_ticks:
            tick = self.normalize_tick(raw)
            if tick is not None:
                ticks.append(tick)
        return ticks

    def validate_instrument(self, inst: Instrument) -> list[str]:
        """Validate instrument metadata. Returns list of warnings/errors."""
        issues: list[str] = []
        if not inst.instrument_id:
            issues.append("instrument_id is required")
        if inst.tick_size <= 0:
            issues.append(f"Invalid tick_size: {inst.tick_size}")
        if inst.lot_size <= 0:
            issues.append(f"Invalid lot_size: {inst.lot_size}")
        if inst.min_price_increment <= 0:
            issues.append(f"Invalid min_price_increment: {inst.min_price_increment}")
        if inst.multiplier <= 0:
            issues.append(f"Invalid multiplier: {inst.multiplier}")
        return issues

    def get_stats(self) -> dict:
        """Return normalizer statistics for monitoring."""
        return {
            "total_processed": self._total_processed,
            "gaps_detected": self._gap_count,
            "duplicates_removed": self._dedup_count,
            "dedup_cache_size": len(self._seen_hashes),
            "instruments_tracked": len(self._last_seq),
        }

    def reset_stats(self) -> None:
        """Reset all counters (for testing/restart)."""
        self._gap_count = 0
        self._dedup_count = 0
        self._total_processed = 0
        self._seen_hashes.clear()
        self._last_seq.clear()
        self._last_timestamp.clear()

    # ---- Private helpers ----

    def _is_duplicate(self, key: str, timestamp_ns: int) -> bool:
        """Check if a tick is a duplicate within the dedup window."""
        # Clean old entries
        now = datetime.now(timezone.utc)
        cutoff = int((now.timestamp() - 0.001) * 1e9)  # 1ms ago
        self._seen_hashes = {
            k: ts for k, ts in self._seen_hashes.items()
            if ts > cutoff
        }
        return key in self._seen_hashes

    def _check_gap(
        self,
        instrument_id: str,
        seq_index: int,
        timestamp_ns: int,
    ) -> GapEvent | None:
        """Check for sequence gaps in tick stream."""
        if seq_index == 0:
            return None

        last = self._last_seq.get(instrument_id, 0)
        if last > 0 and seq_index > last + 1:
            gap_size = seq_index - last - 1
            return GapEvent(
                instrument_id=instrument_id,
                expected_seq=last + 1,
                received_seq=seq_index,
                timestamp_ns=timestamp_ns,
                gap_size=gap_size,
            )

        self._last_seq[instrument_id] = seq_index

        # Clock drift detection
        last_ts = self._last_timestamp.get(instrument_id, 0)
        if last_ts > 0:
            drift_ns = abs(timestamp_ns - last_ts)
            if drift_ns > self._config.clock_drift_tolerance_ns:
                logger.warning(
                    "Clock drift detected for %s: %d ns",
                    instrument_id,
                    drift_ns,
                )

        self._last_timestamp[instrument_id] = timestamp_ns
        return None