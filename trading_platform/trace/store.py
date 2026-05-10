from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Iterator

from trading_platform.trace.models import DecisionTrace

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path("data/decision_traces")
_SECRET_KEYS = {"token", "secret", "password", "api_key", "credential", "pin", "totp", "key"}


def _scrub_secrets(obj: object) -> object:
    """Recursively remove secret-looking keys from dicts."""
    if isinstance(obj, dict):
        return {
            k: "[REDACTED]" if any(s in k.lower() for s in _SECRET_KEYS) else _scrub_secrets(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub_secrets(i) for i in obj]
    return obj


class TraceStore:
    """Append-only JSONL-based decision trace store.

    Thread-safe. Each trace is a single JSON line in a date-partitioned file.
    """

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self._base_dir = Path(base_dir) if base_dir else _DEFAULT_DIR
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._traces: dict[str, DecisionTrace] = {}

    def _current_file(self) -> Path:
        from datetime import date
        return self._base_dir / f"traces_{date.today().isoformat()}.jsonl"

    def save(self, trace: DecisionTrace) -> None:
        safe_dict = _scrub_secrets(trace.to_dict())
        with self._lock:
            self._traces[trace.trace_id] = trace
            try:
                with self._current_file().open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(safe_dict) + "\n")
            except OSError as exc:
                logger.warning("TraceStore: could not write trace %s: %s", trace.trace_id, exc)

    def get(self, trace_id: str) -> DecisionTrace | None:
        with self._lock:
            if trace_id in self._traces:
                return self._traces[trace_id]
        # Scan newest files and newest rows first. Trace updates are append-only,
        # so the latest matching row is the authoritative replay state.
        for path in sorted(self._base_dir.glob("traces_*.jsonl"), reverse=True):
            try:
                for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                    if not line.strip():
                        continue
                    d = json.loads(line)
                    if d.get("trace_id") == trace_id:
                        t = DecisionTrace.from_dict(d)
                        with self._lock:
                            self._traces[trace_id] = t
                        return t
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def iter_recent(self, max_traces: int = 100) -> Iterator[dict]:
        """Yield the most recent traces as dicts (newest first)."""
        collected: list[dict] = []
        seen: set[str] = set()
        for path in sorted(self._base_dir.glob("traces_*.jsonl"), reverse=True):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                for line in reversed(lines):
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    trace_id = str(item.get("trace_id", ""))
                    if trace_id in seen:
                        continue
                    seen.add(trace_id)
                    collected.append(item)
                    if len(collected) >= max_traces:
                        break
            except OSError:
                continue
            if len(collected) >= max_traces:
                break
        yield from collected[:max_traces]

    def count(self) -> int:
        total = 0
        for path in self._base_dir.glob("traces_*.jsonl"):
            try:
                total += sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
            except OSError:
                pass
        return total
