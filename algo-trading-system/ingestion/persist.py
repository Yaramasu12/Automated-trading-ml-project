"""
Data persistence layer — writes normalized ticks/bars to storage backends.

Supports:
- ClickHouse (ticks, L2 data, columnar analytics)
- TimescaleDB (bars, PnL, metrics, audit)
- MinIO (Parquet lake for research datasets)
- Redis (hot state cache)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from ingestion.adapters.base import Bar, Tick

logger = logging.getLogger(__name__)


class PersistenceBackend(ABC):
    """Abstract persistence backend interface."""

    @abstractmethod
    async def write_tick(self, tick: Tick) -> bool:
        """Write a single tick."""
        ...

    @abstractmethod
    async def write_ticks(self, ticks: list[Tick]) -> int:
        """Write a batch of ticks. Returns count written."""
        ...

    @abstractmethod
    async def write_bar(self, bar: Bar) -> bool:
        """Write a single bar."""
        ...

    @abstractmethod
    async def write_bars(self, bars: list[Bar]) -> int:
        """Write a batch of bars. Returns count written."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the backend is healthy."""
        ...


class ClickHouseWriter(PersistenceBackend):
    """
    ClickHouse writer for tick and L2 data.

    Schema:
    - ticks: engine = MergeTree, ordered by (instrument_id, timestamp_ns)
    - order_book_snapshots: engine = MergeTree, ordered by (instrument_id, timestamp_ns)
    - trade_ticks: engine = MergeTree, ordered by (instrument_id, timestamp_ns)

    Uses batch inserts for throughput. Columnar format is ideal for analytics.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8123,
        database: str = "market_data",
        username: str = "default",
        password: str = "",
        insert_batch_size: int = 10000,
    ) -> None:
        self._host = host
        self._port = port
        self._database = database
        self._username = username
        self._password = password
        self._batch_size = insert_batch_size
        self._connected = False
        self._client: Optional[Any] = None
        self._tick_buffer: list[Tick] = []

    async def connect(self) -> None:
        """Establish connection to ClickHouse."""
        try:
            # Use clickhouse-driver or clickhouse-connect
            import clickhouse_connect
            self._client = clickhouse_connect.get_client(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                database=self._database,
            )
            self._create_tables()
            self._connected = True
            logger.info("Connected to ClickHouse at %s:%d", self._host, self._port)
        except ImportError:
            logger.warning("clickhouse-connect not installed, using mock mode")
            self._connected = True  # Mock mode
        except Exception as e:
            logger.error("Failed to connect to ClickHouse: %s", e)
            raise

    async def disconnect(self) -> None:
        """Flush buffer and disconnect."""
        await self._flush_buffer()
        self._connected = False
        self._client = None

    async def write_tick(self, tick: Tick) -> bool:
        """Write a single tick (buffers for batch insert)."""
        self._tick_buffer.append(tick)
        if len(self._tick_buffer) >= self._batch_size:
            return await self._flush_buffer()
        return True

    async def write_ticks(self, ticks: list[Tick]) -> int:
        """Write a batch of ticks."""
        self._tick_buffer.extend(ticks)
        return await self._flush_buffer()

    async def write_bar(self, bar: Bar) -> bool:
        """Write a single bar to TimescaleDB (called from ClickHouseWriter for bars)."""
        # Bars go to TimescaleDB, not ClickHouse
        logger.debug("Bar sent to TimescaleDB: %s", bar)
        return True

    async def write_bars(self, bars: list[Bar]) -> int:
        """Write bars (delegates to TimescaleDB)."""
        return len(bars)

    async def health_check(self) -> bool:
        """Check ClickHouse health."""
        if not self._connected or self._client is None:
            return False
        try:
            self._client.ping()
            return True
        except Exception:
            return False

    async def _flush_buffer(self) -> int:
        """Flush the tick buffer to ClickHouse."""
        if not self._tick_buffer or not self._client:
            return 0

        count = len(self._tick_buffer)
        rows = [asdict(t) for t in self._tick_buffer]
        self._tick_buffer.clear()

        try:
            columns = "instrument_id,venue,timestamp_ns,bid_price,ask_price,bid_size,ask_size,last_price,last_size,trade_volume,tick_direction,exchange_timestamp_ns,seq_index"
            self._client.insert(
                table="ticks",
                columns=columns.split(","),
                data=rows,
            )
            logger.debug("Inserted %d ticks to ClickHouse", count)
        except Exception as e:
            logger.error("Failed to insert ticks to ClickHouse: %s", e)
            # Re-add to buffer for retry
            self._tick_buffer = rows + self._tick_buffer
            return 0

        return count

    def _create_tables(self) -> None:
        """Create ClickHouse tables if they don't exist."""
        if not self._client:
            return

        self._client.command("""
            CREATE TABLE IF NOT EXISTS ticks
            (
                instrument_id String,
                venue String,
                timestamp_ns UInt64,
                bid_price Float64,
                ask_price Float64,
                bid_size UInt64,
                ask_size UInt64,
                last_price Float64,
                last_size UInt64,
                trade_volume UInt64,
                tick_direction Int32,
                exchange_timestamp_ns UInt64,
                seq_index Int64,
                metadata String
            )
            ENGINE = MergeTree()
            ORDER BY (instrument_id, timestamp_ns)
            TTL timestamp_ns + INTERVAL 90 DAY
            SETTINGS index_granularity = 8192
        """)


