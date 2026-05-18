from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Callable

from trading_platform.ai.features import FeatureSnapshot

# fcntl is Unix-only; provide a no-op fallback so the module imports on Windows.
try:
    import fcntl as _fcntl

    def _flock_ex(fd: int) -> None:
        _fcntl.flock(fd, _fcntl.LOCK_EX)

    def _flock_un(fd: int) -> None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)

except ImportError:  # pragma: no cover — Windows path
    _flock_ex = lambda fd: None  # noqa: E731
    _flock_un = lambda fd: None  # noqa: E731

logger = logging.getLogger(__name__)

_DEFAULT_STORE_DIR = Path(__file__).parent.parent.parent / "data" / "feature_store"
_DEFAULT_MAX_ROWS_PER_SYMBOL = 5_000

_DRIFT_KEYS = ["momentum_5", "momentum_20", "realized_volatility", "volume_ratio", "trend_strength"]


class FeatureStore:
    """Persistent JSONL-based store for feature snapshots used in ML training and drift detection.

    Optional publish hook
    ---------------------
    Call set_publish_hook(fn) once after construction to receive a callback
    (symbol: str, record: dict) every time a feature row is appended.
    The runtime uses this to publish BusTopic.FEATURES to the TypedTopicBus
    without coupling FeatureStore to the streaming layer.
    """

    def __init__(self, store_dir: Path | None = None, max_rows_per_symbol: int = _DEFAULT_MAX_ROWS_PER_SYMBOL):
        self.store_dir = store_dir or _DEFAULT_STORE_DIR
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.max_rows_per_symbol = max_rows_per_symbol
        self._publish_hook: "Callable[[str, dict], None] | None" = None
        # In-memory row count cache: avoids reading the full file on every append.
        # Populated lazily on first append and incremented on each write.
        self._row_counts: dict[str, int] = {}

    def set_publish_hook(self, fn: "Callable[[str, dict], None]") -> None:
        """Register a callback invoked on every append(). Thread-safe (write-once)."""
        self._publish_hook = fn

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

        # Lazily initialize the in-memory row count from the file on first append.
        if symbol not in self._row_counts:
            try:
                self._row_counts[symbol] = sum(1 for line in path.open() if line.strip()) if path.exists() else 0
            except OSError:
                self._row_counts[symbol] = 0

        with path.open("a") as fp:
            _flock_ex(fp.fileno())
            try:
                fp.write(json.dumps(record) + "\n")
                fp.flush()
                self._row_counts[symbol] = self._row_counts.get(symbol, 0) + 1
                # Only rewrite the file when the count actually exceeds the cap —
                # avoids full read+write on the hot path (59 symbols × every scan).
                if self.max_rows_per_symbol > 0 and self._row_counts[symbol] > self.max_rows_per_symbol:
                    self._trim_locked_file(path, symbol)
            finally:
                _flock_un(fp.fileno())

        # Notify streaming bus if a publish hook is registered
        if self._publish_hook is not None:
            try:
                self._publish_hook(symbol, record)
            except Exception as _hook_exc:
                logger.debug("FeatureStore publish hook error: %s", _hook_exc)

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

    def _trim_locked_file(self, path: Path, symbol: str | None = None) -> None:
        """Rewrite the file keeping only the most recent max_rows_per_symbol lines.

        Called only when the in-memory row count exceeds the cap, so the expensive
        full read+write does not happen on every append.
        """
        if self.max_rows_per_symbol <= 0:
            return
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return
        if len(lines) <= self.max_rows_per_symbol:
            if symbol:
                self._row_counts[symbol] = len(lines)
            return
        keep = lines[-self.max_rows_per_symbol:]
        path.write_text("\n".join(keep) + "\n")
        if symbol:
            self._row_counts[symbol] = len(keep)

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
