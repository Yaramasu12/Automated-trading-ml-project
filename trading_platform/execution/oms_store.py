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
    metadata        TEXT,
    algo_id         TEXT,
    signal_hash     TEXT
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
    # 2026-07 audit-fix events
    "fill_unresolved",            # C1: no terminal state within tracking window
    "fill_price_missing",         # M2: FILLED reported without an average price
    "mode_mismatch_rejected",     # H3: intent stamped in another execution mode
    "duplicate_suppressed",       # H5: idempotency re-check under instrument lock
    "queue_full_exit_deferred",   # M4: exit intent deferred, plan stays armed
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

    def __init__(self, db_path: Path | None = None, algo_id: str | None = None) -> None:
        self.db_path = db_path or _DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._seq = 0
        self._seq_lock = threading.Lock()
        # SEBI retail-algo compliance groundwork (REDESIGN_PROMPT.md §6.2):
        # the exchange-issued Algo-ID is a platform-wide constant for the
        # lifetime of a deployment, not a per-order value — set once here so
        # every append() call stamps it automatically without every one of
        # the 20+ call sites across execution/scheduler.py needing to pass
        # it explicitly. None/empty until a real Algo-ID has actually been
        # obtained by registering with Angel One (a real-world business
        # process this codebase can't do on its own); every row is simply
        # untagged until then, same as today.
        self._algo_id = algo_id or None
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            # WAL mode needs a shared-memory-mapped -shm sidecar file alongside
            # the main .db file, which doesn't reliably work over a Docker
            # Desktop Windows bind mount — confirmed 2026-07-29: every write
            # from this store 500'd with "sqlite3.OperationalError: unable to
            # open database file" raised from exactly this PRAGMA, even though
            # the plain sqlite3.connect() one line above it succeeded (the .db
            # file itself opens fine; only the WAL sidecar setup fails). This
            # store is written concurrently by both the trading-api and
            # scheduler containers via the same host-mounted ./data volume,
            # which is exactly the multi-process-over-bind-mount case where
            # WAL is least reliable. DELETE (the traditional rollback journal)
            # needs no shared memory mapping and is the standard workaround.
            conn.execute("PRAGMA journal_mode=DELETE")
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
            # Migration for pre-existing databases created before algo_id/
            # signal_hash existed: CREATE TABLE IF NOT EXISTS above is a
            # no-op against an already-created table, so an upgrade needs an
            # explicit ALTER TABLE. Checked via PRAGMA rather than a bare
            # try/except so this is idempotent and doesn't mask a genuine
            # schema error under a different cause.
            existing_columns = {row[1] for row in cur.execute("PRAGMA table_info(oms_events)").fetchall()}
            for column in ("algo_id", "signal_hash"):
                if column not in existing_columns:
                    cur.execute(f"ALTER TABLE oms_events ADD COLUMN {column} TEXT")

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
        algo_id: str | None = None,
        signal_hash: str | None = None,
    ) -> str:
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"Unknown OMS event type: {event_type}")
        event_id = self._next_event_id()
        # Falls back to the platform-wide Algo-ID set at construction — see
        # __init__'s docstring comment. An explicit per-call value (rare;
        # mainly for tests) always wins.
        effective_algo_id = algo_id if algo_id is not None else self._algo_id
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO oms_events
                   (event_id, occurred_at, event_type, order_id, idempotency_key,
                    symbol, strategy_name, side, quantity, price, priority,
                    broker_order_id, fill_price, fill_qty, rejection_reason, metadata,
                    algo_id, signal_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    effective_algo_id,
                    signal_hash,
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

    # Orders whose MOST RECENT event is one of these two mean the local
    # system lost track of the order's true broker-side state — the
    # "position may exist at the broker without ledger/exit tracking" case
    # scheduler.py's tracking timeout already logs per-order. This is the
    # exact orphan signal REDESIGN_PROMPT.md §6.3 asks for; it was already
    # being recorded, just with no query to surface it (confirmed
    # 2026-08-06: no "list still-open orders" query existed at all).
    _UNRESOLVED_EVENT_TYPES = ("fill_unresolved", "fill_price_missing")

    def unresolved_orders(self, since: str | None = None, limit: int = 200) -> list[dict]:
        """Orders whose latest recorded event is fill_unresolved or
        fill_price_missing — i.e. the broker's true fill state was never
        confirmed within the scheduler's tracking window.

        `since` (an ISO-8601 occurred_at string) restricts to recent rows.
        This matters because the append-only OMS log has no "resolved"
        event type — once an order lands here, it stays here forever
        unless some later event (of ANY type) gets appended for the same
        order_id, even if a human fixed the real position by hand. Without
        a `since` filter this can surface stale, already-handled orders
        indefinitely; callers wanting "is this still a live problem" should
        pass a recent cutoff (e.g. the last few hours) rather than treat an
        old row here as necessarily still open today.
        """
        query = """
            SELECT e.* FROM oms_events e
            INNER JOIN (
                SELECT order_id, MAX(id) AS max_id
                FROM oms_events
                GROUP BY order_id
            ) latest ON e.order_id = latest.order_id AND e.id = latest.max_id
            WHERE e.event_type IN ({placeholders})
        """.format(placeholders=",".join("?" * len(self._UNRESOLVED_EVENT_TYPES)))
        params: list = list(self._UNRESOLVED_EVENT_TYPES)
        if since is not None:
            query += " AND e.occurred_at >= ?"
            params.append(since)
        query += " ORDER BY e.id DESC LIMIT ?"
        params.append(limit)
        with self._cursor() as cur:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    def event_count(self) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM oms_events")
            return cur.fetchone()[0]

    def checkpoint(self) -> None:
        # No-op under journal_mode=DELETE (see _conn()) — DELETE mode commits
        # straight to the main .db file each transaction, unlike WAL, which
        # is what this used to flush. Kept as a method so callers (e.g.
        # daily_scheduler.py's per-job checkpoint sweep) don't need to know
        # which journal mode is active.
        pass

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None
