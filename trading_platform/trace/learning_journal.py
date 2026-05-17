from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "paper_learning_journal.db"

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS paper_learning_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    occurred_at     TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    trace_id        TEXT,
    order_id        TEXT,
    trade_id        TEXT,
    symbol          TEXT,
    strategy_name   TEXT,
    regime          TEXT,
    side            TEXT,
    quantity        INTEGER,
    execution_mode  TEXT NOT NULL,
    source          TEXT NOT NULL,
    payload         TEXT
)
"""

_CREATE_IDX_TRACE = "CREATE INDEX IF NOT EXISTS idx_ple_trace ON paper_learning_events(trace_id, id)"
_CREATE_IDX_ORDER = "CREATE INDEX IF NOT EXISTS idx_ple_order ON paper_learning_events(order_id, id)"
_CREATE_IDX_EVENT = "CREATE INDEX IF NOT EXISTS idx_ple_event ON paper_learning_events(event_type, id)"
_CREATE_IDX_SYMBOL = "CREATE INDEX IF NOT EXISTS idx_ple_symbol ON paper_learning_events(symbol, occurred_at)"

VALID_EVENT_TYPES = frozenset({
    "fill_recorded",
    "slippage_recorded",
    "outcome_label_created",
    "post_trade_learning_updated",
    "emergency_square_off",
})


class PaperLearningJournal:
    """Append-only durable journal for paper/shadow learning evidence.

    OMS records order state transitions and TraceStore records the decision story.
    This journal records the paper-trading evidence needed for M4 readiness:
    fills, slippage, generated labels, and post-trade learning updates.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    @contextmanager
    def _cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        conn = self._conn()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()

    def _init_schema(self) -> None:
        with self._cursor() as cur:
            cur.execute(_CREATE_EVENTS)
            cur.execute(_CREATE_IDX_TRACE)
            cur.execute(_CREATE_IDX_ORDER)
            cur.execute(_CREATE_IDX_EVENT)
            cur.execute(_CREATE_IDX_SYMBOL)

    def _next_event_id(self) -> str:
        with self._seq_lock:
            self._seq += 1
            return f"ple_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{self._seq:06d}"

    def append(
        self,
        event_type: str,
        *,
        trace_id: str = "",
        order_id: str = "",
        trade_id: str = "",
        symbol: str = "",
        strategy_name: str = "",
        regime: str = "",
        side: str = "",
        quantity: int | None = None,
        execution_mode: str = "PAPER",
        source: str = "runtime",
        payload: dict | None = None,
    ) -> str:
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"Unknown paper learning event type: {event_type}")
        event_id = self._next_event_id()
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO paper_learning_events
                   (event_id, occurred_at, event_type, trace_id, order_id, trade_id,
                    symbol, strategy_name, regime, side, quantity, execution_mode,
                    source, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    datetime.now(timezone.utc).isoformat(),
                    event_type,
                    trace_id,
                    order_id,
                    trade_id,
                    symbol,
                    strategy_name,
                    regime,
                    side,
                    quantity,
                    execution_mode,
                    source,
                    json.dumps(payload or {}),
                ),
            )
        return event_id

    def events_for_trace(self, trace_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM paper_learning_events WHERE trace_id=? ORDER BY id",
                (trace_id,),
            )
            return [self._decode(dict(row)) for row in cur.fetchall()]

    def recent_events(
        self,
        limit: int = 50,
        *,
        trace_id: str | None = None,
        event_type: str | None = None,
        execution_mode: str | None = None,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if trace_id:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        if event_type:
            if event_type not in VALID_EVENT_TYPES:
                raise ValueError(f"Unknown paper learning event type: {event_type}")
            clauses.append("event_type = ?")
            params.append(event_type)
        if execution_mode:
            clauses.append("execution_mode = ?")
            params.append(execution_mode)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"SELECT * FROM paper_learning_events {where} ORDER BY id DESC LIMIT ?",
                (*params, int(limit)),
            )
            return [self._decode(dict(row)) for row in cur.fetchall()]

    def labels_for_trace(self, trace_id: str) -> list[dict]:
        return [
            dict(event.get("payload") or {})
            for event in self.events_for_trace(trace_id)
            if event.get("event_type") == "outcome_label_created"
        ]

    def summary(self, limit: int = 25, *, trace_id: str | None = None) -> dict:
        clauses: list[str] = []
        params: list[object] = []
        if trace_id:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"SELECT event_type, COUNT(*) AS count FROM paper_learning_events {where} GROUP BY event_type",
                params,
            )
            event_counts = {row["event_type"]: row["count"] for row in cur.fetchall()}
        recent = self.recent_events(limit=limit, trace_id=trace_id)
        slip_values = [
            float((event.get("payload") or {}).get("realized_slippage_pct", 0.0))
            for event in recent
            if event.get("event_type") == "slippage_recorded"
        ]
        return {
            "db_path": str(self.db_path),
            "trace_id": trace_id,
            "event_counts": event_counts,
            "fill_count": int(event_counts.get("fill_recorded", 0)),
            "slippage_count": int(event_counts.get("slippage_recorded", 0)),
            "label_count": int(event_counts.get("outcome_label_created", 0)),
            "learning_update_count": int(event_counts.get("post_trade_learning_updated", 0)),
            "average_recent_slippage_pct": (
                sum(slip_values) / len(slip_values) if slip_values else 0.0
            ),
            "recent_events": recent,
        }

    @staticmethod
    def _decode(row: dict) -> dict:
        payload = row.get("payload")
        if isinstance(payload, str) and payload:
            try:
                row["payload"] = json.loads(payload)
            except json.JSONDecodeError:
                row["payload"] = {"raw": payload}
        elif payload is None:
            row["payload"] = {}
        return row

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            self._local.conn = None

    def checkpoint(self) -> None:
        conn = self._conn()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
