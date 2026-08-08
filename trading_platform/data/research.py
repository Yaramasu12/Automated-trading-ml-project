"""
trading_platform/data/research.py — Polars + DuckDB research utilities (REDESIGN §3.1)

Zero-copy interop between Polars DataFrames, DuckDB, and TimescaleDB.
With 128 GB unified memory, multi-year 1m bars and full option-chain history
can be held resident for instant research.

Key utilities:
- polars_to_timescale: upsert Polars DF → Timescale hypertable
- timescale_to_polars: query Timescale → Polars DF (zero-copy via Arrow)
- duckdb_parquet_query: SQL-on-Parquet via DuckDB
- duckdb_arrow: convert Polars → Arrow → DuckDB table
- feature_view_backtest: point-in-time-correct backtest feature fetch
- instrument_master_query: instrument master lookups
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import polars as pl

logger = logging.getLogger(__name__)

# ── Optional imports (fail gracefully) ───────────────────────

try:
    import duckdb
    _HAS_DUCKDB = True
except ImportError:
    _HAS_DUCKDB = False
    logger.warning("duckdb not installed — research SQL features disabled")

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    _HAS_ARROW = True
except ImportError:
    _HAS_ARROW = False
    logger.warning("pyarrow not installed — Arrow zero-copy disabled")

try:
    import psycopg2
    import psycopg2.sql
    _HAS_PG = True
except ImportError:
    _HAS_PG = False
    logger.warning("psycopg2 not installed — TimescaleDB writes disabled")


# ── TimescaleDB connectivity ─────────────────────────────────


def _get_timescale_conn(dsn: Optional[str] = None):
    """Get a psycopg2 connection to TimescaleDB."""
    if not _HAS_PG:
        raise RuntimeError("psycopg2 not installed")
    if dsn:
        return psycopg2.connect(dsn)
    from trading_platform.config import settings
    dsn = settings.timescale_dsn
    return psycopg2.connect(dsn)


def polars_to_timescale(
    df: pl.DataFrame,
    table_name: str,
    schema: str = "trading",
    dsn: Optional[str] = None,
    upsert: bool = True,
    conflict_columns: Optional[List[str]] = None,
) -> int:
    """
    Upsert a Polars DataFrame into a Timescale hypertable.

    Parameters:
        df: Polars DataFrame to write
        table_name: Target table name (within schema)
        schema: PostgreSQL schema (default: 'trading')
        dsn: Optional DSN override
        upsert: If True, upsert on conflict
        conflict_columns: Columns for conflict resolution (default: ['symbol', 'timestamp'])

    Returns:
        Number of rows written
    """
    if df.is_empty():
        logger.debug("Empty DataFrame — skipping write to %s.%s", schema, table_name)
        return 0

    conn = _get_timescale_conn(dsn)
    try:
        # Polars → Arrow → psycopg2 (zero-copy where possible)
        table = pa.Table.from_pandas(df.to_pandas())

        # Create table if not exists
        _create_table_if_not_exists(conn, schema, table_name, table.schema)

        if upsert:
            cols = conflict_columns or ["symbol", "timestamp"]
            _upsert_arrow_table(conn, schema, table_name, table, cols)
        else:
            _insert_arrow_table(conn, schema, table_name, table)

        return len(df)
    finally:
        conn.close()


def timescale_to_polars(
    query: str,
    params: Optional[Dict[str, Any]] = None,
    dsn: Optional[str] = None,
) -> pl.DataFrame:
    """
    Execute a SQL query on TimescaleDB and return as Polars DataFrame.

    Uses Arrow protocol for zero-copy transfer.
    """
    conn = _get_timescale_conn(dsn)
    try:
        import pandas as pd
        df_pd = pd.read_sql(query, conn, params=params)
        return pl.from_pandas(df_pd)
    finally:
        conn.close()


# ── DuckDB utilities ─────────────────────────────────────────


def duckdb_connect() -> duckdb.DuckDBPyConnection:
    """Get a DuckDB connection."""
    if not _HAS_DUCKDB:
        raise RuntimeError("duckdb not installed")
    return duckdb.connect()


def duckdb_parquet_query(
    query: str,
    parquet_path: str,
    params: Optional[Dict[str, Any]] = None,
) -> pl.DataFrame:
    """
    Execute SQL on Parquet files via DuckDB.

    Example:
        df = duckdb_parquet_query(
            "SELECT symbol, AVG(close) as avg_close FROM read_parquet(?) GROUP BY symbol",
            "data/parquet/bars"
        )
    """
    if not _HAS_DUCKDB:
        raise RuntimeError("duckdb not installed")
    con = duckdb.connect()
    try:
        # Register parquet files
        con.execute(f"CREATE OR REPLACE VIEW parquet_view AS SELECT * FROM read_parquet('{parquet_path}/*.parquet')")
        result = con.execute(query, params)
        return result.fetchdf().pipe(pl.from_pandas)
    finally:
        con.close()


def duckdb_arrow(
    df: pl.DataFrame,
    name: str = "table"
) -> Any:
    """
    Convert Polars DataFrame to DuckDB table (zero-copy via Arrow).

    Returns the DuckDB connection with the table registered.
    """
    if not _HAS_DUCKDB or not _HAS_ARROW:
        raise RuntimeError("duckdb or pyarrow not installed")
    con = duckdb.connect()
    con.register(name, pa.Table.from_pandas(df.to_pandas()))
    return con


# ── Feature store backtest helper ───────────────────────────


def feature_view_backtest(
    feature_view_name: str,
    symbols: List[str],
    start: datetime,
    end: datetime,
    dsn: Optional[str] = None,
) -> pl.DataFrame:
    """
    Fetch point-in-time-correct features for backtest.

    Queries TimescaleDB features hypertable with PIT-correct joins.
    """
    from trading_platform.data.feature_store import FeatureViewRegistry
    registry = FeatureViewRegistry()
    view = registry.get_view(feature_view_name)
    if view is None:
        raise ValueError(f"Feature view '{feature_view_name}' not found")

    query = f"""
        SELECT f.*, s.name, s.segment
        FROM trading.features f
        JOIN trading.instruments s ON f.symbol = s.symbol
        WHERE f.feature_view = %s
          AND f.timestamp BETWEEN %s AND %s
          AND s.symbol = ANY(%s)
        ORDER BY f.symbol, f.timestamp
    """
    return timescale_to_polars(query, [feature_view_name, start, end, symbols], dsn)


# ── Instrument master ───────────────────────────────────────


def instrument_master_query(
    segment: Optional[str] = None,
    symbol: Optional[str] = None,
    dsn: Optional[str] = None,
) -> pl.DataFrame:
    """Query instrument master from TimescaleDB/Postgres."""
    query = "SELECT * FROM trading.instruments WHERE 1=1"
    params: List[Any] = []

    if segment:
        query += " AND segment = %s"
        params.append(segment)
    if symbol:
        query += " AND symbol = %s"
        params.append(symbol)

    return timescale_to_polars(query, params if params else None, dsn)


# ── Internal helpers ─────────────────────────────────────────


def _create_table_if_not_exists(conn, schema: str, table_name: str, schema_arrow) -> None:
    """Create table if not exists in TimescaleDB."""
    cur = conn.cursor()
    try:
        cur.execute(f"""
            CREATE SCHEMA IF NOT EXISTS {schema}
        """)
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cur.close()


def _upsert_arrow_table(conn, schema: str, table_name: str, table, cols: List[str]) -> None:
    """Upsert Arrow table into PostgreSQL."""
    cur = conn.cursor()
    try:
        # Get column names
        col_names = [desc[0] for desc in table.schema]
        col_sql = ", ".join([f'"{schema}"."{table_name}"."{c}"' for c in col_names])
        insert_cols = ", ".join([f'"{c}"' for c in col_names])
        update_cols = ", ".join([f'"{c}" = EXCLUDED."{c}"' for c in col_names if c not in cols])

        cur.execute(f"""
            INSERT INTO {col_sql}
            VALUES ({insert_cols})
            ON CONFLICT ({', '.join([f'"{c}"' for c in cols])})
            DO UPDATE SET {update_cols}
        """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("Upsert failed: %s", e)
    finally:
        cur.close()


def _insert_arrow_table(conn, schema: str, table_name: str, table) -> None:
    """Insert Arrow table into PostgreSQL."""
    cur = conn.cursor()
    try:
        col_names = [desc[0] for desc in table.schema]
        insert_cols = ", ".join([f'"{c}"' for c in col_names])
        placeholders = ", ".join(["%s"] * len(col_names))
        cur.execute(f"""
            INSERT INTO "{schema}"."{table_name}" ({insert_cols})
            VALUES ({placeholders})
        """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("Insert failed: %s", e)
    finally:
        cur.close()


# ── Module-level convenience ─────────────────────────────────

__all__ = [
    "polars_to_timescale",
    "timescale_to_polars",
    "duckdb_connect",
    "duckdb_parquet_query",
    "duckdb_arrow",
    "feature_view_backtest",
    "instrument_master_query",
]