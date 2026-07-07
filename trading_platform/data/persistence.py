"""TradingDatabase — dual-backend persistence layer.

Production backend : PostgreSQL 16 + TimescaleDB + pgvector
                     Activated when DATABASE_URL env var starts with "postgresql"
                     or when database_url= is passed to the constructor.

Test / dev backend : SQLite (WAL mode)
                     Activated when db_path= is passed OR no DATABASE_URL is set.

Public API is identical for both backends — all callers are unaffected.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "trading.db"

# ---------------------------------------------------------------------------
# PostgreSQL DDL
# ---------------------------------------------------------------------------

_PG_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS trades (
    trade_id       TEXT        PRIMARY KEY,
    order_id       TEXT        NOT NULL,
    symbol         TEXT        NOT NULL,
    side           TEXT        NOT NULL,
    quantity       INTEGER     NOT NULL,
    price          DOUBLE PRECISION NOT NULL,
    charges        DOUBLE PRECISION NOT NULL DEFAULT 0,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    strategy_name  TEXT        NOT NULL,
    execution_mode TEXT        NOT NULL DEFAULT 'BACKTEST',
    is_test        BOOLEAN     NOT NULL DEFAULT FALSE,
    feature_vector vector(7)
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id             BIGSERIAL   PRIMARY KEY,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    cash           DOUBLE PRECISION NOT NULL,
    equity         DOUBLE PRECISION NOT NULL,
    realized_pnl   DOUBLE PRECISION NOT NULL,
    unrealized_pnl DOUBLE PRECISION NOT NULL,
    drawdown       DOUBLE PRECISION NOT NULL,
    execution_mode TEXT        NOT NULL DEFAULT 'BACKTEST'
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    trade_date     DATE        PRIMARY KEY,
    realized_pnl   DOUBLE PRECISION NOT NULL DEFAULT 0,
    unrealized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_trades   INTEGER     NOT NULL DEFAULT 0,
    winning_trades INTEGER     NOT NULL DEFAULT 0,
    ending_equity  DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS model_runs (
    id             BIGSERIAL   PRIMARY KEY,
    run_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    model_name     TEXT        NOT NULL,
    symbol         TEXT        NOT NULL,
    regime         TEXT,
    signal         TEXT,
    confidence     DOUBLE PRECISION,
    metadata       JSONB
);

CREATE TABLE IF NOT EXISTS risk_events (
    id             BIGSERIAL   PRIMARY KEY,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type     TEXT        NOT NULL,
    symbol         TEXT,
    reason         TEXT        NOT NULL,
    risk_score     DOUBLE PRECISION,
    approved       BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS open_positions (
    symbol          TEXT    NOT NULL,
    execution_mode  TEXT    NOT NULL,
    quantity        INTEGER NOT NULL,
    average_price   DOUBLE PRECISION NOT NULL,
    realized_pnl    DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, execution_mode)
);

CREATE TABLE IF NOT EXISTS active_exit_plans (
    plan_id              TEXT    PRIMARY KEY,
    symbol               TEXT    NOT NULL,
    execution_mode       TEXT    NOT NULL,
    side                 TEXT    NOT NULL,
    entry_price          DOUBLE PRECISION NOT NULL,
    quantity             INTEGER NOT NULL,
    strategy_name        TEXT    NOT NULL,
    stop_loss_price      DOUBLE PRECISION,
    target_price         DOUBLE PRECISION,
    trailing_pct         DOUBLE PRECISION,
    expiry_date          DATE,
    partial_exit_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS market_patterns (
    pattern_id            TEXT    PRIMARY KEY,
    regime                TEXT    NOT NULL,
    news_sentiment_bucket TEXT    NOT NULL,
    volatility_bucket     TEXT    NOT NULL,
    momentum_bucket       TEXT    NOT NULL,
    rsi_bucket            TEXT    NOT NULL,
    feature_vector        vector(7),
    win_rate              DOUBLE PRECISION NOT NULL,
    avg_return            DOUBLE PRECISION NOT NULL,
    sample_size           INTEGER NOT NULL,
    last_updated          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS profit_guard_outcomes (
    id         BIGSERIAL        PRIMARY KEY,
    underlying TEXT             NOT NULL,
    won        BOOLEAN          NOT NULL,
    pnl_pct    DOUBLE PRECISION NOT NULL,
    ts         TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reflections (
    id         BIGSERIAL        PRIMARY KEY,
    trace_id   TEXT             NOT NULL,
    underlying TEXT             NOT NULL,
    won        BOOLEAN          NOT NULL,
    pnl_pct    DOUBLE PRECISION NOT NULL,
    quality    DOUBLE PRECISION NOT NULL,
    regime     TEXT,
    payload    JSONB,
    ts         TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_weights (
    agent_name TEXT             PRIMARY KEY,
    weight     DOUBLE PRECISION NOT NULL,
    updated_at TIMESTAMPTZ      NOT NULL DEFAULT now()
);
"""