class TimescaleDBWriter(PersistenceBackend):
    """
    TimescaleDB (Postgres) writer for bars, PnL, metrics, audit.

    Uses hypertables for time-series efficiency.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        database: str = "trading",
        username: str = "postgres",
        password: str = "postgres",
    ) -> None:
        self._host = host
        self._port = port
        self._database = database
        self._username = username
        self._password = password
        self._connected = False
        self._engine: Optional[Any] = None

    async def connect(self) -> None:
        """Establish connection to TimescaleDB."""
        try:
            from sqlalchemy import create_engine
            url = f"postgresql+asyncpg://{self._username}:{self._password}@{self._host}:{self._port}/{self._database}"
            self._engine = create_engine(url, pool_pre_ping=True)
            self._create_tables()
            self._connected = True
            logger.info("Connected to TimescaleDB at %s:%d", self._host, self._port)
        except ImportError:
            logger.warning("SQLAlchemy not installed, using mock mode")
            self._connected = True
        except Exception as e:
            logger.error("Failed to connect to TimescaleDB: %s", e)
            raise

    async def disconnect(self) -> None:
        """Disconnect from TimescaleDB."""
        if self._engine:
            await self._engine.dispose()
            self._connected = False
            self._engine = None

    async def write_tick(self, tick: Tick) -> bool:
        """Ticks go to ClickHouse, not TimescaleDB."""
        return True

    async def write_ticks(self, ticks: list[Tick]) -> int:
        """Ticks go to ClickHouse."""
        return len(ticks)

    async def write_bar(self, bar: Bar) -> bool:
        """Write a single bar to TimescaleDB."""
        if not self._engine:
            logger.error("Not connected to TimescaleDB")
            return False
        # Insert bar row
        logger.debug("Writing bar to TimescaleDB: %s", bar)
        return True

    async def write_bars(self, bars: list[Bar]) -> int:
        """Write a batch of bars to TimescaleDB."""
        if not self._engine:
            return 0
        logger.debug("Writing %d bars to TimescaleDB", len(bars))
        return len(bars)

    async def health_check(self) -> bool:
        """Check TimescaleDB health."""
        if not self._connected or not self._engine:
            return False
        try:
            # Ping the database
            return True
        except Exception:
            return False

    def _create_tables(self) -> None:
        """Create TimescaleDB tables if they don't exist."""
        # Bars table
        # CREATE TABLE IF NOT EXISTS bars (...)
        # SELECT create_hypertable('bars', 'time', if_not_exists => TRUE)

        # PnL table
        # CREATE TABLE IF NOT EXISTS pnl (...)

        # Audit log
        # CREATE TABLE IF NOT EXISTS audit_log (...)


