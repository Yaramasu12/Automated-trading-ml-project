from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "oms_events.db"

_CREATE_OMS_EVENTS = """
CREATE TABLE IF NOT EXISTS oms_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    idempotency_key TEXT,
    symbol          TEXT,
    strategy_name   TEXT,
    side            TEXT,
    quantity        INTEGER,
    price           REAL,
    priority        INTEGER,
    broker_order_id TEXT,
    fill_price      REAL,
    fill_qty        INTEGER,
    rejection_reason TEXT,
    metadata        TEXT
)
"""

_CREATE_IDX_ORDER = "CREATE INDEX IF NOT EXISTS idx_oms_order ON oms_events(order_id)"
_CREATE_IDX_IDEM = "CREATE INDEX IF NOT EXISTS idx_oms_idem ON oms_events(idempotency_key)"
_CREATE_IDX_SYM = "CREATE INDEX IF NOT EXISTS idx_oms_sym ON oms_events(symbol, occurred_at)"

VALID_EVENT_TYPES = frozenset({
    "intent_queued",
    "compliance_approved",
    "compliance_rejected",
    "capital_check_passed",
    "capital_check_failed",
    "risk_approved",
    "risk_rejected",
    "lock_acquired",
    "lock_released",
    "broker_submitted",
    "broker_acknowledged",
    "broker_filled",
    "broker_partially_filled",
    "broker_rejected",
    "broker_cancelled",
    "fill_processed",
    "exit_plan_created",
    "position_reconciled",
    "kill_switch_cancelled",
    "expiry_exit_triggered",
    "manual_approval_requested",
    "manual_approval_approved",
    "manual_approval_rejected",
    "manual_approval_expired",
    "multi_leg_created",
    "multi_leg_completed",
    "multi_leg_rolled_back",
    "square_off_requested",
})


class OMSEventStore:
    """Append-only SQLite OMS event log in WAL mode.

    Every order state transition writes one row. Never updates or deletes.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _DEFAULT_DB_PATH
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
            cur.execute(_CREATE_OMS_EVENTS)
            cur.execute(_CREATE_IDX_ORDER)
            cur.execute(_CREATE_IDX_IDEM)
            cur.execute(_CREATE_IDX_SYM)

    def _next_event_id(self) -> str:
        with self._seq_lock:
            self._seq += 1
            return f"evt_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{self._seq:06d}"

    def append(
        self,
        event_type: str,
        order_id: str,
        idempotency_key: str | None = None,
        symbol: str | None = None,
        strategy_name: str | None = None,
        side: str | None = None,
        quantity: int | None = None,
        price: float | None = None,
        priority: int | None = None,
        broker_order_id: str | None = None,
        fill_price: float | None = None,
        fill_qty: int | None = None,
        rejection_reason: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"Unknown OMS event type: {event_type}")
        event_id = self._next_event_id()
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO oms_events
                   (event_id, occurred_at, event_type, order_id, idempotency_key,
                    symbol, strategy_name, side, quantity, price, priority,
                    broker_order_id, fill_price, fill_qty, rejection_reason, metadata)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    datetime.now(timezone.utc).isoformat(),
                    event_type,
                    order_id,
                    idempotency_key,
                    symbol,
                    strategy_name,
                    side,
                    quantity,
                    price,
                    priority,
                    broker_order_id,
                    fill_price,
                    fill_qty,
                    rejection_reason,
                    json.dumps(metadata) if metadata else None,
                ),
            )
        return event_id

    def is_duplicate(self, idempotency_key: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "SELECT 1 FROM oms_events WHERE idempotency_key=? AND event_type='broker_submitted' LIMIT 1",
                (idempotency_key,),
            )
            return cur.fetchone() is not None

    def events_for_order(self, order_id: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM oms_events WHERE order_id=? ORDER BY id",
                (order_id,),
            )
            return [dict(row) for row in cur.fetchall()]

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM oms_events ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cur.fetchall()]

    def event_count(self) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM oms_events")
            return cur.fetchone()[0]

    def checkpoint(self) -> None:
        conn = self._conn()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            self._local.conn = None
