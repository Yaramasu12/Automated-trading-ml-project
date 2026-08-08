"""
Configuration — environment-driven, typed, validated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"
    BACKTEST = "backtest"


class Env(str, Enum):
    DEV = "dev"
    PROD = "prod"


@dataclass(slots=True)
class BrokerConfig:
    """Broker connection settings."""
    gateway_url: str = "localhost"
    gateway_port: int = 4000
    api_key: str = ""
    api_secret: str = ""
    app_id: str = ""
    # IBKR specific
    ib_gateway_host: str = "localhost"
    ib_gateway_port: int = 4002
    ib_client_id: str = "algo-trader-01"
    ib_read_only: bool = True  # False only for live
    # Crypto venue
    crypto_api_key: str = ""
    crypto_api_secret: str = ""
    crypto_passphrase: str = ""


@dataclass(slots=True)
class StorageConfig:
    """Database and storage connection settings."""
    # ClickHouse
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_db: str = "trading"
    clickhouse_user: str = "default"
    clickhouse_password: str = ""

    # TimescaleDB / PostgreSQL
    timescaledb_host: str = "localhost"
    timescaledb_port: int = 5432
    timescaledb_db: str = "trading"
    timescaledb_user: str = "default"
    timescaledb_password: str = ""

    # PostgreSQL (audit/config)
    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_db: str = "config"
    postgres_user: str = "default"
    postgres_password: str = ""

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "trading-data"

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""

    # Qdrant
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_api_key: str = ""


@dataclass(slots=True)
class EventBusConfig:
    """Redpanda / Kafka event bus settings."""
    bootstrap_servers: str = "localhost:9092"
    tick_topic: str = "market.ticks"
    bar_topic: str = "market.bars"
    order_topic: str = "execution.orders"
    fill_topic: str = "execution.fills"
    risk_topic: str = "risk.signals"
    group_id: str = "strategy-engine"


@dataclass(slots=True)
class RiskConfig:
    """Risk limits — strategy can never widen these."""
    # Per-order
    max_order_size: float = 1000.0
    max_notional_per_order: float = 50000.0
    max_orders_per_second: int = 10

    # Per-symbol
    max_exposure_per_symbol: float = 100000.0

    # Portfolio-level
    max_gross_exposure: float = 500000.0
    max_net_exposure: float = 250000.0
    max_leverage: float = 1.0

    # Drawdown
    max_daily_loss: float = 5000.0
    max_weekly_loss: float = 15000.0
    max_drawdown_pct: float = 0.10  # 10%

    # Price collars
    price_collar_pct: float = 0.05  # 5% from last price

    # Kill switch
    kill_switch_enabled: bool = True
    kill_switch_endpoint: str = "redis://localhost:6379/15/kill_switch"


@dataclass(slots=True)
class StrategyConfig:
    """Strategy engine configuration."""
    mode: Mode = Mode.PAPER
    confirm_live: bool = False
    seed: int = 42
    # Signal combination
    ensemble_weights: dict[str, float] = field(default_factory=dict)
    # Position sizing
    vol_target_annual: float = 0.15
    kelly_fraction: float = 0.25
    # Regime detection
    regime_window: int = 60


@dataclass(slots=True)
class MonitoringConfig:
    """Observability configuration."""
    prometheus_host: str = "localhost"
    prometheus_port: int = 9090
    grafana_host: str = "localhost"
    grafana_port: int = 3000
    grafana_admin_user: str = "admin"
    grafana_admin_password: str = "admin"
    loki_host: str = "localhost"
    loki_port: int = 3100
    alertmanager_host: str = "localhost"
    alertmanager_port: int = 9093
    mlflow_tracking_uri: str = "http://localhost:5000"
    mlflow_registry_uri: str = "http://localhost:5000"


@dataclass(slots=True)
class AppConfig:
    """Top-level application configuration."""
    mode: Mode = Mode.PAPER
    confirm_live: bool = False
    env: Env = Env.DEV
    log_level: str = "INFO"
    data_dir: Path = field(default_factory=lambda: Path("data"))
    models_dir: Path = field(default_factory=lambda: Path("models"))
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    event_bus: EventBusConfig = field(default_factory=EventBusConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)

    @property
    def allow_live_trading(self) -> bool:
        """Check if live trading is permitted."""
        return self.mode == Mode.LIVE and self.confirm_live


def load_config(env: str | None = None) -> AppConfig:
    """Load configuration from environment variables with defaults."""
    env = env or os.getenv("APP_ENV", "dev")

    mode_str = os.getenv("MODE", "paper")
    mode = Mode(mode_str)
    confirm_live = os.getenv("CONFIRM_LIVE", "") == "I_UNDERSTAND"

    return AppConfig(
        mode=mode,
        confirm_live=confirm_live,
        env=Env(env),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        broker=BrokerConfig(
            gateway_url=os.getenv("GATEWAY_URL", "localhost"),
            gateway_port=int(os.getenv("GATEWAY_PORT", "4000")),
            api_key=os.getenv("BROKER_API_KEY", ""),
            api_secret=os.getenv("BROKER_API_SECRET", ""),
            app_id=os.getenv("BROKER_APP_ID", ""),
            ib_gateway_host=os.getenv("IB_GATEWAY_HOST", "localhost"),
            ib_gateway_port=int(os.getenv("IB_GATEWAY_PORT", "4002")),
            ib_client_id=os.getenv("IB_CLIENT_ID", "algo-trader-01"),
            ib_read_only=not confirm_live,
            crypto_api_key=os.getenv("CRYPTO_API_KEY", ""),
            crypto_api_secret=os.getenv("CRYPTO_API_SECRET", ""),
            crypto_passphrase=os.getenv("CRYPTO_PASSPHRASE", ""),
        ),
        storage=StorageConfig(
            clickhouse_host=os.getenv("CLICKHOUSE_HOST", "localhost"),
            clickhouse_port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
            clickhouse_db=os.getenv("CLICKHOUSE_DB", "trading"),
            clickhouse_user=os.getenv("CLICKHOUSE_USER", "default"),
            clickhouse_password=os.getenv("CLICKHOUSE_PASSWORD", ""),
            timescaledb_host=os.getenv("TIMESCALEDB_HOST", "localhost"),
            timescaledb_port=int(os.getenv("TIMESCALEDB_PORT", "5432")),
            timescaledb_db=os.getenv("TIMESCALEDB_DB", "trading"),
            timescaledb_user=os.getenv("TIMESCALEDB_USER", "default"),
            timescaledb_password=os.getenv("TIMESCALEDB_PASSWORD", ""),
            postgres_host=os.getenv("POSTGRES_HOST", "localhost"),
            postgres_port=int(os.getenv("POSTGRES_PORT", "5433")),
            postgres_db=os.getenv("POSTGRES_DB", "config"),
            postgres_user=os.getenv("POSTGRES_USER", "default"),
            postgres_password=os.getenv("POSTGRES_PASSWORD", ""),
            minio_endpoint=os.getenv("MINIO_ENDPOINT", "localhost:9000"),
            minio_access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
            minio_secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
            minio_bucket=os.getenv("MINIO_BUCKET", "trading-data"),
            redis_host=os.getenv("REDIS_HOST", "localhost"),
            redis_port=int(os.getenv("REDIS_PORT", "6379")),
            redis_db=int(os.getenv("REDIS_DB", "0")),
            redis_password=os.getenv("REDIS_PASSWORD", ""),
            qdrant_host=os.getenv("QDRANT_HOST", "localhost"),
            qdrant_port=int(os.getenv("QDRANT_PORT", "6333")),
            qdrant_api_key=os.getenv("QDRANT_API_KEY", ""),
        ),
        event_bus=EventBusConfig(
            bootstrap_servers=os.getenv("REDPANDA_BOOTSTRAP", "localhost:9092"),
        ),
        risk=RiskConfig(
            max_order_size=float(os.getenv("RISK_MAX_ORDER_SIZE", "1000")),
            max_notional_per_order=float(os.getenv("RISK_MAX_NOTIONAL", "50000")),
            max_orders_per_second=int(os.getenv("RISK_MAX_ORDERS_PER_SEC", "10")),
            max_exposure_per_symbol=float(os.getenv("RISK_MAX_EXPOSURE_SYMBOL", "100000")),
            max_gross_exposure=float(os.getenv("RISK_MAX_GROSS", "500000")),
            max_net_exposure=float(os.getenv("RISK_MAX_NET", "250000")),
            max_leverage=float(os.getenv("RISK_MAX_LEVERAGE", "1.0")),
            max_daily_loss=float(os.getenv("RISK_MAX_DAILY_LOSS", "5000")),
            max_weekly_loss=float(os.getenv("RISK_MAX_WEEKLY_LOSS", "15000")),
            max_drawdown_pct=float(os.getenv("RISK_MAX_DRAWDOWN", "0.10")),
        ),
        strategy=StrategyConfig(
            mode=mode,
            confirm_live=confirm_live,
            seed=int(os.getenv("SEED", "42")),
        ),
        monitoring=MonitoringConfig(
            prometheus_host=os.getenv("PROMETHEUS_HOST", "localhost"),
            prometheus_port=int(os.getenv("PROMETHEUS_PORT", "9090")),
            grafana_host=os.getenv("GRAFANA_HOST", "localhost"),
            grafana_port=int(os.getenv("GRAFANA_PORT", "3000")),
            grafana_admin_user=os.getenv("GRAFANA_USER", "admin"),
            grafana_admin_password=os.getenv("GRAFANA_PASSWORD", "admin"),
            mlflow_tracking_uri=os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000"),
        ),
    )


__all__ = ["AppConfig", "BrokerConfig", "StorageConfig", "EventBusConfig", "RiskConfig", "StrategyConfig", "MonitoringConfig", "Mode", "Env", "load_config"]