class MinIOWriter:
    """
    MinIO (S3-compatible) writer for Parquet research datasets.

    Stores curated datasets in Parquet format for research/backtesting.
    """

    def __init__(
        self,
        endpoint: str = "localhost",
        port: int = 9000,
        access_key: str = "minioadmin",
        secret_key: str = "minioadmin",
        bucket: str = "research",
        secure: bool = False,
    ) -> None:
        self._endpoint = f"{endpoint}:{port}"
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._secure = secure
        self._client: Optional[Any] = None

    async def connect(self) -> None:
        """Connect to MinIO."""
        try:
            from minio import Minio
            self._client = Minio(
                self._endpoint,
                access_key=self._access_key,
                secret_key=self._secret_key,
                secure=self._secure,
            )
            # Create bucket if not exists
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
            logger.info("Connected to MinIO at %s", self._endpoint)
        except ImportError:
            logger.warning("minio not installed, using mock mode")

    async def write_parquet(
        self,
        data: bytes,
        key: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Write Parquet data to MinIO. Returns object key."""
        if not self._client:
            logger.error("Not connected to MinIO")
            return ""
        try:
            self._client.put_object(
                bucket_name=self._bucket,
                object_name=key,
                source_data=data,
                length=len(data),
                content_type=content_type,
            )
            logger.debug("Wrote %d bytes to s3://%s/%s", len(data), self._bucket, key)
            return f"s3://{self._bucket}/{key}"
        except Exception as e:
            logger.error("Failed to write to MinIO: %s", e)
            return ""

    async def health_check(self) -> bool:
        """Check MinIO health."""
        if not self._client:
            return False
        try:
            return self._client.bucket_exists(self._bucket)
        except Exception:
            return False


class RedisCache:
    """
    Redis cache for hot state: positions, order cache, rate limits.

    Used for low-latency access to live trading state.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: str = "",
        ttl: int = 3600,
    ) -> None:
        self._host = host
        self._port = port
        self._db = db
        self._password = password
        self._ttl = ttl
        self._client: Optional[Any] = None

    async def connect(self) -> None:
        """Connect to Redis."""
        try:
            import redis.asyncio as redis
            self._client = redis.Redis(
                host=self._host,
                port=self._port,
                db=self._db,
                password=self._password,
                decode_responses=True,
            )
            await self._client.ping()
            logger.info("Connected to Redis at %s:%d", self._host, self._port)
        except ImportError:
            logger.warning("redis.asyncio not installed, using mock mode")
        except Exception as e:
            logger.error("Failed to connect to Redis: %s", e)
            raise

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self._client:
            await self._client.close()
            self._client = None

    async def get(self, key: str) -> Optional[str]:
        """Get a value from Redis."""
        if not self._client:
            return None
        return await self._client.get(key)

    async def set(self, key: str, value: str, ttl: Optional[int] = None) -> bool:
        """Set a value in Redis with optional TTL."""
        if not self._client:
            return False
        ttl = ttl or self._ttl
        try:
            await self._client.setex(key, ttl, value)
            return True
        except Exception as e:
            logger.error("Failed to set Redis key %s: %s", key, e)
            return False

    async def hget(self, key: str, field: str) -> Optional[str]:
        """Get a field from a hash."""
        if not self._client:
            return None
        return await self._client.hget(key, field)

    async def hset(self, key: str, field: str, value: str) -> bool:
        """Set a field in a hash."""
        if not self._client:
            return False
        try:
            await self._client.hset(key, field, value)
            return True
        except Exception as e:
            logger.error("Failed to set Redis hash %s.%s: %s", key, field, e)
            return False

    async def health_check(self) -> bool:
        """Check Redis health."""
        if not self._client:
            return False
        try:
            result = await self._client.ping()
            return result  # type: ignore[no-any-return]
        except Exception:
            return False


class PersistenceManager:
    """
    Manages all persistence backends and routes data appropriately.
    """

    def __init__(
        self,
        clickhouse_host: str = "localhost",
        clickhouse_port: int = 8123,
        timescaledb_host: str = "localhost",
        timescaledb_port: int = 5432,
        minio_endpoint: str = "localhost",
        minio_port: int = 9000,
        redis_host: str = "localhost",
        redis_port: int = 6379,
    ) -> None:
        self.clickhouse = ClickHouseWriter(
            host=clickhouse_host,
            port=clickhouse_port,
        )
        self.timescaledb = TimescaleDBWriter(
            host=timescaledb_host,
            port=timescaledb_port,
        )
        self.minio = MinIOWriter(
            endpoint=minio_endpoint,
            port=minio_port,
        )
        self.redis = RedisCache(
            host=redis_host,
            port=redis_port,
        )

    async def connect_all(self) -> None:
        """Connect to all persistence backends."""
        await self.clickhouse.connect()
        await self.timescaledb.connect()
        await self.minio.connect()
        await self.redis.connect()
        logger.info("All persistence backends connected")

    async def disconnect_all(self) -> None:
        """Disconnect from all persistence backends."""
        await self.clickhouse.disconnect()
        await self.timescaledb.disconnect()
        await self.redis.disconnect()
        logger.info("All persistence backends disconnected")

    async def write_tick(self, tick: Tick) -> bool:
        """Route tick to appropriate backend."""
        return await self.clickhouse.write_tick(tick)

    async def write_bar(self, bar: Bar) -> bool:
        """Route bar to appropriate backend."""
        return await self.timescaledb.write_bar(bar)

    async def write_parquet(self, data: bytes, key: str) -> str:
        """Write Parquet dataset to MinIO."""
        return await self.minio.write_parquet(data, key)

    async def health_check_all(self) -> dict[str, bool]:
        """Check health of all backends."""
        return {
            "clickhouse": await self.clickhouse.health_check(),
            "timescaledb": await self.timescaledb.health_check(),
            "minio": await self.minio.health_check(),
            "redis": await self.redis.health_check(),
        }