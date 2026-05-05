from __future__ import annotations

import fcntl
import json
import logging
import math
from dataclasses import asdict
from datetime import date
from pathlib import Path

from trading_platform.ai.features import FeatureSnapshot

logger = logging.getLogger(__name__)

_DEFAULT_STORE_DIR = Path(__file__).parent.parent.parent / "data" / "feature_store"

_DRIFT_KEYS = ["momentum_5", "momentum_20", "realized_volatility", "volume_ratio", "trend_strength"]


class FeatureStore:
    """Persistent JSONL-based store for feature snapshots used in ML training and drift detection."""

    def __init__(self, store_dir: Path | None = None):
        self.store_dir = store_dir or _DEFAULT_STORE_DIR
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        safe = symbol.replace("/", "_").replace(":", "_").replace(" ", "_")
        return self.store_dir / f"{safe}.jsonl"

    def append(self, symbol: str, as_of: date, features: FeatureSnapshot, regime: str) -> None:
        """Append one feature record atomically using an exclusive file lock."""
        if not symbol:
            return
        record = {
            "date": as_of.isoformat(),
            "symbol": symbol,
            "regime": regime,
            **asdict(features),
        }
        path = self._path(symbol)
        with path.open("a") as fp:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
            try:
                fp.write(json.dumps(record) + "\n")
                fp.flush()
            finally:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)

    def load(self, symbol: str, limit: int | None = None) -> list[dict]:
        """Return records for symbol, newest last. Optionally cap to `limit` most recent."""
        path = self._path(symbol)
        if not path.exists():
            return []
        records: list[dict] = []
        with path.open() as fp:
            for line in fp:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        logger.warning("Skipping malformed JSONL line in %s: %s", path.name, exc)
                        continue
        return records[-limit:] if limit else records

    def all_symbols(self) -> list[str]:
        return [p.stem for p in self.store_dir.glob("*.jsonl")]

    def all_records(self) -> list[dict]:
        records: list[dict] = []
        for symbol in self.all_symbols():
            records.extend(self.load(symbol))
        return records

    def count(self, symbol: str) -> int:
        return len(self.load(symbol))

    def clear(self, symbol: str) -> None:
        path = self._path(symbol)
        if path.exists():
            path.unlink()

    def clear_all(self) -> None:
        for p in self.store_dir.glob("*.jsonl"):
            p.unlink()

    def feature_drift_score(self, symbol: str, window: int = 20) -> float:
        """Compute population drift via normalised mean shift.

        Compares the *oldest* `window` records vs the most recent `window` records.
        Returns a score in [0, ∞) — values above 0.25 indicate meaningful drift.
        """
        records = self.load(symbol)
        if len(records) < window * 2:
            return 0.0
        old = records[-(window * 2) : -window]
        recent = records[-window:]
        shifts: list[float] = []
        for key in _DRIFT_KEYS:
            old_vals = [r[key] for r in old if key in r]
            new_vals = [r[key] for r in recent if key in r]
            if not old_vals or not new_vals:
                continue
            old_mean = sum(old_vals) / len(old_vals)
            new_mean = sum(new_vals) / len(new_vals)
            variance = sum((v - old_mean) ** 2 for v in old_vals) / len(old_vals)
            old_std = math.sqrt(variance) if variance > 0 else 1e-8
            shifts.append(abs(new_mean - old_mean) / old_std)
        return sum(shifts) / len(shifts) if shifts else 0.0

    def regime_distribution(self, symbol: str, limit: int | None = None) -> dict[str, int]:
        """Count of each regime label in stored records."""
        distribution: dict[str, int] = {}
        for record in self.load(symbol, limit=limit):
            regime = record.get("regime", "UNKNOWN")
            distribution[regime] = distribution.get(regime, 0) + 1
        return distribution
