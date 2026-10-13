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
from datetime import date, datetime, timedelta, timezone
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
    feature_vector vector(7),
    exit_reason    TEXT,
    lot_size       INTEGER     NOT NULL DEFAULT 1
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

-- audit_results/2026-09-13/AUDIT_REPORT.md finding #7 (high): the daily-loss
-- circuit breaker's session-start baseline was recomputed from cost-basis
-- equity (cash + entry cost of open positions — ignores any existing
-- unrealized P&L) on EVERY process start, including a mid-session restart.
-- One persisted row per IST trading date lets a restart RESTORE the
-- baseline this morning's premarket routine already captured (with real
-- marks, not cost basis) instead of quietly recomputing a wrong one.
CREATE TABLE IF NOT EXISTS session_baselines (
    trade_date     DATE        PRIMARY KEY,
    equity         DOUBLE PRECISION NOT NULL,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- GoalGovernance's annual-tracking anchor (goal/governance.py). Singleton
-- row (id always 1) so a restart RESTORES the same anchor instead of
-- silently re-anchoring to a new "today" every time the container
-- restarts -- see reset_baseline()'s own comment (2026-09-19) for why an
-- unpersisted anchor would defeat the whole point of tracking sustained
-- performance over time.
CREATE TABLE IF NOT EXISTS goal_governance_baseline (
    id             INTEGER     PRIMARY KEY,
    start_date     DATE        NOT NULL,
    start_capital  DOUBLE PRECISION NOT NULL,
    reset_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Daily short-vol premium monitor (Section 9-17 research follow-up,
-- 2026-09-22): one row per (trading day, index) recording that index's OWN
-- implied vol, realized vol, and the resulting premium -- see
-- trading_platform/research/index_premium.py. REPORTING ONLY: nothing reads
-- this table to gate a real order -- it exists so a human (or a future
-- research pass) can see which index's vol-selling premium is currently
-- rich without re-running a backtest by hand.
CREATE TABLE IF NOT EXISTS index_vol_premium (
    trade_date          DATE        NOT NULL,
    index_symbol        TEXT        NOT NULL,
    spot                DOUBLE PRECISION NOT NULL,
    atm_iv30            DOUBLE PRECISION NOT NULL,
    realized_vol20      DOUBLE PRECISION,
    premium             DOUBLE PRECISION,
    trailing_median_60d DOUBLE PRECISION,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, index_symbol)
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

-- Implementation prompt Section 21B: a durable, lease-protected run record
-- per (job, IST trading session date) so a scheduler restart cannot
-- double-count a training run. daily_scheduler.py's own in-memory
-- last_run_date dict already prevented re-selection within one process's
-- lifetime, but resets to empty on every restart — a restart mid-day could
-- re-trigger model_retrain/intraday_retrain a second time that day. The
-- UNIQUE constraint below is the actual idempotency mechanism: claiming a
-- run is one INSERT that either succeeds (this process owns today's run)
-- or hits the constraint (a row already exists — inspect its state/lease
-- to decide whether to refuse or take over a crashed process's stale lease).
CREATE TABLE IF NOT EXISTS learning_runs (
    id               BIGSERIAL   PRIMARY KEY,
    job_name         TEXT        NOT NULL,
    session_date     DATE        NOT NULL,
    dataset_version  TEXT,
    job_version      TEXT,
    state            TEXT        NOT NULL,
    lease_owner      TEXT,
    lease_expires_at TIMESTAMPTZ,
    detail           TEXT,
    metadata         JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_name, session_date)
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

CREATE TABLE IF NOT EXISTS runtime_monitor_log (
    id           BIGSERIAL   PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    severity     TEXT        NOT NULL,
    summary_text TEXT,
    anomalies    JSONB,
    metrics      JSONB
);

CREATE TABLE IF NOT EXISTS daily_ai_reviews (
    id           BIGSERIAL   PRIMARY KEY,
    review_date  DATE        NOT NULL UNIQUE,
    summary_text TEXT,
    model_id     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tuning_suggestions (
    id             BIGSERIAL   PRIMARY KEY,
    review_date    DATE        NOT NULL,
    parameter      TEXT        NOT NULL,
    current_value  TEXT,
    proposed_value TEXT,
    rationale      TEXT,
    confidence     DOUBLE PRECISION,
    model_id       TEXT,
    status         TEXT        NOT NULL DEFAULT 'pending',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS strategy_hypotheses (
    id                  BIGSERIAL   PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    title               TEXT        NOT NULL,
    thesis_text         TEXT,
    suggested_universe  TEXT,
    suggested_features  TEXT,
    model_id            TEXT,
    status              TEXT        NOT NULL DEFAULT 'proposed',
    validation_run_id   TEXT,
    validation_verdict  TEXT
);

CREATE TABLE IF NOT EXISTS backtest_gate_results (
    id              BIGSERIAL   PRIMARY KEY,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    backtest_id     TEXT        NOT NULL,
    strategy_id     TEXT        NOT NULL,
    subject_type    TEXT        NOT NULL DEFAULT 'strategy',
    gate_name       TEXT        NOT NULL,
    result          TEXT        NOT NULL,
    metric_value    DOUBLE PRECISION,
    threshold_value DOUBLE PRECISION,
    message         TEXT,
    details         TEXT,
    git_sha         TEXT,
    config_snapshot TEXT
);

CREATE TABLE IF NOT EXISTS strategy_promotions (
    strategy_id     TEXT        PRIMARY KEY,
    status          TEXT        NOT NULL DEFAULT 'research',
    version         INTEGER     NOT NULL DEFAULT 1,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata        TEXT
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
CREATE INDEX IF NOT EXISTS idx_monitor_log_ts      ON runtime_monitor_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_tuning_status       ON tuning_suggestions (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hypotheses_status   ON strategy_hypotheses (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gate_results_backtest ON backtest_gate_results (backtest_id);
CREATE INDEX IF NOT EXISTS idx_gate_results_strategy ON backtest_gate_results (strategy_id, run_at DESC);
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
    feature_vector TEXT,
    exit_reason   TEXT,
    lot_size      INTEGER NOT NULL DEFAULT 1
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
-- See the matching postgres table's comment above (finding #7).
CREATE TABLE IF NOT EXISTS session_baselines (
    trade_date    TEXT PRIMARY KEY,
    equity        REAL NOT NULL,
    recorded_at   TEXT NOT NULL
);
-- See the matching postgres table's comment above.
CREATE TABLE IF NOT EXISTS goal_governance_baseline (
    id            INTEGER PRIMARY KEY,
    start_date    TEXT NOT NULL,
    start_capital REAL NOT NULL,
    reset_at      TEXT NOT NULL
);
-- See the matching postgres table's comment above.
CREATE TABLE IF NOT EXISTS index_vol_premium (
    trade_date          TEXT NOT NULL,
    index_symbol        TEXT NOT NULL,
    spot                REAL NOT NULL,
    atm_iv30            REAL NOT NULL,
    realized_vol20      REAL,
    premium             REAL,
    trailing_median_60d REAL,
    recorded_at         TEXT NOT NULL,
    PRIMARY KEY (trade_date, index_symbol)
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
-- See the matching postgres table's comment above (Section 21B).
CREATE TABLE IF NOT EXISTS learning_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name         TEXT NOT NULL,
    session_date     TEXT NOT NULL,
    dataset_version  TEXT,
    job_version      TEXT,
    state            TEXT NOT NULL,
    lease_owner      TEXT,
    lease_expires_at TEXT,
    detail           TEXT,
    metadata         TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (job_name, session_date)
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
CREATE TABLE IF NOT EXISTS runtime_monitor_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    severity     TEXT NOT NULL,
    summary_text TEXT,
    anomalies    TEXT,
    metrics      TEXT
);
CREATE TABLE IF NOT EXISTS daily_ai_reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date  TEXT NOT NULL UNIQUE,
    summary_text TEXT,
    model_id     TEXT,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tuning_suggestions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date    TEXT NOT NULL,
    parameter      TEXT NOT NULL,
    current_value  TEXT,
    proposed_value TEXT,
    rationale      TEXT,
    confidence     REAL,
    model_id       TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL,
    reviewed_at    TEXT
);
CREATE TABLE IF NOT EXISTS strategy_hypotheses (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at         TEXT NOT NULL,
    title              TEXT NOT NULL,
    thesis_text        TEXT,
    suggested_universe TEXT,
    suggested_features TEXT,
    model_id           TEXT,
    status             TEXT NOT NULL DEFAULT 'proposed',
    validation_run_id  TEXT,
    validation_verdict TEXT
);
CREATE TABLE IF NOT EXISTS backtest_gate_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT NOT NULL,
    backtest_id     TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,
    subject_type    TEXT NOT NULL DEFAULT 'strategy',
    gate_name       TEXT NOT NULL,
    result          TEXT NOT NULL,
    metric_value    REAL,
    threshold_value REAL,
    message         TEXT,
    details         TEXT,
    git_sha         TEXT,
    config_snapshot TEXT
);
CREATE TABLE IF NOT EXISTS strategy_promotions (
    strategy_id     TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'research',
    version         INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL,
    metadata        TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ts        ON trades (timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_mode      ON trades (execution_mode);
CREATE INDEX IF NOT EXISTS idx_trades_is_test   ON trades (is_test);
CREATE INDEX IF NOT EXISTS idx_gate_results_backtest ON backtest_gate_results (backtest_id);
CREATE INDEX IF NOT EXISTS idx_gate_results_strategy ON backtest_gate_results (strategy_id, run_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_mode   ON portfolio_snapshots (execution_mode);
CREATE INDEX IF NOT EXISTS idx_outcomes_sym     ON profit_guard_outcomes (underlying, ts DESC);
CREATE INDEX IF NOT EXISTS idx_reflections_sym  ON reflections (underlying, ts DESC);
CREATE INDEX IF NOT EXISTS idx_monitor_log_ts   ON runtime_monitor_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_tuning_status    ON tuning_suggestions (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hypotheses_status ON strategy_hypotheses (status, created_at DESC);
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
        import os
        import psycopg2
        import psycopg2.pool
        # maxconn=10 was fine at AGENT_SCAN_CONCURRENCY=2 (the AWS-instance-sized
        # default) but not at higher concurrency on larger hosts: 64 concurrent
        # scan pipelines each doing brief portfolio/feature-store/trace reads
        # and writes exhausted a 10-connection pool 276 times in one trading day
        # (confirmed via "psycopg2.pool.PoolError: connection pool exhausted" in
        # the logs, 2026-07-28), silently swallowing whatever DB operation each
        # one needed — including, possibly, order-path writes. Postgres itself
        # allows 100 connections total; 40 leaves real headroom for everything
        # else (API requests, scheduler, admin) while comfortably covering
        # concurrent pipelines' short-lived connection use.
        maxconn = int(os.getenv("POSTGRES_POOL_MAXCONN", "40"))
        minconn = int(os.getenv("POSTGRES_POOL_MINCONN", "4"))
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=minconn, maxconn=maxconn, dsn=self._url
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
                        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit_reason TEXT",
                        # audit_results/2026-09-13/AUDIT_REPORT.md finding #4.
                        # KNOWN LIMITATION: this backfills every pre-existing
                        # row to 1, which is correct for equities but WRONG
                        # for any historical derivative trade — there is no
                        # way to recover the true historical lot_size from
                        # this column alone. Do not treat lot_size on a row
                        # older than this migration as trustworthy; cross-
                        # reference symbol/expiry against the instrument
                        # master before using it for reconciliation. Every
                        # trade recorded AFTER this migration carries its
                        # real value (PortfolioLedger.apply_fill always
                        # passes intent.instrument.lot_size).
                        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS lot_size INTEGER DEFAULT 1",
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
    def backend(self) -> str:
        """Which store this instance resolved to: 'postgres' or 'sqlite'.

        Public so the runtime can refuse to serve P&L from the stale local
        SQLite file when a Postgres store is required (2026-10-13 audit #5)."""
        return self._mode

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
            # WAL mode needs a shared-memory-mapped -shm sidecar file, which
            # doesn't reliably survive this environment's mix of Docker
            # Desktop Windows bind-mount access and rapid native local test
            # runs against the same default data/trading.db path — confirmed
            # 2026-07-29: this file (and oms_events.db / paper_learning_journal.db,
            # same root cause, see their _conn()) were found genuinely
            # corrupted (PRAGMA integrity_check failures, not just open
            # errors) after today's session. Salvaged what was readable and
            # switched to DELETE, which needs no shared memory mapping.
            conn.execute("PRAGMA journal_mode=DELETE")
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
            self._sl_ensure_column(cur, "trades", "exit_reason", "TEXT")
            # See the matching postgres migration's comment above (finding
            # #4) — same backfill-to-1 caveat for pre-migration rows.
            self._sl_ensure_column(cur, "trades", "lot_size", "INTEGER NOT NULL DEFAULT 1")

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
        exit_reason: str | None = None,
    ) -> None:
        is_test = trade.strategy_name in {"manual_preview", "trace_fill_test"}
        ts = trade.timestamp.isoformat() if hasattr(trade.timestamp, "isoformat") else str(trade.timestamp)
        lot_size = getattr(trade, "lot_size", 1)
        if self._mode == "postgres":
            vec_val = _vec_to_pg(feature_vector) if feature_vector else None
            with self._cursor() as cur:
                cur.execute(
                    """INSERT INTO trades
                       (trade_id, order_id, symbol, side, quantity, price, charges,
                        ts, strategy_name, execution_mode, is_test, feature_vector, exit_reason, lot_size)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s)
                       ON CONFLICT (trade_id) DO NOTHING""",
                    (
                        trade.trade_id, trade.order_id, trade.symbol,
                        trade.side.value, trade.quantity, trade.price,
                        trade.charges, ts, trade.strategy_name,
                        execution_mode, is_test, vec_val, exit_reason, lot_size,
                    ),
                )
        else:
            vec_json = json.dumps(feature_vector) if feature_vector else None
            with self._cursor() as cur:
                cur.execute(
                    """INSERT OR IGNORE INTO trades
                       (trade_id, order_id, symbol, side, quantity, price, charges,
                        timestamp, strategy_name, execution_mode, is_test, feature_vector, exit_reason, lot_size)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        trade.trade_id, trade.order_id, trade.symbol,
                        trade.side.value, trade.quantity, trade.price,
                        trade.charges, ts, trade.strategy_name,
                        execution_mode, int(is_test), vec_json, exit_reason, lot_size,
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

    def distinct_paper_trading_days(self, strategy_name: str) -> int:
        """Count of distinct calendar dates this strategy has a real PAPER
        fill on — immutable evidence a promotion gate can trust, unlike a
        metadata counter nothing was ever wiring up.

        audit_results/2026-09-13/AUDIT_REPORT.md finding #11 (medium): the
        short_vol promotion gate reported 0 paper days despite months of
        real August-September paper fills, because StrategyPromotionService
        was constructed with no paper_days_lookup at all — it silently fell
        back to record.metadata.get("paper_days", 0), which nothing in this
        codebase ever writes. This is what that lookup should have been:
        derived directly from the trades table (excludes is_test rows the
        same way trades() does), not a manually-maintained counter."""
        test_clause = "is_test = FALSE" if self._mode == "postgres" else "COALESCE(is_test, 0) = 0"
        sql = self._p(
            f"SELECT COUNT(DISTINCT DATE({self._ts})) AS days FROM trades "
            f"WHERE strategy_name = ? AND execution_mode = 'PAPER' AND {test_clause}"
        )
        with self._cursor() as cur:
            cur.execute(sql, (strategy_name,))
            row = cur.fetchone()
            result = _row_to_dict(cur, row) if row else None
        return int(result["days"]) if result and result["days"] is not None else 0

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
        """Each row carries its own execution_mode even when the caller
        doesn't filter by one — found in the 2026-09-16 Section 19 audit:
        without it, a PAPER and a future LIVE session's snapshots would
        render as one unbroken, unlabeled equity line with no way for a
        caller to tell them apart (dashboard truthfulness: never let a
        forward-PAPER result be mistaken for a genuine LIVE one)."""
        if execution_mode:
            sql = self._p(
                "SELECT recorded_at, equity, drawdown, execution_mode FROM portfolio_snapshots "
                "WHERE execution_mode=? ORDER BY id DESC LIMIT ?"
            )
            params = (execution_mode, limit)
        else:
            sql = self._p(
                "SELECT recorded_at, equity, drawdown, execution_mode FROM portfolio_snapshots "
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

    def save_session_baseline(self, trade_date: date, equity: float) -> None:
        """Persist the daily-loss circuit breaker's session-start equity for
        one IST trading date. See session_baselines' own comment (audit
        finding #7) — this is what lets a mid-day restart restore the value
        this morning's premarket routine already captured instead of
        recomputing a wrong (cost-basis) one."""
        now = datetime.now(timezone.utc).isoformat()
        if self._mode == "postgres":
            sql = """INSERT INTO session_baselines (trade_date, equity, recorded_at)
                     VALUES (%s,%s,%s)
                     ON CONFLICT (trade_date) DO UPDATE SET
                       equity=EXCLUDED.equity, recorded_at=EXCLUDED.recorded_at"""
            params: Any = (trade_date, equity, now)
        else:
            sql = """INSERT INTO session_baselines (trade_date, equity, recorded_at)
                     VALUES (?,?,?)
                     ON CONFLICT(trade_date) DO UPDATE SET
                       equity=excluded.equity, recorded_at=excluded.recorded_at"""
            params = (trade_date.isoformat(), equity, now)
        with self._cursor() as cur:
            cur.execute(sql, params)

    def get_session_baseline(self, trade_date: date) -> float | None:
        sql = self._p("SELECT equity FROM session_baselines WHERE trade_date = ?")
        params = (trade_date if self._mode == "postgres" else trade_date.isoformat(),)
        with self._cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            result = _row_to_dict(cur, row) if row else None
        return float(result["equity"]) if result else None

    def save_goal_governance_baseline(self, start_date: date, start_capital: float) -> None:
        """Persist GoalGovernance's annual-tracking anchor (singleton row,
        id=1). See goal_governance_baseline's own comment — a restart must
        RESTORE this, not silently re-anchor to a new "today" every time."""
        now = datetime.now(timezone.utc).isoformat()
        if self._mode == "postgres":
            sql = """INSERT INTO goal_governance_baseline (id, start_date, start_capital, reset_at)
                     VALUES (1,%s,%s,%s)
                     ON CONFLICT (id) DO UPDATE SET
                       start_date=EXCLUDED.start_date, start_capital=EXCLUDED.start_capital,
                       reset_at=EXCLUDED.reset_at"""
            params: Any = (start_date, start_capital, now)
        else:
            sql = """INSERT INTO goal_governance_baseline (id, start_date, start_capital, reset_at)
                     VALUES (1,?,?,?)
                     ON CONFLICT(id) DO UPDATE SET
                       start_date=excluded.start_date, start_capital=excluded.start_capital,
                       reset_at=excluded.reset_at"""
            params = (start_date.isoformat(), start_capital, now)
        with self._cursor() as cur:
            cur.execute(sql, params)

    def get_goal_governance_baseline(self) -> dict | None:
        sql = "SELECT start_date, start_capital FROM goal_governance_baseline WHERE id = 1"
        with self._cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            result = _row_to_dict(cur, row) if row else None
        if not result:
            return None
        start_date = result["start_date"]
        if isinstance(start_date, str):
            start_date = date.fromisoformat(start_date[:10])
        return {"start_date": start_date, "start_capital": float(result["start_capital"])}

    # ------------------------------------------------------------------
    # Index vol-premium monitor (research/index_premium.py) -- reporting only
    # ------------------------------------------------------------------

    def save_index_premium(
        self, trade_date: date, index_symbol: str, spot: float, atm_iv30: float,
        realized_vol20: float | None, premium: float | None, trailing_median_60d: float | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if self._mode == "postgres":
            sql = """INSERT INTO index_vol_premium
                     (trade_date, index_symbol, spot, atm_iv30, realized_vol20,
                      premium, trailing_median_60d, recorded_at)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT (trade_date, index_symbol) DO UPDATE SET
                       spot=EXCLUDED.spot, atm_iv30=EXCLUDED.atm_iv30,
                       realized_vol20=EXCLUDED.realized_vol20, premium=EXCLUDED.premium,
                       trailing_median_60d=EXCLUDED.trailing_median_60d,
                       recorded_at=EXCLUDED.recorded_at"""
            params: Any = (trade_date, index_symbol, spot, atm_iv30, realized_vol20,
                          premium, trailing_median_60d, now)
        else:
            sql = """INSERT INTO index_vol_premium
                     (trade_date, index_symbol, spot, atm_iv30, realized_vol20,
                      premium, trailing_median_60d, recorded_at)
                     VALUES (?,?,?,?,?,?,?,?)
                     ON CONFLICT(trade_date, index_symbol) DO UPDATE SET
                       spot=excluded.spot, atm_iv30=excluded.atm_iv30,
                       realized_vol20=excluded.realized_vol20, premium=excluded.premium,
                       trailing_median_60d=excluded.trailing_median_60d,
                       recorded_at=excluded.recorded_at"""
            params = (trade_date.isoformat(), index_symbol, spot, atm_iv30, realized_vol20,
                     premium, trailing_median_60d, now)
        with self._cursor() as cur:
            cur.execute(sql, params)

    def recent_index_premium(self, index_symbol: str, limit: int = 90) -> list[dict]:
        """Most recent `limit` rows for one index, oldest first -- the shape
        a realized-vol / trailing-median computation needs."""
        sql = self._p(
            "SELECT * FROM index_vol_premium WHERE index_symbol = ? "
            "ORDER BY trade_date DESC LIMIT ?"
        )
        with self._cursor() as cur:
            cur.execute(sql, (index_symbol, limit))
            rows = list(reversed(self._rows(cur, cur.fetchall())))
        for r in rows:
            if isinstance(r.get("trade_date"), str):
                r["trade_date"] = date.fromisoformat(r["trade_date"][:10])
        return rows

    def latest_index_premium(self) -> list[dict]:
        """The single most recent row per index -- "today's reading"."""
        sql = self._p("SELECT * FROM index_vol_premium ORDER BY trade_date DESC LIMIT ?")
        with self._cursor() as cur:
            # A handful of days' worth across a handful of indices is enough
            # to find each index's single most-recent row below.
            cur.execute(sql, (32,))
            rows = self._rows(cur, cur.fetchall())
        latest: dict[str, dict] = {}
        for r in rows:
            sym = r["index_symbol"]
            if sym not in latest:
                if isinstance(r.get("trade_date"), str):
                    r["trade_date"] = date.fromisoformat(r["trade_date"][:10])
                latest[sym] = r
        return list(latest.values())

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

    def recent_model_runs(
        self,
        model_name: str | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Read back what scripts/daily_scheduler.py's model_retrain /
        intraday_retrain jobs recorded via save_model_run(): every nightly
        verdict (accepted/rejected, per horizon), win or lose. Written since
        that job's introduction but never read back anywhere until the
        Section 21A/J audit (2026-09-14) — this table was write-only."""
        clauses, params = [], []
        if model_name:
            clauses.append("model_name = ?")
            params.append(model_name)
        if since:
            clauses.append("run_at >= ?")
            params.append(since if self._mode == "postgres" else since.isoformat())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = self._p(f"SELECT * FROM model_runs {where} ORDER BY id DESC LIMIT ?")
        with self._cursor() as cur:
            cur.execute(sql, [*params, limit])
            rows = self._rows(cur, cur.fetchall())
        for row in rows:
            row["metadata"] = self._json_field(row.get("metadata"), default={})
        return rows

    # ------------------------------------------------------------------
    # Learning runs (Section 21B: durable, lease-protected training-job
    # state so a scheduler restart cannot double-count a run)
    # ------------------------------------------------------------------

    _LEARNING_RUN_IN_PROGRESS_STATE = "RUNNING"

    def claim_learning_run(
        self,
        job_name: str,
        session_date: date,
        *,
        dataset_version: str | None = None,
        job_version: str | None = None,
        lease_owner: str,
        lease_seconds: float = 1800.0,
    ) -> dict | None:
        """Atomically claim ownership of `job_name`'s run for `session_date`.

        Returns the claimed row (now leased to `lease_owner`) if this caller
        may proceed, or None if it must not: either the run already reached
        a terminal state today (the durable replacement for
        daily_scheduler.py's in-memory last_run_date dict — that dict resets
        on every process restart, so a restart mid-day could otherwise
        re-trigger the same training run a second time), or another process
        currently holds a still-valid lease on it.

        A row with an EXPIRED lease (the process that claimed it crashed
        without ever reaching a terminal state) is fair game — this is the
        recovery path for a crashed scheduler, not a duplicate-run risk,
        since the crashed run's own DB mutations (if any) are idempotent
        writes keyed by this same (job_name, session_date) row.
        """
        now = datetime.now(timezone.utc)
        now_val = now if self._mode == "postgres" else now.isoformat()
        sd_val = session_date if self._mode == "postgres" else session_date.isoformat()
        meta_val = json.dumps({})

        insert_sql = self._p(
            "INSERT INTO learning_runs "
            "(job_name, session_date, dataset_version, job_version, state, "
            " lease_owner, lease_expires_at, metadata, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (job_name, session_date) DO NOTHING"
        )
        lease_expires = now + timedelta(seconds=lease_seconds)
        lease_expires_val = lease_expires if self._mode == "postgres" else lease_expires.isoformat()
        with self._cursor() as cur:
            cur.execute(insert_sql, (
                job_name, sd_val, dataset_version, job_version,
                self._LEARNING_RUN_IN_PROGRESS_STATE,
                lease_owner, lease_expires_val, meta_val, now_val, now_val,
            ))

            select_sql = self._p("SELECT * FROM learning_runs WHERE job_name=? AND session_date=?")
            cur.execute(select_sql, (job_name, sd_val))
            row = _row_to_dict(cur, cur.fetchone())
            if row is None:
                return None  # should not happen; defensive

            existing_lease_expires = self._parse_dt(row.get("lease_expires_at"))
            lease_still_valid = bool(existing_lease_expires and existing_lease_expires > now)
            is_running = row.get("state") == self._LEARNING_RUN_IN_PROGRESS_STATE

            if not is_running:
                return None  # already reached a terminal state today
            if lease_still_valid and row.get("lease_owner") != lease_owner:
                return None  # another process is actively holding this run

            # Either freshly inserted by us, or a RUNNING row with an
            # expired/absent lease — (re)claim it.
            update_sql = self._p(
                "UPDATE learning_runs SET lease_owner=?, lease_expires_at=?, updated_at=? "
                "WHERE id=?"
            )
            cur.execute(update_sql, (lease_owner, lease_expires_val, now_val, row["id"]))
            row["lease_owner"] = lease_owner
            row["lease_expires_at"] = lease_expires_val
            row["metadata"] = self._json_field(row.get("metadata"), default={})
            return row

    def update_learning_run(
        self,
        run_id: int,
        state: str,
        *,
        detail: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Advance a claimed run's state. `state` is one of the
        implementation-prompt-named milestones (DATASET_READY, EVALUATED,
        TRAINED_OR_SKIPPED, CANDIDATE_VALIDATED, SHADOW_OR_PAPER_ELIGIBLE) or
        a failure state (MISSING_DATA, VALIDATION_REJECTED, RESOURCE_TIMEOUT,
        OPERATIONAL_FAILURE) — anything other than the internal RUNNING
        marker is treated as terminal-for-today by claim_learning_run."""
        now = datetime.now(timezone.utc)
        now_val = now if self._mode == "postgres" else now.isoformat()
        sql = self._p("UPDATE learning_runs SET state=?, detail=?, metadata=?, updated_at=? WHERE id=?")
        meta_val = json.dumps(metadata) if metadata is not None else None
        with self._cursor() as cur:
            cur.execute(sql, (state, detail, meta_val, now_val, run_id))

    def recent_learning_runs(self, job_name: str | None = None, limit: int = 50) -> list[dict]:
        clauses, params = [], []
        if job_name:
            clauses.append("job_name = ?")
            params.append(job_name)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = self._p(f"SELECT * FROM learning_runs {where} ORDER BY id DESC LIMIT ?")
        with self._cursor() as cur:
            cur.execute(sql, [*params, limit])
            rows = self._rows(cur, cur.fetchall())
        for row in rows:
            row["metadata"] = self._json_field(row.get("metadata"), default={})
        return rows

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

    def latest_kill_switch_state(self) -> dict | None:
        """The most recent kill_switch_activated/kill_switch_cleared event, or
        None if the kill switch has never fired. Found 2026-09-13 investigating
        the 2026-09-11 kill-switch trip: kill_switch_active lived only as an
        in-memory TradingRuntime attribute, defaulting to False on every
        __init__ — so ANY restart (a routine redeploy, a crash, Docker's own
        unless-stopped policy) silently discarded an active trip with no audit
        trail, unlike the three prior trips (2026-08-23, 2026-08-27, 2026-09-01)
        which all show an explicit kill_switch_cleared event. This reads the
        SAME risk_events audit trail the activation/clear calls already write,
        so a restart can restore the correct state instead of quietly
        defaulting to "safe to trade" whenever a trip was never actually,
        deliberately cleared."""
        sql = self._p(
            "SELECT * FROM risk_events WHERE event_type IN "
            "('kill_switch_activated', 'kill_switch_cleared') ORDER BY id DESC LIMIT 1"
        )
        with self._cursor() as cur:
            cur.execute(sql)
            rows = self._rows(cur, cur.fetchall())
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Backtest validation gates (REDESIGN §5)
    # ------------------------------------------------------------------

    def save_gate_result(
        self,
        *,
        backtest_id: str,
        strategy_id: str,
        gate_name: str,
        result: str,
        metric_value: float | None,
        threshold_value: float | None,
        message: str,
        details: dict | None = None,
        subject_type: str = "strategy",
        git_sha: str | None = None,
        config_snapshot: dict | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        sql = self._p(
            """INSERT INTO backtest_gate_results
               (run_at, backtest_id, strategy_id, subject_type, gate_name, result,
                metric_value, threshold_value, message, details, git_sha, config_snapshot)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"""
        )
        params = (
            now, backtest_id, strategy_id, subject_type, gate_name, result,
            metric_value, threshold_value, message,
            json.dumps(details or {}, default=str),
            git_sha,
            json.dumps(config_snapshot or {}, default=str),
        )
        with self._cursor() as cur:
            cur.execute(sql, params)

    def save_gate_results_batch(
        self,
        backtest_id: str,
        strategy_id: str,
        gate_results: Any,  # validation.gates.BacktestGateResults
        subject_type: str = "strategy",
        git_sha: str | None = None,
        config_snapshot: dict | None = None,
    ) -> None:
        """Persist every evaluated GateOutcome on `gate_results` — the only
        write path callers need."""
        outcomes = [
            gate_results.walk_forward, gate_results.cpcv, gate_results.dsr,
            gate_results.pbo, gate_results.monte_carlo, gate_results.cost_model,
            gate_results.paper_days, gate_results.promotion_ladder,
        ]
        for outcome in outcomes:
            if outcome is None:
                continue
            self.save_gate_result(
                backtest_id=backtest_id,
                strategy_id=strategy_id,
                gate_name=outcome.gate_name,
                result=outcome.result.value if hasattr(outcome.result, "value") else str(outcome.result),
                metric_value=float(outcome.metric),
                threshold_value=float(outcome.threshold),
                message=outcome.message,
                details=outcome.details,
                subject_type=subject_type,
                git_sha=git_sha,
                config_snapshot=config_snapshot,
            )

    def recent_gate_results(
        self,
        backtest_id: str | None = None,
        strategy_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses, params = [], []
        if backtest_id:
            clauses.append("backtest_id = ?")
            params.append(backtest_id)
        if strategy_id:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = self._p(f"SELECT * FROM backtest_gate_results{where} ORDER BY id DESC LIMIT ?")
        params.append(limit)
        with self._cursor() as cur:
            cur.execute(sql, tuple(params))
            return self._rows(cur, cur.fetchall())

    def latest_gate_summary(self, strategy_id: str) -> dict | None:
        """Gate rows from the most recent backtest run for `strategy_id`,
        aggregated to {"backtest_id", "run_at", "all_passed", "gates": {...}}.

        all_passed mirrors BacktestGateResults.all_passed: WARN and SKIP count
        as non-blocking, only an explicit FAIL blocks. Returns None when the
        strategy has no recorded gate run at all (caller treats that as "not
        gated yet", which must NOT be read as a pass).
        """
        rows = self.recent_gate_results(strategy_id=strategy_id, limit=200)
        if not rows:
            return None
        latest_backtest_id = rows[0].get("backtest_id")
        latest = [r for r in rows if r.get("backtest_id") == latest_backtest_id]
        gates = {
            r.get("gate_name"): {
                "result": r.get("result"),
                "metric": r.get("metric_value"),
                "threshold": r.get("threshold_value"),
                "message": r.get("message"),
            }
            for r in latest
        }
        all_passed = bool(gates) and all(g["result"] != "fail" for g in gates.values())
        run_at = latest[0].get("run_at")
        return {
            "backtest_id": latest_backtest_id,
            "run_at": run_at.isoformat() if hasattr(run_at, "isoformat") else run_at,
            "all_passed": all_passed,
            "gates": gates,
        }

    # ------------------------------------------------------------------
    # Strategy promotion ladder (REDESIGN §5 / §13 phase 3)
    # ------------------------------------------------------------------

    def get_strategy_promotion(self, strategy_id: str) -> dict | None:
        sql = self._p("SELECT * FROM strategy_promotions WHERE strategy_id = ?")
        with self._cursor() as cur:
            cur.execute(sql, (strategy_id,))
            rows = self._rows(cur, cur.fetchall())
        return rows[0] if rows else None

    def upsert_strategy_promotion(
        self,
        strategy_id: str,
        status: str,
        version: int = 1,
        metadata: dict | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(metadata or {}, default=str)
        sql = self._p(
            """INSERT INTO strategy_promotions (strategy_id, status, version, updated_at, metadata)
               VALUES (?,?,?,?,?)
               ON CONFLICT (strategy_id) DO UPDATE SET
                   status = EXCLUDED.status,
                   version = EXCLUDED.version,
                   updated_at = EXCLUDED.updated_at,
                   metadata = EXCLUDED.metadata"""
        )
        with self._cursor() as cur:
            cur.execute(sql, (strategy_id, status, version, now, payload))

    def list_strategy_promotions(self) -> list[dict]:
        sql = self._p("SELECT * FROM strategy_promotions ORDER BY strategy_id")
        with self._cursor() as cur:
            cur.execute(sql)
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
    # Runtime monitor digests (continuous local-model observability loop)
    # ------------------------------------------------------------------

    def save_monitor_digest(
        self,
        severity: str,
        summary_text: str | None,
        anomalies: list[str] | None,
        metrics: dict | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        sql = self._p(
            "INSERT INTO runtime_monitor_log (ts, severity, summary_text, anomalies, metrics) "
            "VALUES (?,?,?,?,?)"
        )
        with self._cursor() as cur:
            cur.execute(sql, (
                now, severity, summary_text,
                json.dumps(anomalies or []), json.dumps(metrics or {}),
            ))

    def recent_monitor_digests(self, limit: int = 20) -> list[dict]:
        sql = self._p("SELECT * FROM runtime_monitor_log ORDER BY id DESC LIMIT ?")
        with self._cursor() as cur:
            cur.execute(sql, (limit,))
            rows = self._rows(cur, cur.fetchall())
        for row in rows:
            row["anomalies"] = self._json_field(row.get("anomalies"), default=[])
            row["metrics"] = self._json_field(row.get("metrics"), default={})
        return rows

    @staticmethod
    def _json_field(value: Any, default: Any) -> Any:
        """Decode a JSON/JSONB column read back from either backend.

        SQLite has no native JSON type (these columns are declared TEXT), so
        the driver always returns the raw string. psycopg2 auto-adapts JSONB
        columns to Python objects on SELECT, so under PostgreSQL `value` is
        already a list/dict — calling json.loads() on it raises TypeError
        (confirmed 2026-07-28: every /monitor/runtime-digest call 500'd
        against the live Postgres-backed container, even though the identical
        SQLite-backed unit tests passed — the two backends were never
        exercised by the same test). Mirrors the existing decode pattern in
        trace/learning_journal.py's _decode().
        """
        if isinstance(value, str):
            return json.loads(value) if value else default
        return value if value is not None else default

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        """Decode a TIMESTAMPTZ column read back from either backend into a
        real, comparable datetime — same backend-divergence shape as
        _json_field (SQLite always returns the raw ISO string; psycopg2
        auto-adapts TIMESTAMPTZ to an already-aware datetime object)."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Daily AI review & tuning suggestions (post-market advisor)
    # ------------------------------------------------------------------

    def save_daily_ai_review(self, review_date: date, summary_text: str, model_id: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if self._mode == "postgres":
            sql = (
                "INSERT INTO daily_ai_reviews (review_date, summary_text, model_id, created_at) "
                "VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (review_date) DO UPDATE SET "
                "summary_text=EXCLUDED.summary_text, model_id=EXCLUDED.model_id, created_at=EXCLUDED.created_at"
            )
            params: Any = (review_date, summary_text, model_id, now)
        else:
            sql = (
                "INSERT INTO daily_ai_reviews (review_date, summary_text, model_id, created_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(review_date) DO UPDATE SET "
                "summary_text=excluded.summary_text, model_id=excluded.model_id, created_at=excluded.created_at"
            )
            params = (review_date.isoformat(), summary_text, model_id, now)
        with self._cursor() as cur:
            cur.execute(sql, params)

    def get_daily_ai_review(self, review_date: date) -> dict | None:
        sql = self._p("SELECT * FROM daily_ai_reviews WHERE review_date=?")
        params = (review_date,) if self._mode == "postgres" else (review_date.isoformat(),)
        with self._cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return _row_to_dict(cur, row) if row else None

    def save_tuning_suggestion(
        self,
        review_date: date,
        parameter: str,
        current_value: str,
        proposed_value: str,
        rationale: str,
        confidence: float | None,
        model_id: str | None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rd = review_date if self._mode == "postgres" else review_date.isoformat()
        sql = self._p(
            "INSERT INTO tuning_suggestions "
            "(review_date, parameter, current_value, proposed_value, rationale, confidence, "
            "model_id, status, created_at) VALUES (?,?,?,?,?,?,?,'pending',?)"
        )
        with self._cursor() as cur:
            cur.execute(sql, (rd, parameter, current_value, proposed_value, rationale, confidence, model_id, now))
            if self._mode == "postgres":
                cur.execute("SELECT lastval()")
                return int(cur.fetchone()[0])
            return int(cur.lastrowid)

    def tuning_suggestions(self, status: str | None = None, limit: int = 50) -> list[dict]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?"); params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = self._p(f"SELECT * FROM tuning_suggestions {where} ORDER BY id DESC LIMIT ?")
        with self._cursor() as cur:
            cur.execute(sql, [*params, limit])
            return self._rows(cur, cur.fetchall())

    def update_tuning_suggestion_status(self, suggestion_id: int, status: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        sql = self._p("UPDATE tuning_suggestions SET status=?, reviewed_at=? WHERE id=?")
        with self._cursor() as cur:
            cur.execute(sql, (status, now, suggestion_id))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Strategy hypotheses (research idea generation — never self-validated)
    # ------------------------------------------------------------------

    def save_strategy_hypothesis(
        self,
        title: str,
        thesis_text: str,
        suggested_universe: str | None,
        suggested_features: str | None,
        model_id: str | None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        sql = self._p(
            "INSERT INTO strategy_hypotheses "
            "(created_at, title, thesis_text, suggested_universe, suggested_features, "
            "model_id, status) VALUES (?,?,?,?,?,?,'proposed')"
        )
        with self._cursor() as cur:
            cur.execute(sql, (now, title, thesis_text, suggested_universe, suggested_features, model_id))
            if self._mode == "postgres":
                cur.execute("SELECT lastval()")
                return int(cur.fetchone()[0])
            return int(cur.lastrowid)

    def strategy_hypotheses(self, status: str | None = None, limit: int = 50) -> list[dict]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?"); params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = self._p(f"SELECT * FROM strategy_hypotheses {where} ORDER BY id DESC LIMIT ?")
        with self._cursor() as cur:
            cur.execute(sql, [*params, limit])
            return self._rows(cur, cur.fetchall())

    def mark_hypothesis_tested(self, hypothesis_id: int, validation_run_id: str, validation_verdict: str) -> bool:
        sql = self._p(
            "UPDATE strategy_hypotheses SET status='tested', validation_run_id=?, "
            "validation_verdict=? WHERE id=?"
        )
        with self._cursor() as cur:
            cur.execute(sql, (validation_run_id, validation_verdict, hypothesis_id))
            return cur.rowcount > 0

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
                conn.close()
                self._local.conn = None

    def checkpoint(self) -> None:
        # No-op under journal_mode=DELETE (see _sqlite_conn()) — DELETE mode
        # commits straight to the main .db file each transaction, unlike WAL,
        # which is what this used to flush. Postgres mode never needed this.
        pass