_PG_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_trades_ts          ON trades (ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_mode        ON trades (execution_mode);
CREATE INDEX IF NOT EXISTS idx_trades_symbol      ON trades (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_mode     ON portfolio_snapshots (execution_mode, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_runs_symbol  ON model_runs (symbol, run_at DESC);
CREATE INDEX IF NOT EXISTS idx_risk_events_type   ON risk_events (event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_patterns_regime    ON market_patterns (regime);
CREATE INDEX IF NOT EXISTS idx_patterns_vec       ON market_patterns
    USING ivfflat (feature_vector vector_cosine_ops) WITH (lists = 10);
CREATE INDEX IF NOT EXISTS idx_outcomes_underlying ON profit_guard_outcomes (underlying, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reflections_sym     ON reflections (underlying, ts DESC);
"""

_PG_HYPERTABLES = """
SELECT create_hypertable('trades',                'ts',          if_not_exists => TRUE, migrate_data => TRUE);
SELECT create_hypertable('portfolio_snapshots',   'recorded_at', if_not_exists => TRUE, migrate_data => TRUE);
SELECT create_hypertable('model_runs',            'run_at',      if_not_exists => TRUE, migrate_data => TRUE);
SELECT create_hypertable('risk_events',           'occurred_at', if_not_exists => TRUE, migrate_data => TRUE);
SELECT create_hypertable('profit_guard_outcomes', 'ts',          if_not_exists => TRUE, migrate_data => TRUE);
SELECT create_hypertable('reflections',           'ts',          if_not_exists => TRUE, migrate_data => TRUE);
"""

# ---------------------------------------------------------------------------
# SQLite DDL  (unchanged from original — used by tests and local dev)
# ---------------------------------------------------------------------------

_SL_INIT = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id      TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    quantity      INTEGER NOT NULL,
    price         REAL NOT NULL,
    charges       REAL NOT NULL DEFAULT 0,
    timestamp     TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    execution_mode TEXT NOT NULL DEFAULT 'BACKTEST',
    is_test       INTEGER NOT NULL DEFAULT 0,
    feature_vector TEXT
);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at   TEXT NOT NULL,
    cash          REAL NOT NULL,
    equity        REAL NOT NULL,
    realized_pnl  REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    drawdown      REAL NOT NULL,
    execution_mode TEXT NOT NULL DEFAULT 'BACKTEST'
);
CREATE TABLE IF NOT EXISTS daily_pnl (
    trade_date    TEXT PRIMARY KEY,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    total_trades  INTEGER NOT NULL DEFAULT 0,
    winning_trades INTEGER NOT NULL DEFAULT 0,
    ending_equity REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS model_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TEXT NOT NULL,
    model_name    TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    regime        TEXT,
    signal        TEXT,
    confidence    REAL,
    metadata      TEXT
);
CREATE TABLE IF NOT EXISTS risk_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at   TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    symbol        TEXT,
    reason        TEXT NOT NULL,
    risk_score    REAL,
    approved      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS open_positions (
    symbol          TEXT NOT NULL,
    execution_mode  TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    average_price   REAL NOT NULL,
    realized_pnl    REAL NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (symbol, execution_mode)
);
CREATE TABLE IF NOT EXISTS active_exit_plans (
    plan_id              TEXT PRIMARY KEY,
    symbol               TEXT NOT NULL,
    execution_mode       TEXT NOT NULL,
    side                 TEXT NOT NULL,
    entry_price          REAL NOT NULL,
    quantity             INTEGER NOT NULL,
    strategy_name        TEXT NOT NULL,
    stop_loss_price      REAL,
    target_price         REAL,
    trailing_pct         REAL,
    expiry_date          TEXT,
    partial_exit_enabled INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_patterns (
    pattern_id            TEXT PRIMARY KEY,
    regime                TEXT NOT NULL,
    news_sentiment_bucket TEXT NOT NULL,
    volatility_bucket     TEXT NOT NULL,
    momentum_bucket       TEXT NOT NULL,
    rsi_bucket            TEXT NOT NULL,
    feature_vector        TEXT NOT NULL,
    win_rate              REAL NOT NULL,
    avg_return            REAL NOT NULL,
    sample_size           INTEGER NOT NULL,
    last_updated          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profit_guard_outcomes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    underlying TEXT    NOT NULL,
    won        INTEGER NOT NULL,
    pnl_pct    REAL    NOT NULL,
    ts         TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS reflections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id   TEXT    NOT NULL,
    underlying TEXT    NOT NULL,
    won        INTEGER NOT NULL,
    pnl_pct    REAL    NOT NULL,
    quality    REAL    NOT NULL,
    regime     TEXT,
    payload    TEXT,
    ts         TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_weights (
    agent_name TEXT PRIMARY KEY,
    weight     REAL NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_ts        ON trades (timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_mode      ON trades (execution_mode);
CREATE INDEX IF NOT EXISTS idx_trades_is_test   ON trades (is_test);
CREATE INDEX IF NOT EXISTS idx_snapshots_mode   ON portfolio_snapshots (execution_mode);
CREATE INDEX IF NOT EXISTS idx_outcomes_sym     ON profit_guard_outcomes (underlying, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reflections_sym  ON reflections (underlying, ts DESC);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(cursor, row) -> dict:
    """Convert a psycopg2 or sqlite3 row to a plain dict."""
    if isinstance(row, sqlite3.Row):
        return dict(row)
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


def _vec_to_pg(vec: list) -> str:
    """Convert a Python list to pgvector literal '[1.0,2.0,...]'."""
    return "[" + ",".join(str(float(v)) for v in vec) + "]"


def _vec_from_pg(val) -> list:
    """Parse a pgvector value (string or list) back to Python list."""
    if val is None:
        return []
    if isinstance(val, list):
        return [float(v) for v in val]
    # string form: '[1.0,2.0,...]'
    return [float(v) for v in str(val).strip("[]").split(",") if v.strip()]


# ---------------------------------------------------------------------------
# TradingDatabase  — unified public API
# ---------------------------------------------------------------------------

class TradingDatabase:
    """Thread-safe persistence layer — PostgreSQL (production) or SQLite (tests/dev).

    PostgreSQL is used when DATABASE_URL env var is set or database_url= is passed.
    SQLite is used when db_path= is passed or DATABASE_URL is absent.

    All public methods are identical regardless of backend.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        database_url: str | None = None,
    ) -> None:
        url = database_url or os.getenv("DATABASE_URL", "")
        if url.startswith("postgresql") and db_path is None:
            self._mode = "postgres"
            self._url = url
            self._init_postgres()
        else:
            self._mode = "sqlite"
            self.db_path = db_path or _DEFAULT_DB_PATH
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._local = threading.local()
            self._init_sqlite()

    # ------------------------------------------------------------------
    # PostgreSQL connection pool
    # ------------------------------------------------------------------

    def _init_postgres(self) -> None:
        import psycopg2
        import psycopg2.pool
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2, maxconn=10, dsn=self._url
        )
        self._setup_pg_schema()

    def _setup_pg_schema(self) -> None:
        # Run schema DDL in AUTOCOMMIT so each statement is its own transaction.
        # Otherwise the first failing statement (e.g. an optional TimescaleDB
        # hypertable, or CREATE EXTENSION on a restricted role) aborts the whole
        # transaction in PostgreSQL, every subsequent CREATE TABLE is silently
        # skipped, and the final commit rolls everything back — leaving a fresh
        # database with ZERO tables. Autocommit isolates each statement so the
        # optional/idempotent ones can fail harmlessly without poisoning the rest.
        conn = self._pool.getconn()
        try:
            prev_autocommit = conn.autocommit
            conn.autocommit = True
            cur = conn.cursor()
            try:
                # Order matters: extension + core tables first, then indexes,
                # hypertables, and column migrations (each non-fatal).
                statements = (
                    [s.strip() for s in _PG_DDL.strip().split(";")]
                    + [s.strip() for s in _PG_INDEXES.strip().split(";")]
                    + [s.strip() for s in _PG_HYPERTABLES.strip().split(";")]
                    + [
                        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS ts TIMESTAMPTZ DEFAULT now()",
                        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS feature_vector vector(7)",
                    ]
                )
                for stmt in statements:
                    if not stmt:
                        continue
                    try:
                        cur.execute(stmt)
                    except Exception as exc:
                        # Idempotent re-create, missing pgvector/TimescaleDB, etc.
                        logger.debug("pg schema stmt skipped: %s", exc)
            finally:
                cur.close()
                conn.autocommit = prev_autocommit
        finally:
            self._pool.putconn(conn)

    @contextmanager
    def _cursor(self) -> Generator:
        if self._mode == "postgres":
            conn = self._pool.getconn()
            try:
                cur = conn.cursor()
                try:
                    yield cur
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    cur.close()
            finally:
                self._pool.putconn(conn)
        else:
            conn = self._sqlite_conn()
            cur = conn.cursor()
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()

    @property
    def _ts(self) -> str:
        """Timestamp column name: 'ts' in PostgreSQL, 'timestamp' in SQLite."""
        return "ts" if self._mode == "postgres" else "timestamp"

    def _p(self, sql: str) -> str:
        """Convert SQLite ? placeholders to psycopg2 %s when in postgres mode."""
        if self._mode == "postgres":
            return sql.replace("?", "%s")
        return sql

    def _rows(self, cur, rows) -> list[dict]:
        return [_row_to_dict(cur, r) for r in rows]

    # ------------------------------------------------------------------
    # SQLite connection management
    # ------------------------------------------------------------------

    def _sqlite_conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_sqlite(self) -> None:
        with self._cursor() as cur:
            for stmt in _SL_INIT.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
            self._sl_ensure_column(cur, "trades", "is_test", "INTEGER NOT NULL DEFAULT 0")
            self._sl_ensure_column(cur, "trades", "timestamp", "TEXT NOT NULL DEFAULT ''")
            self._sl_ensure_column(cur, "trades", "feature_vector", "TEXT")

    @staticmethod
    def _sl_ensure_column(cur, table: str, column: str, definition: str) -> None:
        cur.execute(f"PRAGMA table_info({table})")
        existing = {str(row[1]) for row in cur.fetchall()}
        if column not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def save_trade(
        self,
        trade,
        execution_mode: str = "BACKTEST",
        feature_vector: list | None = None,
    ) -> None:
        is_test = trade.strategy_name in {"manual_preview", "trace_fill_test"}
        ts = trade.timestamp.isoformat() if hasattr(trade.timestamp, "isoformat") else str(trade.timestamp)
        if self._mode == "postgres":
            vec_val = _vec_to_pg(feature_vector) if feature_vector else None
            with self._cursor() as cur:
                cur.execute(
                    """INSERT INTO trades
                       (trade_id, order_id, symbol, side, quantity, price, charges,
                        ts, strategy_name, execution_mode, is_test, feature_vector)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)
                       ON CONFLICT (trade_id) DO NOTHING""",
                    (
                        trade.trade_id, trade.order_id, trade.symbol,
                        trade.side.value, trade.quantity, trade.price,
                        trade.charges, ts, trade.strategy_name,
                        execution_mode, is_test, vec_val,
                    ),
                )
        else:
            vec_json = json.dumps(feature_vector) if feature_vector else None
            with self._cursor() as cur:
                cur.execute(
                    """INSERT OR IGNORE INTO trades
                       (trade_id, order_id, symbol, side, quantity, price, charges,
                        timestamp, strategy_name, execution_mode, is_test, feature_vector)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        trade.trade_id, trade.order_id, trade.symbol,
                        trade.side.value, trade.quantity, trade.price,
                        trade.charges, ts, trade.strategy_name,
                        execution_mode, int(is_test), vec_json,
                    ),
                )

    def trades(
        self,
        symbol: str | None = None,
        since: datetime | None = None,
        execution_mode: str | None = None,
        limit: int = 500,
        include_test: bool = False,
    ) -> list[dict]:
        clauses, params = [], []
        if symbol:
            clauses.append("symbol = ?"); params.append(symbol)
        if since:
            clauses.append(f"{self._ts} >= ?"); params.append(since.isoformat())
        if execution_mode:
            clauses.append("execution_mode = ?"); params.append(execution_mode)
        if not include_test:
            if self._mode == "postgres":
                clauses.append("is_test = FALSE")
            else:
                clauses.append("COALESCE(is_test, 0) = 0")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = self._p(f"SELECT * FROM trades {where} ORDER BY {self._ts} DESC LIMIT ?")
        with self._cursor() as cur:
            cur.execute(sql, [*params, limit])
            return self._rows(cur, cur.fetchall())

    def trade_count(self, execution_mode: str | None = None, include_test: bool = False) -> int:
        clauses, params = [], []
        if execution_mode:
            clauses.append("execution_mode=?"); params.append(execution_mode)
        if not include_test:
            if self._mode == "postgres":
                clauses.append("is_test = FALSE")
            else:
                clauses.append("COALESCE(is_test, 0)=0")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = self._p(f"SELECT COUNT(*) FROM trades {where}")
        with self._cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # Portfolio snapshots
    # ------------------------------------------------------------------

    def save_snapshot(self, snapshot, execution_mode: str = "BACKTEST") -> None:
        now = datetime.now(timezone.utc).isoformat()
        sql = self._p(
            """INSERT INTO portfolio_snapshots
               (recorded_at, cash, equity, realized_pnl, unrealized_pnl, drawdown, execution_mode)
               VALUES (?,?,?,?,?,?,?)"""
        )
        with self._cursor() as cur:
            cur.execute(sql, (
                now, snapshot.cash, snapshot.equity,
                snapshot.realized_pnl, snapshot.unrealized_pnl,
                snapshot.drawdown, execution_mode,
            ))

    def latest_snapshot(self, execution_mode: str | None = None) -> dict | None:
        if execution_mode:
            sql = self._p(
                "SELECT * FROM portfolio_snapshots WHERE execution_mode=? ORDER BY id DESC LIMIT 1"
            )
            params = (execution_mode,)
        else:
            sql = "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
            params = ()
        with self._cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return _row_to_dict(cur, row) if row else None

    def equity_curve(self, execution_mode: str | None = None, limit: int = 200) -> list[dict]:
        if execution_mode:
            sql = self._p(
                "SELECT recorded_at, equity, drawdown FROM portfolio_snapshots "
                "WHERE execution_mode=? ORDER BY id DESC LIMIT ?"
            )
            params = (execution_mode, limit)
        else:
            sql = self._p(
                "SELECT recorded_at, equity, drawdown FROM portfolio_snapshots "
                "ORDER BY id DESC LIMIT ?"
            )
            params = (limit,)
        with self._cursor() as cur:
            cur.execute(sql, params)
            return list(reversed(self._rows(cur, cur.fetchall())))

    # ------------------------------------------------------------------
    # Daily P&L
    # ------------------------------------------------------------------

    def upsert_daily_pnl(
        self,
        trade_date: date,
        realized_pnl: float,
        unrealized_pnl: float,
        total_trades: int,
        winning_trades: int,
        ending_equity: float,
    ) -> None:
        if self._mode == "postgres":
            sql = """INSERT INTO daily_pnl
                     (trade_date, realized_pnl, unrealized_pnl, total_trades, winning_trades, ending_equity)
                     VALUES (%s,%s,%s,%s,%s,%s)
                     ON CONFLICT (trade_date) DO UPDATE SET
                       realized_pnl=EXCLUDED.realized_pnl,
                       unrealized_pnl=EXCLUDED.unrealized_pnl,
                       total_trades=EXCLUDED.total_trades,
                       winning_trades=EXCLUDED.winning_trades,
                       ending_equity=EXCLUDED.ending_equity"""
            params = (trade_date, realized_pnl, unrealized_pnl, total_trades, winning_trades, ending_equity)
        else:
            sql = """INSERT INTO daily_pnl
                     (trade_date, realized_pnl, unrealized_pnl, total_trades, winning_trades, ending_equity)
                     VALUES (?,?,?,?,?,?)
                     ON CONFLICT(trade_date) DO UPDATE SET
                       realized_pnl=excluded.realized_pnl,
                       unrealized_pnl=excluded.unrealized_pnl,
                       total_trades=excluded.total_trades,
                       winning_trades=excluded.winning_trades,
                       ending_equity=excluded.ending_equity"""
            params = (trade_date.isoformat(), realized_pnl, unrealized_pnl, total_trades, winning_trades, ending_equity)
        with self._cursor() as cur:
            cur.execute(sql, params)

    def daily_pnl_history(self, limit: int = 30) -> list[dict]:
        sql = self._p("SELECT * FROM daily_pnl ORDER BY trade_date DESC LIMIT ?")
        with self._cursor() as cur:
            cur.execute(sql, (limit,))
            return list(reversed(self._rows(cur, cur.fetchall())))

    # ------------------------------------------------------------------
    # Model runs
    # ------------------------------------------------------------------

    def save_model_run(
        self,
        model_name: str,
        symbol: str,
        regime: str | None = None,
        signal: str | None = None,
        confidence: float | None = None,
        metadata: dict | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if self._mode == "postgres":
            sql = """INSERT INTO model_runs (run_at, model_name, symbol, regime, signal, confidence, metadata)
                     VALUES (%s,%s,%s,%s,%s,%s,%s)"""
            meta_val = json.dumps(metadata) if metadata else None
        else:
            sql = """INSERT INTO model_runs (run_at, model_name, symbol, regime, signal, confidence, metadata)
                     VALUES (?,?,?,?,?,?,?)"""
            meta_val = json.dumps(metadata) if metadata else None
        with self._cursor() as cur:
            cur.execute(sql, (now, model_name, symbol, regime, signal, confidence, meta_val))

    # ------------------------------------------------------------------
    # Risk events
    # ------------------------------------------------------------------

    def save_risk_event(
        self,
        event_type: str,
        reason: str,
        symbol: str | None = None,
        risk_score: float | None = None,
        approved: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if self._mode == "postgres":
            sql = """INSERT INTO risk_events (occurred_at, event_type, symbol, reason, risk_score, approved)
                     VALUES (%s,%s,%s,%s,%s,%s)"""
            params: Any = (now, event_type, symbol, reason, risk_score, approved)
        else:
            sql = """INSERT INTO risk_events (occurred_at, event_type, symbol, reason, risk_score, approved)
                     VALUES (?,?,?,?,?,?)"""
            params = (now, event_type, symbol, reason, risk_score, int(approved))
        with self._cursor() as cur:
            cur.execute(sql, params)

    def recent_risk_events(self, limit: int = 50) -> list[dict]:
        sql = self._p("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?")
        with self._cursor() as cur:
            cur.execute(sql, (limit,))
            return self._rows(cur, cur.fetchall())

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM trades")
            total_trades = cur.fetchone()[0]
            if self._mode == "postgres":
                cur.execute("SELECT COUNT(*) FROM trades WHERE is_test = FALSE")
                production_trades = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM trades WHERE is_test = TRUE")
                test_trades = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM trades WHERE execution_mode='LIVE'")
                live_trades = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM portfolio_snapshots")
                snapshots = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM risk_events WHERE approved = FALSE")
                risk_blocks = cur.fetchone()[0]
            else:
                cur.execute("SELECT COUNT(*) FROM trades WHERE COALESCE(is_test, 0)=0")
                production_trades = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM trades WHERE COALESCE(is_test, 0)=1")
                test_trades = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM trades WHERE execution_mode='LIVE'")
                live_trades = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM portfolio_snapshots")
                snapshots = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM risk_events WHERE approved=0")
                risk_blocks = cur.fetchone()[0]
        backend = f"postgresql ({self._url.split('@')[-1]})" if self._mode == "postgres" else str(self.db_path)
        return {
            "db_backend": self._mode,
            "db_path": backend,
            "total_trades": total_trades,
            "production_trades": production_trades,
            "test_trades": test_trades,
            "live_trades": live_trades,
            "portfolio_snapshots": snapshots,
            "risk_blocks": risk_blocks,
        }

    # ------------------------------------------------------------------
    # Open positions (restart recovery)
    # ------------------------------------------------------------------

    def save_positions(self, positions: dict, execution_mode: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            for symbol, pos in positions.items():
                if pos.quantity == 0:
                    sql = self._p(
                        "DELETE FROM open_positions WHERE symbol=? AND execution_mode=?"
                    )
                    cur.execute(sql, (symbol, execution_mode))
                else:
                    if self._mode == "postgres":
                        cur.execute(
                            """INSERT INTO open_positions
                               (symbol, execution_mode, quantity, average_price, realized_pnl, updated_at)
                               VALUES (%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (symbol, execution_mode) DO UPDATE SET
                                 quantity=EXCLUDED.quantity,
                                 average_price=EXCLUDED.average_price,
                                 realized_pnl=EXCLUDED.realized_pnl,
                                 updated_at=EXCLUDED.updated_at""",
                            (symbol, execution_mode, pos.quantity, pos.average_price,
                             pos.realized_pnl, now),
                        )
                    else:
                        cur.execute(
                            """INSERT INTO open_positions
                               (symbol, execution_mode, quantity, average_price, realized_pnl, updated_at)
                               VALUES (?,?,?,?,?,?)
                               ON CONFLICT(symbol, execution_mode) DO UPDATE SET
                                 quantity=excluded.quantity,
                                 average_price=excluded.average_price,
                                 realized_pnl=excluded.realized_pnl,
                                 updated_at=excluded.updated_at""",
                            (symbol, execution_mode, pos.quantity, pos.average_price,
                             pos.realized_pnl, now),
                        )

    def load_positions(self, execution_mode: str) -> list[dict]:
        sql = self._p(
            "SELECT * FROM open_positions WHERE execution_mode=? AND quantity != 0"
        )
        with self._cursor() as cur:
            cur.execute(sql, (execution_mode,))
            return self._rows(cur, cur.fetchall())

    # ------------------------------------------------------------------
    # Active exit plans (restart recovery)
    # ------------------------------------------------------------------

    def save_exit_plan(self, plan: dict, execution_mode: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if self._mode == "postgres":
            cur_exec = self._cursor()
            sql = """INSERT INTO active_exit_plans
                     (plan_id, symbol, execution_mode, side, entry_price, quantity,
                      strategy_name, stop_loss_price, target_price, trailing_pct,
                      expiry_date, partial_exit_enabled, created_at)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT (plan_id) DO UPDATE SET
                       quantity=EXCLUDED.quantity,
                       stop_loss_price=EXCLUDED.stop_loss_price,
                       target_price=EXCLUDED.target_price,
                       trailing_pct=EXCLUDED.trailing_pct"""
        else:
            cur_exec = self._cursor()
            sql = """INSERT INTO active_exit_plans
                     (plan_id, symbol, execution_mode, side, entry_price, quantity,
                      strategy_name, stop_loss_price, target_price, trailing_pct,
                      expiry_date, partial_exit_enabled, created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(plan_id) DO UPDATE SET
                       quantity=excluded.quantity,
                       stop_loss_price=excluded.stop_loss_price,
                       target_price=excluded.target_price,
                       trailing_pct=excluded.trailing_pct"""
        expiry = plan.get("expiry_date")
        partial = plan.get("partial_exit_enabled", False)
        params = (
            plan["plan_id"], plan["symbol"], execution_mode, plan["side"],
            plan["entry_price"], plan["quantity"], plan["strategy_name"],
            plan.get("stop_loss_price"), plan.get("target_price"),
            plan.get("trailing_pct"), expiry,
            partial if self._mode == "postgres" else int(partial),
            now,
        )
        with cur_exec as cur:
            cur.execute(sql, params)

    def delete_exit_plan(self, plan_id: str) -> None:
        sql = self._p("DELETE FROM active_exit_plans WHERE plan_id=?")
        with self._cursor() as cur:
            cur.execute(sql, (plan_id,))

    def delete_exit_plans_for_symbol(self, symbol: str, execution_mode: str) -> None:
        sql = self._p(
            "DELETE FROM active_exit_plans WHERE symbol=? AND execution_mode=?"
        )
        with self._cursor() as cur:
            cur.execute(sql, (symbol, execution_mode))

    def load_exit_plans(self, execution_mode: str) -> list[dict]:
        sql = self._p(
            "SELECT * FROM active_exit_plans WHERE execution_mode=?"
        )
        with self._cursor() as cur:
            cur.execute(sql, (execution_mode,))
            return self._rows(cur, cur.fetchall())

    # ------------------------------------------------------------------
    # MarketRAG pattern persistence (profit-critical learning memory)
    # ------------------------------------------------------------------

    def save_rag_pattern(
        self,
        pattern_id: str,
        regime: str,
        news_sentiment_bucket: str,
        volatility_bucket: str,
        momentum_bucket: str,
        rsi_bucket: str,
        feature_vector: list,
        win_rate: float,
        avg_return: float,
        sample_size: int,
        last_updated: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if self._mode == "postgres":
            vec_val = _vec_to_pg(feature_vector) if feature_vector else None
            with self._cursor() as cur:
                cur.execute(
                    """INSERT INTO market_patterns
                       (pattern_id, regime, news_sentiment_bucket, volatility_bucket,
                        momentum_bucket, rsi_bucket, feature_vector, win_rate,
                        avg_return, sample_size, last_updated)
                       VALUES (%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s)
                       ON CONFLICT (pattern_id) DO UPDATE SET
                         win_rate=EXCLUDED.win_rate,
                         avg_return=EXCLUDED.avg_return,
                         sample_size=EXCLUDED.sample_size,
                         last_updated=EXCLUDED.last_updated,
                         feature_vector=EXCLUDED.feature_vector""",
                    (
                        pattern_id, regime, news_sentiment_bucket, volatility_bucket,
                        momentum_bucket, rsi_bucket, vec_val,
                        win_rate, avg_return, sample_size, now,
                    ),
                )
        else:
            with self._cursor() as cur:
                cur.execute(
                    """INSERT INTO market_patterns
                       (pattern_id, regime, news_sentiment_bucket, volatility_bucket,
                        momentum_bucket, rsi_bucket, feature_vector, win_rate,
                        avg_return, sample_size, last_updated)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(pattern_id) DO UPDATE SET
                         win_rate=excluded.win_rate,
                         avg_return=excluded.avg_return,
                         sample_size=excluded.sample_size,
                         last_updated=excluded.last_updated""",
                    (
                        pattern_id, regime, news_sentiment_bucket, volatility_bucket,
                        momentum_bucket, rsi_bucket, json.dumps(feature_vector),
                        win_rate, avg_return, sample_size, last_updated,
                    ),
                )

    def load_rag_patterns(self) -> list[dict]:
        sql = "SELECT * FROM market_patterns ORDER BY sample_size DESC"
        with self._cursor() as cur:
            cur.execute(sql)
            rows = []
            for row in cur.fetchall():
                r = _row_to_dict(cur, row)
                fv = r.get("feature_vector")
                if fv is None:
                    r["feature_vector"] = []
                elif isinstance(fv, str):
                    # SQLite stores as JSON; pgvector may return '[...]' string
                    try:
                        r["feature_vector"] = json.loads(fv)
                    except Exception:
                        r["feature_vector"] = _vec_from_pg(fv)
                else:
                    r["feature_vector"] = _vec_from_pg(fv)
                rows.append(r)
            return rows

    # ------------------------------------------------------------------
    # pgvector similarity search — trade history
    # ------------------------------------------------------------------

    def search_similar_trades(
        self,
        query_vec: list[float],
        limit: int = 20,
        execution_mode: str | None = None,
    ) -> list[dict]:
        """Return past trades whose market feature_vector is closest to query_vec.

        PostgreSQL: uses pgvector cosine distance (<=> operator) with IVFFlat.
        SQLite: returns empty list (no vector index available).
        """
        if self._mode != "postgres" or not query_vec:
            return []
        vec_str = _vec_to_pg(query_vec)
        clauses = ["feature_vector IS NOT NULL"]
        params: list = [vec_str, vec_str]
        if execution_mode:
            clauses.append("execution_mode = %s")
            params.append(execution_mode)
        params.append(limit)
        where = "WHERE " + " AND ".join(clauses)
        sql = (
            f"SELECT *, 1 - (feature_vector <=> %s::vector) AS similarity "
            f"FROM trades {where} "
            f"ORDER BY feature_vector <=> %s::vector ASC "
            f"LIMIT %s"
        )
        with self._cursor() as cur:
            cur.execute(sql, params)
            return self._rows(cur, cur.fetchall())

    # ------------------------------------------------------------------
    # pgvector similarity search — market patterns
    # ------------------------------------------------------------------

    def search_similar_patterns(
        self,
        query_vec: list[float],
        limit: int = 8,
        min_sample_size: int = 3,
    ) -> list[dict]:
        """Return market patterns nearest to query_vec using pgvector cosine distance.

        Returns rows ordered by similarity DESC with an extra 'similarity' column.
        Falls back to a full table scan for SQLite (Python-side ranking in MarketRAG).
        """
        if self._mode == "postgres":
            if not query_vec:
                return []
            vec_str = _vec_to_pg(query_vec)
            sql = (
                "SELECT *, 1 - (feature_vector <=> %s::vector) AS similarity "
                "FROM market_patterns "
                "WHERE feature_vector IS NOT NULL AND sample_size >= %s "
                "ORDER BY feature_vector <=> %s::vector ASC "
                "LIMIT %s"
            )
            with self._cursor() as cur:
                cur.execute(sql, (vec_str, min_sample_size, vec_str, limit))
                rows = []
                for row in cur.fetchall():
                    r = _row_to_dict(cur, row)
                    fv = r.get("feature_vector")
                    r["feature_vector"] = _vec_from_pg(fv) if fv else []
                    rows.append(r)
                return rows
        else:
            sql = "SELECT * FROM market_patterns WHERE sample_size >= ?"
            with self._cursor() as cur:
                cur.execute(sql, (min_sample_size,))
                rows = []
                for row in cur.fetchall():
                    r = _row_to_dict(cur, row)
                    fv = r.get("feature_vector")
                    if fv is None:
                        r["feature_vector"] = []
                    elif isinstance(fv, str):
                        try:
                            r["feature_vector"] = json.loads(fv)
                        except Exception:
                            r["feature_vector"] = []
                    rows.append(r)
                return rows

    # ------------------------------------------------------------------
    # ProfitGuard outcome persistence
    # ------------------------------------------------------------------

    def save_outcome(self, underlying: str, won: bool, pnl_pct: float, ts: str) -> None:
        now = ts or datetime.now(timezone.utc).isoformat()
        if self._mode == "postgres":
            with self._cursor() as cur:
                cur.execute(
                    "INSERT INTO profit_guard_outcomes (underlying, won, pnl_pct, ts) "
                    "VALUES (%s,%s,%s,%s)",
                    (underlying, won, pnl_pct, now),
                )
        else:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT INTO profit_guard_outcomes (underlying, won, pnl_pct, ts) "
                    "VALUES (?,?,?,?)",
                    (underlying, int(won), pnl_pct, now),
                )

    def load_recent_outcomes(self, underlying: str, limit: int = 20) -> list[dict]:
        sql = self._p(
            "SELECT underlying, won, pnl_pct, ts FROM profit_guard_outcomes "
            "WHERE underlying = ? ORDER BY id DESC LIMIT ?"
        )
        with self._cursor() as cur:
            cur.execute(sql, (underlying, limit))
            rows = self._rows(cur, cur.fetchall())
        # Return in chronological order (oldest first) so callers can replay into deques
        return list(reversed(rows))

    def load_all_outcome_underlyings(self) -> list[str]:
        sql = "SELECT DISTINCT underlying FROM profit_guard_outcomes"
        with self._cursor() as cur:
            cur.execute(sql)
            return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # ReflectionEngine persistence
    # ------------------------------------------------------------------

    def save_reflection(
        self,
        trace_id: str,
        underlying: str,
        won: bool,
        pnl_pct: float,
        quality: float,
        regime: str,
        payload: dict,
        ts: str,
    ) -> None:
        now = ts or datetime.now(timezone.utc).isoformat()
        if self._mode == "postgres":
            with self._cursor() as cur:
                cur.execute(
                    "INSERT INTO reflections "
                    "(trace_id, underlying, won, pnl_pct, quality, regime, payload, ts) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (trace_id, underlying, won, pnl_pct, quality, regime,
                     json.dumps(payload), now),
                )
        else:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT INTO reflections "
                    "(trace_id, underlying, won, pnl_pct, quality, regime, payload, ts) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (trace_id, underlying, int(won), pnl_pct, quality, regime,
                     json.dumps(payload), now),
                )

    def save_agent_weights(self, weights: dict[str, float]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if self._mode == "postgres":
            with self._cursor() as cur:
                for name, w in weights.items():
                    cur.execute(
                        "INSERT INTO agent_weights (agent_name, weight, updated_at) "
                        "VALUES (%s,%s,%s) "
                        "ON CONFLICT (agent_name) DO UPDATE SET "
                        "weight=EXCLUDED.weight, updated_at=EXCLUDED.updated_at",
                        (name, w, now),
                    )
        else:
            with self._cursor() as cur:
                for name, w in weights.items():
                    cur.execute(
                        "INSERT INTO agent_weights (agent_name, weight, updated_at) "
                        "VALUES (?,?,?) "
                        "ON CONFLICT(agent_name) DO UPDATE SET "
                        "weight=excluded.weight, updated_at=excluded.updated_at",
                        (name, w, now),
                    )

    def load_agent_weights(self) -> dict[str, float]:
        sql = "SELECT agent_name, weight FROM agent_weights"
        with self._cursor() as cur:
            cur.execute(sql)
            return {row[0]: float(row[1]) for row in cur.fetchall()}

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._mode == "postgres":
            try:
                self._pool.closeall()
            except Exception:
                pass
        else:
            conn = getattr(self._local, "conn", None)
            if conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.close()
                self._local.conn = None

    def checkpoint(self) -> None:
        if self._mode == "sqlite":
            conn = self._sqlite_conn()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
