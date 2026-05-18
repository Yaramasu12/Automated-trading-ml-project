from __future__ import annotations

import gzip
import json
import logging
import shutil
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from trading_platform.trace.models import DecisionTrace
from trading_platform.agent.market_hours import now_ist

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path("data/decision_traces")
_DEFAULT_DB_PATH = Path("data/trading.db")
_SECRET_KEYS = {"token", "secret", "password", "api_key", "credential", "pin", "totp", "key"}
_DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
_DEFAULT_UNCOMPRESSED_DAYS = 7
_STARTUP_RELOAD_LIMIT = 500

_CREATE_TRACES_TABLE = """
CREATE TABLE IF NOT EXISTS decision_traces (
    trace_id    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    payload     TEXT NOT NULL
)
"""
_CREATE_TRACES_IDX = "CREATE INDEX IF NOT EXISTS idx_dt_created_at ON decision_traces(created_at)"


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
    """Append-only JSONL-based decision trace store with SQLite mirror.

    Thread-safe. Each trace is a single JSON line in a date-partitioned file
    and is also mirrored to the ``decision_traces`` table in data/trading.db
    for durable hot-reload on restart.
    """

    def __init__(
        self,
        base_dir: Path | str | None = None,
        *,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
        keep_uncompressed_days: int = _DEFAULT_UNCOMPRESSED_DAYS,
        db_path: Path | str | None = None,
    ) -> None:
        self._base_dir = Path(base_dir) if base_dir else _DEFAULT_DIR
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._max_file_bytes = max_file_bytes
        self._keep_uncompressed_days = keep_uncompressed_days
        self._lock = threading.Lock()
        self._traces: dict[str, DecisionTrace] = {}
        self._db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._db_local = threading.local()
        self._init_db()
        self._reload_from_db()

    def _db_conn(self) -> sqlite3.Connection:
        if not getattr(self._db_local, "conn", None):
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._db_local.conn = conn
        return self._db_local.conn

    def _init_db(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._db_conn()
            conn.execute(_CREATE_TRACES_TABLE)
            conn.execute(_CREATE_TRACES_IDX)
            conn.commit()
        except Exception as exc:
            logger.warning("TraceStore: could not init SQLite schema: %s", exc)

    def _reload_from_db(self) -> None:
        """Load the most recent traces from SQLite into the in-memory index on startup."""
        try:
            conn = self._db_conn()
            cur = conn.execute(
                "SELECT trace_id, payload FROM decision_traces "
                "ORDER BY created_at DESC LIMIT ?",
                (_STARTUP_RELOAD_LIMIT,),
            )
            count = 0
            for row in cur.fetchall():
                try:
                    d = json.loads(row[1])
                    t = DecisionTrace.from_dict(d)
                    self._traces[t.trace_id] = t
                    count += 1
                except Exception:
                    continue
            if count:
                logger.info("TraceStore: reloaded %d traces from SQLite on startup", count)
        except Exception as exc:
            logger.warning("TraceStore: could not reload from SQLite: %s", exc)

    def _db_upsert(self, trace_id: str, created_at: str, payload_json: str) -> None:
        try:
            conn = self._db_conn()
            conn.execute(
                "INSERT OR REPLACE INTO decision_traces (trace_id, created_at, payload) VALUES (?, ?, ?)",
                (trace_id, created_at, payload_json),
            )
            conn.commit()
        except Exception as exc:
            logger.warning("TraceStore: SQLite upsert failed for %s: %s", trace_id, exc)

    def _current_file(self) -> Path:
        return self._base_dir / f"traces_{now_ist().date().isoformat()}.jsonl"

    def save(self, trace: DecisionTrace) -> None:
        safe_dict = _scrub_secrets(trace.to_dict())
        payload_json = json.dumps(safe_dict, separators=(",", ":"))
        with self._lock:
            self._traces[trace.trace_id] = trace
            # Mirror to SQLite for durable hot-reload on restart
            self._db_upsert(
                trace.trace_id,
                trace.created_at.isoformat(),
                payload_json,
            )
            try:
                self._maintenance()
                path = self._writable_file()
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(payload_json + "\n")
            except OSError as exc:
                logger.warning("TraceStore: could not write trace %s: %s", trace.trace_id, exc)

    def get(self, trace_id: str) -> DecisionTrace | None:
        with self._lock:
            if trace_id in self._traces:
                return self._traces[trace_id]
        # Scan newest files and newest rows first. Trace updates are append-only,
        # so the latest matching row is the authoritative replay state.
        for path in self._trace_paths():
            try:
                for line in reversed(self._read_lines(path)):
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
        for path in self._trace_paths():
            try:
                lines = self._read_lines(path)
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
        for path in self._trace_paths():
            try:
                total += sum(1 for line in self._read_lines(path) if line.strip())
            except OSError:
                pass
        return total

    def _writable_file(self) -> Path:
        current = self._current_file()
        if current.exists() and current.stat().st_size >= self._max_file_bytes:
            rollover = self._base_dir / f"traces_{now_ist().date().isoformat()}_{datetime.now(timezone.utc).strftime('%H%M%S')}.jsonl"
            if not rollover.exists():
                current.rename(rollover)
        return current

    def _maintenance(self) -> None:
        cutoff = now_ist().date() - timedelta(days=self._keep_uncompressed_days)
        for path in self._base_dir.glob("traces_*.jsonl"):
            path_date = self._date_from_path(path)
            if path_date is None or path_date >= cutoff:
                continue
            gz_path = path.with_suffix(path.suffix + ".gz")
            if gz_path.exists():
                path.unlink(missing_ok=True)
                continue
            with path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            path.unlink(missing_ok=True)

    def _trace_paths(self) -> list[Path]:
        paths = list(self._base_dir.glob("traces_*.jsonl")) + list(self._base_dir.glob("traces_*.jsonl.gz"))
        return sorted(paths, reverse=True)

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return fh.read().splitlines()
        return path.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def _date_from_path(path: Path) -> date | None:
        stem = path.name.removeprefix("traces_")
        raw = stem[:10]
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
