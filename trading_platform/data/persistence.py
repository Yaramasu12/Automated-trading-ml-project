from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Generator

_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "trading.db"

_CREATE_TRADES = """
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
    execution_mode TEXT NOT NULL DEFAULT 'BACKTEST'
)
"""

_CREATE_PORTFOLIO_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at   TEXT NOT NULL,
    cash          REAL NOT NULL,
    equity        REAL NOT NULL,
    realized_pnl  REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    drawdown      REAL NOT NULL,
    execution_mode TEXT NOT NULL DEFAULT 'BACKTEST'
)
"""

_CREATE_DAILY_PNL = """
CREATE TABLE IF NOT EXISTS daily_pnl (
    trade_date    TEXT PRIMARY KEY,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    total_trades  INTEGER NOT NULL DEFAULT 0,
    winning_trades INTEGER NOT NULL DEFAULT 0,
    ending_equity REAL NOT NULL DEFAULT 0
)
"""

_CREATE_MODEL_RUNS = """
CREATE TABLE IF NOT EXISTS model_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TEXT NOT NULL,
    model_name    TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    regime        TEXT,
    signal        TEXT,
    confidence    REAL,
    metadata      TEXT
)
"""

_CREATE_RISK_EVENTS = """
CREATE TABLE IF NOT EXISTS risk_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at   TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    symbol        TEXT,
    reason        TEXT NOT NULL,
    risk_score    REAL,
    approved      INTEGER NOT NULL DEFAULT 0
)
"""


class TradingDatabase:
    """Thread-safe SQLite journal for trades, portfolio snapshots, and risk events.

    Uses WAL mode so reads never block writes and vice versa — safe for
    the async FastAPI + background scheduler pattern.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    # ------------------------------------------------------------------
    # Internal connection management
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
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
            cur.execute(_CREATE_TRADES)
            cur.execute(_CREATE_PORTFOLIO_SNAPSHOTS)
            cur.execute(_CREATE_DAILY_PNL)
            cur.execute(_CREATE_MODEL_RUNS)
            cur.execute(_CREATE_RISK_EVENTS)

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def save_trade(self, trade, execution_mode: str = "BACKTEST") -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO trades
                   (trade_id, order_id, symbol, side, quantity, price, charges,
                    timestamp, strategy_name, execution_mode)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade.trade_id,
                    trade.order_id,
                    trade.symbol,
                    trade.side.value,
                    trade.quantity,
                    trade.price,
                    trade.charges,
                    trade.timestamp.isoformat(),
                    trade.strategy_name,
                    execution_mode,
                ),
            )

    def trades(
        self,
        symbol: str | None = None,
        since: datetime | None = None,
        execution_mode: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        clauses = []
        params: list = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        if execution_mode:
            clauses.append("execution_mode = ?")
            params.append(execution_mode)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._cursor() as cur:
            cur.execute(
                f"SELECT * FROM trades {where} ORDER BY timestamp DESC LIMIT ?",
                [*params, limit],
            )
            return [dict(row) for row in cur.fetchall()]

    def trade_count(self, execution_mode: str | None = None) -> int:
        with self._cursor() as cur:
            if execution_mode:
                cur.execute("SELECT COUNT(*) FROM trades WHERE execution_mode=?", (execution_mode,))
            else:
                cur.execute("SELECT COUNT(*) FROM trades")
            return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # Portfolio snapshots
    # ------------------------------------------------------------------

    def save_snapshot(self, snapshot, execution_mode: str = "BACKTEST") -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO portfolio_snapshots
                   (recorded_at, cash, equity, realized_pnl, unrealized_pnl, drawdown, execution_mode)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    snapshot.cash,
                    snapshot.equity,
                    snapshot.realized_pnl,
                    snapshot.unrealized_pnl,
                    snapshot.drawdown,
                    execution_mode,
                ),
            )

    def latest_snapshot(self, execution_mode: str | None = None) -> dict | None:
        with self._cursor() as cur:
            if execution_mode:
                cur.execute(
                    "SELECT * FROM portfolio_snapshots WHERE execution_mode=? ORDER BY id DESC LIMIT 1",
                    (execution_mode,),
                )
            else:
                cur.execute("SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            return dict(row) if row else None

    def equity_curve(self, execution_mode: str | None = None, limit: int = 200) -> list[dict]:
        with self._cursor() as cur:
            if execution_mode:
                cur.execute(
                    "SELECT recorded_at, equity, drawdown FROM portfolio_snapshots WHERE execution_mode=? ORDER BY id DESC LIMIT ?",
                    (execution_mode, limit),
                )
            else:
                cur.execute(
                    "SELECT recorded_at, equity, drawdown FROM portfolio_snapshots ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            return list(reversed([dict(row) for row in cur.fetchall()]))

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
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO daily_pnl
                   (trade_date, realized_pnl, unrealized_pnl, total_trades, winning_trades, ending_equity)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(trade_date) DO UPDATE SET
                     realized_pnl=excluded.realized_pnl,
                     unrealized_pnl=excluded.unrealized_pnl,
                     total_trades=excluded.total_trades,
                     winning_trades=excluded.winning_trades,
                     ending_equity=excluded.ending_equity""",
                (
                    trade_date.isoformat(),
                    realized_pnl,
                    unrealized_pnl,
                    total_trades,
                    winning_trades,
                    ending_equity,
                ),
            )

    def daily_pnl_history(self, limit: int = 30) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM daily_pnl ORDER BY trade_date DESC LIMIT ?", (limit,))
            return list(reversed([dict(row) for row in cur.fetchall()]))

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
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO model_runs (run_at, model_name, symbol, regime, signal, confidence, metadata)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    model_name,
                    symbol,
                    regime,
                    signal,
                    confidence,
                    json.dumps(metadata) if metadata else None,
                ),
            )

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
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO risk_events (occurred_at, event_type, symbol, reason, risk_score, approved)
                   VALUES (?,?,?,?,?,?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    event_type,
                    symbol,
                    reason,
                    risk_score,
                    int(approved),
                ),
            )

    def recent_risk_events(self, limit: int = 50) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM trades")
            total_trades = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM trades WHERE execution_mode='LIVE'")
            live_trades = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM portfolio_snapshots")
            snapshots = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM risk_events WHERE approved=0")
            risk_blocks = cur.fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "total_trades": total_trades,
            "live_trades": live_trades,
            "portfolio_snapshots": snapshots,
            "risk_blocks": risk_blocks,
        }

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None
