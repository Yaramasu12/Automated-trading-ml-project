"""
trading_platform/config.py — Extended settings for REDESIGN_PROMPT infrastructure (§3, §7, §11)

All new fields are gitignored via .env. Existing config is preserved.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from trading_platform.domain.enums import ExecutionMode
from trading_platform.logging_safety import install_secret_redaction

logger = logging.getLogger(__name__)

# Original pre-redesign safety/config surface (execution_mode, live-order
# gating, risk limits, angel_one_* credentials, feature flags, load_settings()
# factory with validation, ...) lives below alongside the newer REDESIGN_*
# fields. An earlier pass replaced this whole file with only the new
# UPPER_SNAKE_CASE fields and dropped 47 pre-existing lower_snake_case fields
# that most of the codebase (api/runtime.py, agent/trading_agent.py,
# broker/angel_one.py, ...) reads directly — see memory redesign-prompt-status
# for how this was found. Do not remove the lower_snake_case fields again;
# they are not legacy cruft, they are what the running app uses today.

LIVE_ORDER_CONFIRMATION_PHRASE = "I_ACCEPT_REAL_MONEY_LIVE_ORDERS"


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_local_env_files() -> None:
    _load_env_file(Path(".env"))
    _load_env_file(Path(".env.local"))


def _parse_cors_origins(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    parts = [p.strip() for p in raw.split(",")]
    return tuple(p for p in parts if p)


def _parse_csv_tuple(raw: str) -> tuple[str, ...]:
    return tuple(part.strip().upper() for part in raw.split(",") if part.strip())


# ──────────────────────────────────────────────
# Helper: env vars
# ──────────────────────────────────────────────


def _env(name: str, default: str, *, cast: Any = str) -> Any:
    """Read env var with optional cast."""
    raw = os.environ.get(name, default)
    if raw is None:
        return default
    return cast(raw)


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "y")


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _env_list(name: str, default: List[str]) -> List[str]:
    val = os.environ.get(name)
    if val is None:
        return list(default)
    return [x.strip() for x in val.split(",") if x.strip()]


def _env_dict(name: str, default: Dict[str, str]) -> Dict[str, str]:
    val = os.environ.get(name)
    if val is None:
        return dict(default)
    result: Dict[str, str] = {}
    for pair in val.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            result[k.strip()] = v.strip()
    return result


# ──────────────────────────────────────────────
# IST / NSE session helpers
# ──────────────────────────────────────────────


def _nse_session(t: Optional[time] = None) -> str:
    """Return 'PRE', 'OPEN', 'CLOSE', 'CLOSED' based on NSE session."""
    if t is None:
        from trading_platform.utils import now_ist
        t = now_ist().time()
    nse_pre = time(9, 0)
    nse_open = time(9, 15)
    nse_close = time(15, 30)
    nse_closing = time(15, 32)
    if t < nse_open:
        return "PRE"
    if t < nse_close:
        return "OPEN"
    if t < nse_closing:
        return "CLOSE"
    return "CLOSED"


# ──────────────────────────────────────────────
# Settings dataclass
# ──────────────────────────────────────────────


@dataclass
class Settings:
    # ── RESTORED (pre-redesign core — see module docstring) ──
    # execution_mode/live_trading_enabled/live_order_confirmation together
    # gate LIVE order submission (see can_submit_live_orders below) — do not
    # rename or rescale these; api/runtime.py and broker/angel_one.py read
    # them by these exact lower_snake_case names.
    execution_mode: ExecutionMode = field(
        default_factory=lambda: ExecutionMode(os.getenv("EXECUTION_MODE", "BACKTEST").upper())
    )
    broker: str = field(default_factory=lambda: _env("BROKER", "ANGEL_ONE"))
    live_trading_enabled: bool = field(default_factory=lambda: _bool_env("LIVE_TRADING_ENABLED", False))
    initial_capital: float = field(default_factory=lambda: _env_float("INITIAL_CAPITAL", 1000000.0))
    max_drawdown: float = field(default_factory=lambda: _env_float("MAX_DRAWDOWN", 0.10))
    max_daily_loss: float = field(default_factory=lambda: _env_float("MAX_DAILY_LOSS", 0.02))
    max_position_pct: float = field(default_factory=lambda: _env_float("MAX_POSITION_PCT", 0.05))
    max_margin_utilization: float = field(default_factory=lambda: _env_float("MAX_MARGIN_UTILIZATION", 0.60))
    live_order_confirmation: str = field(default_factory=lambda: _env("LIVE_ORDER_CONFIRMATION", ""))
    angel_one_api_key: str = field(default_factory=lambda: _env("ANGEL_ONE_API_KEY", ""))
    angel_one_api_secret: str = field(default_factory=lambda: _env("ANGEL_ONE_API_SECRET", ""))
    angel_one_client_code: str = field(default_factory=lambda: _env("ANGEL_ONE_CLIENT_CODE", ""))
    angel_one_pin: str = field(default_factory=lambda: _env("ANGEL_ONE_PIN", ""))
    angel_one_totp_secret: str = field(default_factory=lambda: _env("ANGEL_ONE_TOTP_SECRET", ""))
    angel_one_instrument_master_url: str = field(default_factory=lambda: _env(
        "ANGEL_ONE_INSTRUMENT_MASTER_URL",
        "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    ))
    angel_one_instrument_cache_path: str = field(default_factory=lambda: _env(
        "ANGEL_ONE_INSTRUMENT_CACHE_PATH", "data/processed/angel_one_instruments.json",
    ))
    # REDESIGN_PROMPT.md §2 explicitly calls for removing this vestige from
    # .env.example (100%-local/no-cloud constraint) — keep the field (some
    # pre-existing code may still read it) but it no longer has a live use.
    aws_region: str = field(default_factory=lambda: _env("AWS_REGION", ""))
    angel_one_algo_id: str = field(default_factory=lambda: _env("ANGEL_ONE_ALGO_ID", ""))
    api_auth_token: str = field(default_factory=lambda: _env("API_AUTH_TOKEN", ""))
    api_cors_origins: tuple[str, ...] = field(default_factory=lambda: _parse_cors_origins(
        os.getenv("API_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    ))
    api_auth_required: bool = field(default_factory=lambda: _bool_env("API_AUTH_REQUIRED", False))
    auto_start_agent: bool = field(default_factory=lambda: _bool_env("AUTO_START_AGENT", False))
    auto_start_live_feed: bool = field(default_factory=lambda: _bool_env("AUTO_START_LIVE_FEED", False))
    auto_load_instrument_cache: bool = field(default_factory=lambda: _bool_env("AUTO_LOAD_INSTRUMENT_CACHE", True))
    auto_load_models: bool = field(default_factory=lambda: _bool_env("AUTO_LOAD_MODELS", False))
    premarket_refresh_instruments: bool = field(default_factory=lambda: _bool_env("PREMARKET_REFRESH_INSTRUMENTS", False))
    live_feed_default_symbols: tuple[str, ...] = field(default_factory=lambda: _parse_csv_tuple(
        os.getenv(
            "LIVE_FEED_DEFAULT_SYMBOLS",
            "NIFTY,BANKNIFTY,FINNIFTY,SENSEX,RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK,SBIN",
        )
    ))
    live_feed_max_symbols: int = field(default_factory=lambda: max(1, _env_int("LIVE_FEED_MAX_SYMBOLS", 80)))

    # Phase 1-9 feature flags
    enable_ai_council: bool = field(default_factory=lambda: _bool_env("ENABLE_AI_COUNCIL", True))
    enable_neural_lab: bool = field(default_factory=lambda: _bool_env("ENABLE_NEURAL_LAB", True))
    enable_marl_lab: bool = field(default_factory=lambda: _bool_env("ENABLE_MARL_LAB", True))
    enable_goal_governor: bool = field(default_factory=lambda: _bool_env("ENABLE_GOAL_GOVERNOR", True))
    local_llm_gateway: str = field(default_factory=lambda: _env("LOCAL_LLM_GATEWAY", "disabled"))
    local_llm_coordinator_model: str = field(default_factory=lambda: _env("LOCAL_LLM_COORDINATOR_MODEL", "gemma4-26b-moe"))
    local_llm_fast_model: str = field(default_factory=lambda: _env("LOCAL_LLM_FAST_MODEL", "gemma4-e4b"))
    local_llm_sentiment_model: str = field(default_factory=lambda: _env(
        "LOCAL_LLM_SENTIMENT_MODEL", "llama-3-8b-instruct-finance-rag"
    ))
    local_llm_max_output_tokens: int = field(default_factory=lambda: _env_int("LOCAL_LLM_MAX_OUTPUT_TOKENS", 2048))

    enable_runtime_monitor: bool = field(default_factory=lambda: _bool_env("ENABLE_RUNTIME_MONITOR", True))
    runtime_monitor_interval_seconds: int = field(default_factory=lambda: max(
        60, _env_int("RUNTIME_MONITOR_INTERVAL_SECONDS", 900)
    ))
    enable_news_feed: bool = field(default_factory=lambda: _bool_env("ENABLE_NEWS_FEED", True))
    news_fetch_interval_seconds: int = field(default_factory=lambda: max(
        60, _env_int("NEWS_FETCH_INTERVAL_SECONDS", 300)
    ))
    enable_portfolio_guardian: bool = field(default_factory=lambda: _bool_env("ENABLE_PORTFOLIO_GUARDIAN", True))
    portfolio_guardian_interval_seconds: int = field(default_factory=lambda: max(
        15, _env_int("PORTFOLIO_GUARDIAN_INTERVAL_SECONDS", 60)
    ))
    enable_reconciliation: bool = field(default_factory=lambda: _bool_env("ENABLE_RECONCILIATION", True))
    reconciliation_interval_seconds: int = field(default_factory=lambda: max(
        10, _env_int("RECONCILIATION_INTERVAL_SECONDS", 30)
    ))
    enable_gamma_exposure_gate: bool = field(default_factory=lambda: _bool_env("ENABLE_GAMMA_EXPOSURE_GATE", False))

    # Market-hours option-chain snapshots -> per-underlying ATM IV history
    # (REDESIGN §3/§4.2). OptionsChainCollector.capture() already persisted this
    # to CSV and exposed atm_iv_history(), but nothing ever ran it on a schedule
    # — only 7 ad-hoc capture dates existed as of 2026-08-08. This history is
    # the ONLY way to get per-index implied vol: India VIX is NIFTY-based, so
    # using it for BANKNIFTY/FINNIFTY (which realize ~24% vs NIFTY's ~18%)
    # prices those structures at a vol they never trade at. It cannot be
    # back-filled from any free source, so it exists only if recorded from now
    # on. Default 300s ~= 78 snapshots/day, comfortably inside Angel One's
    # candle throttle given the collector serialises its fetches.
    # Transaction Cost Analysis (REDESIGN §6.4) — scores every fill against the
    # mark at submission. Execution quality is routinely 10-20bps/trade, which
    # on a 4-leg condor (8 spread crossings per round trip) rivals the whole
    # strategy's annual edge — and unlike a prediction signal, a basis point
    # saved cannot fail to generalise. Read-only measurement; it does not alter
    # routing. Default ON: it only observes.
    enable_tca: bool = field(default_factory=lambda: _bool_env("ENABLE_TCA", True))

    enable_chain_capture: bool = field(default_factory=lambda: _bool_env("ENABLE_CHAIN_CAPTURE", True))
    chain_capture_interval_seconds: int = field(default_factory=lambda: max(
        60, _env_int("CHAIN_CAPTURE_INTERVAL_SECONDS", 300)
    ))
    chain_capture_underlyings: tuple[str, ...] = field(default_factory=lambda: _parse_csv_tuple(
        os.getenv("CHAIN_CAPTURE_UNDERLYINGS", "NIFTY,BANKNIFTY,FINNIFTY")
    ))

    # Goal governor — see GoalGovernance (goal/governance.py); annual_target_pct
    # is the one that actually scales live short-vol position sizing.
    yearly_profit_target: float = field(default_factory=lambda: _env_float("YEARLY_PROFIT_TARGET", 50_000_000.0))
    annual_target_pct: float = field(default_factory=lambda: _env_float("ANNUAL_TARGET_PCT", 0.35))

    # Database (empty = SQLite fallback; set DATABASE_URL for PostgreSQL) —
    # distinct from the newer POSTGRES_*/TIMESCALE_* fields below, which are
    # for the separate Timescale hypertable store, not this OLTP connection.
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", ""))

    # ── Existing: Angel One (preserved) ──────────────────────
    ANGEL_ONE_API_KEY: str = field(default_factory=lambda: _env("ANGEL_ONE_API_KEY", ""))
    ANGEL_ONE_FEED_API_KEY: str = field(default_factory=lambda: _env("ANGEL_ONE_FEED_API_KEY", ""))
    ANGEL_ONE_ACCESS_TOKEN: str = field(default_factory=lambda: _env("ANGEL_ONE_ACCESS_TOKEN", ""))
    ANGEL_ONE_CLIENT_LOG_ID: str = field(default_factory=lambda: _env("ANGEL_ONE_CLIENT_LOG_ID", ""))
    ANGEL_ONE_PARENT_TOKEN: str = field(default_factory=lambda: _env("ANGEL_ONE_PARENT_TOKEN", ""))
    ANGEL_ONE_TOKEN: str = field(default_factory=lambda: _env("ANGEL_ONE_TOKEN", ""))
    ANGEL_ONE_OTP: str = field(default_factory=lambda: _env("ANGEL_ONE_OTP", ""))
    ANGEL_ONE_REFRESH_TOKEN: str = field(default_factory=lambda: _env("ANGEL_ONE_REFRESH_TOKEN", ""))
    ANGEL_ONE_SMART_API_TOKEN: str = field(default_factory=lambda: _env("ANGEL_ONE_SMART_API_TOKEN", ""))

    # Broker base URL
    ANGEL_ONE_BASE_URL: str = field(default_factory=lambda: _env("ANGEL_ONE_BASE_URL", "https://margincalculator.angelbroking.com"))
    ANGEL_ONE_HISTORICAL_API: str = field(default_factory=lambda: _env(
        "ANGEL_ONE_HISTORICAL_API",
        "https://margincalculator.angelbroking.com/OnlineService/API/HistoryChart",
    ))

    # TrueData was evaluated (REDESIGN §3.0) and abandoned: trial credentials
    # never authenticated against the live API (see memory
    # truedata-credentials-rejected). Do not re-add TRUEDATA_* settings
    # without a new, verified vendor account.

    # ── NEW (REDESIGN §3): Upstox adapter ────────────────────
    UPSTOX_API_KEY: str = field(default_factory=lambda: _env("UPSTOX_API_KEY", ""))
    UPSTOX_REDIRECT_URL: str = field(default_factory=lambda: _env("UPSTOX_REDIRECT_URL", "http://localhost:8000/auth/upstox/callback"))
    UPSTOX_CLIENT_SECRET: str = field(default_factory=lambda: _env("UPSTOX_CLIENT_SECRET", ""))
    UPSTOX_ENABLED: bool = field(default_factory=lambda: _env_bool("UPSTOX_ENABLED", False))

    # ── NEW (REDESIGN §3): Dhan adapter ──────────────────────
    DHAN_ACCESS_TOKEN: str = field(default_factory=lambda: _env("DHAN_ACCESS_TOKEN", ""))
    DHAN_API_HOST: str = field(default_factory=lambda: _env("DHAN_API_HOST", "https://api.dhan.co"))
    DHAN_SOCKET_HOST: str = field(default_factory=lambda: _env("DHAN_SOCKET_HOST", "wss://ws.dhan.co"))
    DHAN_ENABLED: bool = field(default_factory=lambda: _env_bool("DHAN_ENABLED", False))

    # ── NEW (REDESIGN §3): MarketDataAdapter config ──────────
    MARKET_DATA_SOURCE: str = field(default_factory=lambda: _env("MARKET_DATA_SOURCE", "angel_one"))
    # Max sharded WebSocket connections (Angel One: 3)
    MAX_WS_CONNECTIONS: int = field(default_factory=lambda: _env_int("MAX_WS_CONNECTIONS", 3))
    TOKENS_PER_SOCKET: int = field(default_factory=lambda: _env_int("TOKENS_PER_SOCKET", 1000))
    # Staleness thresholds
    STALENESS_THRESHOLD_SECONDS: float = field(default_factory=lambda: _env_float("STALENESS_THRESHOLD_SECONDS", 10.0))
    # Option chain collection interval (seconds)
    CHAIN_SNAPSHOT_INTERVAL_SECONDS: int = field(default_factory=lambda: _env_int("CHAIN_SNAPSHOT_INTERVAL_SECONDS", 30))
    # Strikes band around spot (±percent)
    STRIKE_BAND_PERCENT: float = field(default_factory=lambda: _env_float("STRIKE_BAND_PERCENT", 10.0))

    # ── Existing: Redis ──────────────────────────────────────
    REDIS_HOST: str = field(default_factory=lambda: _env("REDIS_HOST", "localhost"))
    REDIS_PORT: int = field(default_factory=lambda: _env_int("REDIS_PORT", 6379))
    REDIS_DB: int = field(default_factory=lambda: _env_int("REDIS_DB", 0))
    REDIS_PASSWORD: str = field(default_factory=lambda: _env("REDIS_PASSWORD", ""))

    # ── NEW (REDESIGN §3): Redis Streams topics ──────────────
    TICK_STREAM_TOPIC: str = field(default_factory=lambda: _env("TICK_STREAM_TOPIC", "tick.*"))
    SIGNAL_STREAM_TOPIC: str = field(default_factory=lambda: _env("SIGNAL_STREAM_TOPIC", "signal.*"))
    ORDER_STREAM_TOPIC: str = field(default_factory=lambda: _env("ORDER_STREAM_TOPIC", "order.*"))
    FILL_STREAM_TOPIC: str = field(default_factory=lambda: _env("FILL_STREAM_TOPIC", "fill.*"))
    RISK_STREAM_TOPIC: str = field(default_factory=lambda: _env("RISK_STREAM_TOPIC", "risk.*"))

    # ── NEW (REDESIGN §7): Postgres / TimescaleDB ────────────
    POSTGRES_HOST: str = field(default_factory=lambda: _env("POSTGRES_HOST", "localhost"))
    POSTGRES_PORT: int = field(default_factory=lambda: _env_int("POSTGRES_PORT", 5432))
    POSTGRES_USER: str = field(default_factory=lambda: _env("POSTGRES_USER", "trader"))
    POSTGRES_PASSWORD: str = field(default_factory=lambda: _env("POSTGRES_PASSWORD", "trader_password"))
    POSTGRES_DB: str = field(default_factory=lambda: _env("POSTGRES_DB", "trading"))
    POSTGRES_SCHEMA: str = field(default_factory=lambda: _env("POSTGRES_SCHEMA", "trading"))

    # TimescaleDB connection
    TIMESCALE_HOST: str = field(default_factory=lambda: _env("TIMESCALE_HOST", "localhost"))
    TIMESCALE_PORT: int = field(default_factory=lambda: _env_int("TIMESCALE_PORT", 5432))
    TIMESCALE_USER: str = field(default_factory=lambda: _env("TIMESCALE_USER", "trader"))
    TIMESCALE_PASSWORD: str = field(default_factory=lambda: _env("TIMESCALE_PASSWORD", "trader_password"))
    TIMESCALE_DB: str = field(default_factory=lambda: _env("TIMESCALE_DB", "trading_ts"))

    # ── Qdrant vector DB — backs VectorMemoryStore's persistence (2026-08-29;
    # previously wired to nothing despite this flag existing since REDESIGN §8).
    # Defaults True: connection is fully best-effort (VectorMemoryStore never
    # raises if Qdrant isn't reachable — see agents/vector_memory.py's
    # _init_qdrant()), so defaulting on costs nothing when the qdrant service
    # isn't running (a plain `docker compose up` without `--profile research`
    # doesn't start it) and buys persistence across restarts when it is.
    # "127.0.0.1", not "localhost": measured 2026-08-29 on this Windows dev
    # box — resolving the hostname "localhost" added a consistent ~1s to
    # EVERY qdrant-client call (dual-stack IPv6/IPv4 resolution delay), vs
    # 0.016s through the literal IP. Real server latency is ~6ms either way;
    # this is pure client-side DNS overhead, confirmed by timing the same
    # call repeatedly against the same open connection. Docker-compose
    # overrides this to the `qdrant` service name for container networking,
    # which isn't subject to this specific host-OS resolver behavior.
    QDRANT_HOST: str = field(default_factory=lambda: _env("QDRANT_HOST", "127.0.0.1"))
    QDRANT_PORT: int = field(default_factory=lambda: _env_int("QDRANT_PORT", 6333))
    QDRANT_GRPC_PORT: int = field(default_factory=lambda: _env_int("QDRANT_GRPC_PORT", 6334))
    QDRANT_ENABLED: bool = field(default_factory=lambda: _env_bool("QDRANT_ENABLED", True))

    # ── Existing: Local LLM (LM Studio) ──────────────────────
    LOCAL_LLM_RUNTIME: str = field(default_factory=lambda: _env("LOCAL_LLM_RUNTIME", "stub"))
    LOCAL_LLM_BASE_URL: str = field(default_factory=lambda: _env(
        "LOCAL_LLM_BASE_URL", "http://localhost:1234/v1"
    ))
    LOCAL_LLM_API_KEY: str = field(default_factory=lambda: _env("LOCAL_LLM_API_KEY", "dummy"))
    LOCAL_LLM_PRIMARY_MODEL: str = field(default_factory=lambda: _env("LOCAL_LLM_PRIMARY_MODEL", "qwen3-14b"))
    LOCAL_LLM_SECONDARY_MODEL: str = field(default_factory=lambda: _env("LOCAL_LLM_SECONDARY_MODEL", "qwen3-72b"))
    LOCAL_LLM_EMBEDDING_MODEL: str = field(default_factory=lambda: _env("LOCAL_LLM_EMBEDDING_MODEL", "nomic-embed-text-v1.5"))
    LOCAL_LLM_RERANKER_MODEL: str = field(default_factory=lambda: _env("LOCAL_LLM_RERANKER_MODEL", "bge-reranker-v2-m3"))
    LOCAL_LLM_MAX_CONCURRENT_CALLS: int = field(default_factory=lambda: _env_int("LOCAL_LLM_MAX_CONCURRENT_CALLS", 1))
    LOCAL_LLM_TIMEOUT_SECONDS: int = field(default_factory=lambda: _env_int("LOCAL_LLM_TIMEOUT_SECONDS", 30))

    # ── NEW (REDESIGN §8): Deep model timeout ────────────────
    LOCAL_LLM_DEEP_TIMEOUT_SECONDS: int = field(default_factory=lambda: _env_int("LOCAL_LLM_DEEP_TIMEOUT_SECONDS", 60))
    LOCAL_LLM_FAST_TIMEOUT_SECONDS: int = field(default_factory=lambda: _env_int("LOCAL_LLM_FAST_TIMEOUT_SECONDS", 15))

    # ── Existing: Risk / trading params ──────────────────────
    RISK_ENGINE_ENABLED: bool = field(default_factory=lambda: _env_bool("RISK_ENGINE_ENABLED", True))
    DAILY_PNL_STOP_LOSS: float = field(default_factory=lambda: _env_float("DAILY_PNL_STOP_LOSS", 50000.0))
    MAX_DRAWDOWN_PERCENT: float = field(default_factory=lambda: _env_float("MAX_DRAWDOWN_PERCENT", 10.0))
    KILL_SWITCH_ENABLED: bool = field(default_factory=lambda: _env_bool("KILL_SWITCH_ENABLED", True))
    MARGIN_CEILING: float = field(default_factory=lambda: _env_float("MARGIN_CEILING", 1000000.0))
    MAX_OPEN_POSITIONS: int = field(default_factory=lambda: _env_int("MAX_OPEN_POSITIONS", 10))
    STRATEGY_ENABLED: str = field(default_factory=lambda: _env("STRATEGY_ENABLED", "short_vol"))
    APPROVAL_REQUIRED: bool = field(default_factory=lambda: _env_bool("APPROVAL_REQUIRED", False))
    MAX_ORDER_RATE: int = field(default_factory=lambda: _env_int("MAX_ORDER_RATE", 5))
    ORDER_RATE_WINDOW_SECONDS: int = field(default_factory=lambda: _env_int("ORDER_RATE_WINDOW_SECONDS", 60))
    MAX_DAILY_ORDERS: int = field(default_factory=lambda: _env_int("MAX_DAILY_ORDERS", 200))

    # ── Existing: Exit management ────────────────────────────
    EXIT_MANAGEMENT_ENABLED: bool = field(default_factory=lambda: _env_bool("EXIT_MANAGEMENT_ENABLED", True))
    PROFIT_TAKING_ENABLED: bool = field(default_factory=lambda: _env_bool("PROFIT_TAKING_ENABLED", True))
    STOP_LOSS_ENABLED: bool = field(default_factory=lambda: _env_bool("STOP_LOSS_ENABLED", True))
    TRAILING_SL_ENABLED: bool = field(default_factory=lambda: _env_bool("TRAILING_SL_ENABLED", True))
    NIGHT_HOLDING_ENABLED: bool = field(default_factory=lambda: _env_bool("NIGHT_HOLDING_ENABLED", False))
    EMERGENCY_SQUARE_OFF_ENABLED: bool = field(default_factory=lambda: _env_bool("EMERGENCY_SQUARE_OFF_ENABLED", False))
    SQUARE_OFF_MIN_PROFIT: float = field(default_factory=lambda: _env_float("SQUARE_OFF_MIN_PROFIT", 0.0))

    # ── Existing: Portfolio guardian ─────────────────────────
    PORTFOLIO_GUARDIAN_ENABLED: bool = field(default_factory=lambda: _env_bool("PORTFOLIO_GUARDIAN_ENABLED", True))
    PORTFOLIO_MAX_DRAWDOWN: float = field(default_factory=lambda: _env_float("PORTFOLIO_MAX_DRAWDOWN", 150000.0))
    PORTFOLIO_DAILY_DRAWDOWN: float = field(default_factory=lambda: _env_float("PORTFOLIO_DAILY_DRAWDOWN", 50000.0))
    PORTFOLIO_MIN_CASH: float = field(default_factory=lambda: _env_float("PORTFOLIO_MIN_CASH", 50000.0))

    # ── Existing: Neural lab ─────────────────────────────────
    NEURAL_FORECAST_ENABLED: bool = field(default_factory=lambda: _env_bool("NEURAL_FORECAST_ENABLED", False))
    NEURAL_LATENCY_TOLERANCE: float = field(default_factory=lambda: _env_float("NEURAL_LATENCY_TOLERANCE", 0.5))
    NEURAL_MARKUP_THRESHOLD: float = field(default_factory=lambda: _env_float("NEURAL_MARKUP_THRESHOLD", 0.05))
    VOL_FORECAST_ENABLED: bool = field(default_factory=lambda: _env_bool("VOL_FORECAST_ENABLED", True))
    VOL_FORECAST_MODEL_PATH: str = field(default_factory=lambda: _env("VOL_FORECAST_MODEL_PATH", "models/vol_forecaster.joblib"))
    VOL_FORECAST_LOOKBACK: int = field(default_factory=lambda: _env_int("VOL_FORECAST_LOOKBACK", 60))
    VOL_FORECAST_WINDOW: int = field(default_factory=lambda: _env_int("VOL_FORECAST_WINDOW", 15))

    # ── Existing: Meta-labeling ──────────────────────────────
    META_LABELER_ENABLED: bool = field(default_factory=lambda: _env_bool("META_LABELER_ENABLED", False))
    META_LABELER_MODEL_PATH: str = field(default_factory=lambda: _env("META_LABELER_MODEL_PATH", "models/meta_labeler.joblib"))
    META_CONVICTION_THRESHOLD: float = field(default_factory=lambda: _env_float("META_CONVICTION_THRESHOLD", 0.6))
    META_ABSTAIN_THRESHOLD: float = field(default_factory=lambda: _env_float("META_ABSTAIN_THRESHOLD", 0.55))

    # ── Existing: Regime detection ───────────────────────────
    REGIME_DETECTION_ENABLED: bool = field(default_factory=lambda: _env_bool("REGIME_DETECTION_ENABLED", False))
    REGIME_MODEL_PATH: str = field(default_factory=lambda: _env("REGIME_MODEL_PATH", "models/regime_hmm.pkl"))
    REGIME_N_STATES: int = field(default_factory=lambda: _env_int("REGIME_N_STATES", 3))
    REGIME_UPDATE_INTERVAL: int = field(default_factory=lambda: _env_int("REGIME_UPDATE_INTERVAL", 30))

    # ── Existing: Backtesting ────────────────────────────────
    BACKTEST_ENABLED: bool = field(default_factory=lambda: _env_bool("BACKTEST_ENABLED", True))
    BACKTEST_START_DATE: str = field(default_factory=lambda: _env("BACKTEST_START_DATE", "2024-01-01"))
    BACKTEST_END_DATE: str = field(default_factory=lambda: _env("BACKTEST_END_DATE", "2024-12-31"))
    BACKTEST_INITIAL_CAPITAL: float = field(default_factory=lambda: _env_float("BACKTEST_INITIAL_CAPITAL", 1000000.0))
    BACKTEST_SLIPPAGE_PCT: float = field(default_factory=lambda: _env_float("BACKTEST_SLIPPAGE_PCT", 0.05))
    BACKTEST_BROKERAGE_PCT: float = field(default_factory=lambda: _env_float("BACKTEST_BROKERAGE_PCT", 0.001))
    BACKTEST_STT_PCT: float = field(default_factory=lambda: _env_float("BACKTEST_STT_PCT", 0.01))
    BACKTEST_GST_PCT: float = field(default_factory=lambda: _env_float("BACKTEST_GST_PCT", 18.0))
    BACKTEST_EXCHANGE_TXN_PCT: float = field(default_factory=lambda: _env_float("BACKTEST_EXCHANGE_TXN_PCT", 0.0029))
    BACKTEST_STAMP_DUTY_PCT: float = field(default_factory=lambda: _env_float("BACKTEST_STAMP_DUTY_PCT", 0.0025))
    BACKTEST_SEBI_FEE_PCT: float = field(default_factory=lambda: _env_float("BACKTEST_SEBI_FEE_PCT", 0.00001))

    # ── NEW (REDESIGN §5): Validation gates ─────────────────
    WALK_FORWARD_ENABLED: bool = field(default_factory=lambda: _env_bool("WALK_FORWARD_ENABLED", True))
    CPCV_FOLDS: int = field(default_factory=lambda: _env_int("CPCV_FOLDS", 10))
    EMBARGO_BARS: int = field(default_factory=lambda: _env_int("EMBARGO_BARS", 5))
    DEFLECTED_SHARPE_MIN: float = field(default_factory=lambda: _env_float("DEFLECTED_SHARPE_MIN", 0.5))
    PBO_MAX: float = field(default_factory=lambda: _env_float("PBO_MAX", 0.4))
    MC_SHUFFLE_RUNS: int = field(default_factory=lambda: _env_int("MC_SHUFFLE_RUNS", 1000))
    PROMOTION_PAPER_DAYS: int = field(default_factory=lambda: _env_int("PROMOTION_PAPER_DAYS", 30))
    MIN_WALKFORWARD_SHARPE: float = field(default_factory=lambda: _env_float("MIN_WALKFORWARD_SHARPE", 0.3))
    GATE_MAX_DRAWDOWN: float = field(default_factory=lambda: _env_float("GATE_MAX_DRAWDOWN", 0.15))
    MIN_NET_COST_RATIO: float = field(default_factory=lambda: _env_float("MIN_NET_COST_RATIO", 0.6))
    CPCV_N_TEST_GROUPS: int = field(default_factory=lambda: _env_int("CPCV_N_TEST_GROUPS", 2))
    CSCV_N_GROUPS: int = field(default_factory=lambda: _env_int("CSCV_N_GROUPS", 8))
    # CPCV ships correct+tested but is NOT yet wired into neural/return_forecaster.py's
    # live accept/reject gate — enabling that is a deliberate follow-up decision.
    CPCV_ENABLED: bool = field(default_factory=lambda: _env_bool("CPCV_ENABLED", False))

    # ── Existing: ML / model paths ───────────────────────────
    MODEL_REGISTRY_PATH: str = field(default_factory=lambda: _env("MODEL_REGISTRY_PATH", "models/registry"))
    MODEL_PROMOTION_THRESHOLD: float = field(default_factory=lambda: _env_float("MODEL_PROMOTION_THRESHOLD", 0.55))
    MODEL_MIN_LIVE_DAYS: int = field(default_factory=lambda: _env_int("MODEL_MIN_LIVE_DAYS", 5))
    MODEL_MIN_SAMPLES: int = field(default_factory=lambda: _env_int("MODEL_MIN_SAMPLES", 300))
    MODEL_MAX_AGE_DAYS: int = field(default_factory=lambda: _env_int("MODEL_MAX_AGE_DAYS", 90))
    MODEL_HEALTH_CHECK_INTERVAL: int = field(default_factory=lambda: _env_int("MODEL_HEALTH_CHECK_INTERVAL", 3600))
    MODEL_FEATURE_DRIFT_THRESHOLD: float = field(default_factory=lambda: _env_float("MODEL_FEATURE_DRIFT_THRESHOLD", 0.1))
    MODEL_SLIPPAGE_DELTA_THRESHOLD: float = field(default_factory=lambda: _env_float("MODEL_SLIPPAGE_DELTA_THRESHOLD", 0.03))

    # ── NEW (REDESIGN §8): RAG / embedding config ────────────
    RAG_ENABLED: bool = field(default_factory=lambda: _env_bool("RAG_ENABLED", False))
    RAG_TOP_K: int = field(default_factory=lambda: _env_int("RAG_TOP_K", 10))
    RAG_RERANK_TOP_K: int = field(default_factory=lambda: _env_int("RAG_RERANK_TOP_K", 5))
    RAG_HYBRID_WEIGHT: float = field(default_factory=lambda: _env_float("RAG_HYBRID_WEIGHT", 0.5))
    RAG_CONTEXT_BLURB_LENGTH: int = field(default_factory=lambda: _env_int("RAG_CONTEXT_BLURB_LENGTH", 200))
    RAG_FRESHNESS_WINDOW_DAYS: int = field(default_factory=lambda: _env_int("RAG_FRESHNESS_WINDOW_DAYS", 7))
    RAG_MAX_QUERY_DEPTH: int = field(default_factory=lambda: _env_int("RAG_MAX_QUERY_DEPTH", 3))

    # ── NEW (REDESIGN §8): Agent config ──────────────────────
    AGENT_DEEP_MODEL: str = field(default_factory=lambda: _env("AGENT_DEEP_MODEL", "qwen3-72b"))
    AGENT_FAST_MODEL: str = field(default_factory=lambda: _env("AGENT_FAST_MODEL", "qwen3-14b"))
    AGENT_MAX_CALLS_PER_DAY: int = field(default_factory=lambda: _env_int("AGENT_MAX_CALLS_PER_DAY", 200))
    AGENT_REFLECTION_ENABLED: bool = field(default_factory=lambda: _env_bool("AGENT_REFLECTION_ENABLED", True))
    AGENT_TOOL_USE_ENABLED: bool = field(default_factory=lambda: _env_bool("AGENT_TOOL_USE_ENABLED", True))

    # ── Existing: News ───────────────────────────────────────
    NEWS_RSS_FEEDS: str = field(default_factory=lambda: _env(
        "NEWS_RSS_FEEDS",
        "https://www.moneycontrol.com/feeds/market_feed.json,"
        "https://feeds.economictimes.indiatimes.com/rss&xml=0,feeds/rss/marketfeeds.xml",
    ))
    NEWS_MIN_SENTIMENT_ABS: float = field(default_factory=lambda: _env_float("NEWS_MIN_SENTIMENT_ABS", 0.3))
    NEWS_CACHE_TTL: int = field(default_factory=lambda: _env_int("NEWS_CACHE_TTL", 3600))
    NEWS_CALENDAR_PATH: str = field(default_factory=lambda: _env("NEWS_CALENDAR_PATH", "data/news/calendar.json"))

    # ── Existing: AI council ─────────────────────────────────
    AI_COUNCIL_ENABLED: bool = field(default_factory=lambda: _env_bool("AI_COUNCIL_ENABLED", False))
    AI_COUNCIL_QUORUM: int = field(default_factory=lambda: _env_int("AI_COUNCIL_QUORUM", 4))
    AI_COUNCIL_CONVICTION_THRESHOLD: float = field(default_factory=lambda: _env_float(
        "AI_COUNCIL_CONVICTION_THRESHOLD", 0.55
    ))
    AI_COUNCIL_VETO_THRESHOLD: float = field(default_factory=lambda: _env_float(
        "AI_COUNCIL_VETO_THRESHOLD", 0.7
    ))
    AI_COUNCIL_CONSENSUS_THRESHOLD: float = field(default_factory=lambda: _env_float(
        "AI_COUNCIL_CONSENSUS_THRESHOLD", 0.65
    ))
    AI_COUNCIL_MAX_LATENCY_MS: int = field(default_factory=lambda: _env_int("AI_COUNCIL_MAX_LATENCY_MS", 5000))

    # ── Existing: Orchestration ──────────────────────────────
    CYCLE_INTERVAL_SECONDS: int = field(default_factory=lambda: _env_int("CYCLE_INTERVAL_SECONDS", 300))
    ORCHESTRATOR_ENABLED: bool = field(default_factory=lambda: _env_bool("ORCHESTRATOR_ENABLED", True))
    INTELLIGENCE_ENABLED: bool = field(default_factory=lambda: _env_bool("INTELLIGENCE_ENABLED", True))
    SPECIALIST_ENABLED: bool = field(default_factory=lambda: _env_bool("SPECIALIST_ENABLED", True))
    FORECAST_ENABLED: bool = field(default_factory=lambda: _env_bool("NEURAL_FORECAST_ENABLED", False))
    PORTFOLIO_ENABLED: bool = field(default_factory=lambda: _env_bool("PORTFOLIO_ENABLED", True))
    RISK_CRITIC_ENABLED: bool = field(default_factory=lambda: _env_bool("RISK_CRITIC_ENABLED", True))
    PROFIT_GUARD_ENABLED: bool = field(default_factory=lambda: _env_bool("PROFIT_GUARD_ENABLED", True))
    CONSENSUS_ENABLED: bool = field(default_factory=lambda: _env_bool("CONSENSUS_ENABLED", True))
    GOVERNOR_ENABLED: bool = field(default_factory=lambda: _env_bool("GOVERNOR_ENABLED", True))
    EXECUTION_PLAN_ENABLED: bool = field(default_factory=lambda: _env_bool("EXECUTION_PLAN_ENABLED", True))

    # ── Existing: Directional / short-vol auto ───────────────
    AGENT_DIRECTIONAL_ENABLED: bool = field(default_factory=lambda: _env_bool("AGENT_DIRECTIONAL_ENABLED", False))
    SHORTVOL_AUTO_ENABLED: bool = field(default_factory=lambda: _env_bool("SHORTVOL_AUTO_ENABLED", False))
    SHORTVOL_UNDERLYING: str = field(default_factory=lambda: _env("SHORTVOL_UNDERLYING", "NIFTY"))
    SHORTVOL_STRIKE_BAND: int = field(default_factory=lambda: _env_int("SHORTVOL_STRIKE_BAND", 15))
    SHORTVOL_MAX_CONTRACTS: int = field(default_factory=lambda: _env_int("SHORTVOL_MAX_CONTRACTS", 4))
    SHORTVOL_PROFIT_TARGET: float = field(default_factory=lambda: _env_float("SHORTVOL_PROFIT_TARGET", 20000.0))
    SHORTVOL_STOP_LOSS: float = field(default_factory=lambda: _env_float("SHORTVOL_STOP_LOSS", 10000.0))
    SHORTVOL_EXPIRY_DAY_SQUARE_OFF_HOUR: int = field(default_factory=lambda: _env_int(
        "SHORTVOL_EXPIRY_DAY_SQUARE_OFF_HOUR", 13
    ))
    SHORTVOL_IV_THRESHOLD: float = field(default_factory=lambda: _env_float("SHORTVOL_IV_THRESHOLD", 18.0))
    SHORTVOL_MIN_CREDIT: float = field(default_factory=lambda: _env_float("SHORTVOL_MIN_CREDIT", 100.0))
    SHORTVOL_DELTA_BAND: float = field(default_factory=lambda: _env_float("SHORTVOL_DELTA_BAND", 0.3))
    SHORTVOL_AUTO_MARGIN_CAP: float = field(default_factory=lambda: _env_float("SHORTVOL_AUTO_MARGIN_CAP", 500000.0))

    # ── Existing: Swing template ─────────────────────────────
    SWING_ENABLED: bool = field(default_factory=lambda: _env_bool("SWING_ENABLED", False))
    SWING_UNIVERSE: str = field(default_factory=lambda: _env("SWING_UNIVERSE", "NIFTY_LIQUID_STOCKS"))
    SWING_HORIZON: str = field(default_factory=lambda: _env("SWING_HORIZON", "1-5"))
    SWING_MAX_POSITIONS: int = field(default_factory=lambda: _env_int("SWING_MAX_POSITIONS", 5))
    SWING_STOP_PCT: float = field(default_factory=lambda: _env_float("SWING_STOP_PCT", 3.0))
    SWING_TARGET_PCT: float = field(default_factory=lambda: _env_float("SWING_TARGET_PCT", 6.0))
    SWING_MIN_CONVICTION: float = field(default_factory=lambda: _env_float("SWING_MIN_CONVICTION", 0.55))
    SWING_MAX_PORTFOLIO_WEIGHT: float = field(default_factory=lambda: _env_float("SWING_MAX_PORTFOLIO_WEIGHT", 0.3))

    # ── Existing: Risk guard parameters ──────────────────────
    RISK_GUARD_MARGIN_CAP_PCT: float = field(default_factory=lambda: _env_float("RISK_GUARD_MARGIN_CAP_PCT", 80.0))
    RISK_GUARD_DRAWDOWN_LIMIT: float = field(default_factory=lambda: _env_float("RISK_GUARD_DRAWDOWN_LIMIT", 150000.0))
    RISK_GUARD_DAILY_LOSS_LIMIT: float = field(default_factory=lambda: _env_float("RISK_GUARD_DAILY_LOSS_LIMIT", 50000.0))
    RISK_GUARD_KILL_SWITCH: bool = field(default_factory=lambda: _env_bool("RISK_GUARD_KILL_SWITCH", True))
    RISK_GUARD_NAKED_OPTION_BAN: bool = field(default_factory=lambda: _env_bool("RISK_GUARD_NAKED_OPTION_BAN", True))
    RISK_GUARD_GAMMA_CUTOFF_HOURS: float = field(default_factory=lambda: _env_float("RISK_GUARD_GAMMA_CUTOFF_HOURS", 24.0))
    RISK_GUARD_EXPIRY_CUTOFF_DAYS: int = field(default_factory=lambda: _env_int("RISK_GUARD_EXPIRY_CUTOFF_DAYS", 2))

    # ── Existing: Governance ─────────────────────────────────
    GOVERNANCE_ENABLED: bool = field(default_factory=lambda: _env_bool("GOVERNANCE_ENABLED", True))
    GOVERNANCE_RISK_APPROVAL_THRESHOLD: float = field(default_factory=lambda: _env_float(
        "GOVERNANCE_RISK_APPROVAL_THRESHOLD", 500000.0
    ))
    GOVERNANCE_LOG_DIR: str = field(default_factory=lambda: _env("GOVERNANCE_LOG_DIR", "data/governance"))

    # ── Existing: Monitoring ─────────────────────────────────
    MONITORING_ENABLED: bool = field(default_factory=lambda: _env_bool("MONITORING_ENABLED", True))
    MONITORING_TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: _env("MONITORING_TELEGRAM_BOT_TOKEN", ""))
    MONITORING_TELEGRAM_CHAT_ID: str = field(default_factory=lambda: _env("MONITORING_TELEGRAM_CHAT_ID", ""))
    MONITORING_TELEGRAM_ENABLED: bool = field(default_factory=lambda: _env_bool("MONITORING_TELEGRAM_ENABLED", False))
    MONITORING_ALERT_LEVEL: str = field(default_factory=lambda: _env("MONITORING_ALERT_LEVEL", "INFO"))
    MONITORING_INTERVAL_SECONDS: int = field(default_factory=lambda: _env_int("MONITORING_INTERVAL_SECONDS", 60))
    MONITORING_DRAWDOWN_ALERT_PCT: float = field(default_factory=lambda: _env_float("MONITORING_DRAWDOWN_ALERT_PCT", 5.0))
    MONITORING_LAG_THRESHOLD_MS: int = field(default_factory=lambda: _env_int("MONITORING_LAG_THRESHOLD_MS", 5000))
    MONITORING_RECONCILIATION_THRESHOLD: float = field(default_factory=lambda: _env_float(
        "MONITORING_RECONCILIATION_THRESHOLD", 1000.0
    ))
    MONITORING_LLM_LATENCY_THRESHOLD_MS: int = field(default_factory=lambda: _env_int(
        "MONITORING_LLM_LATENCY_THRESHOLD_MS", 10000
    ))
    MONITORING_QUEUE_DEPTH_THRESHOLD: int = field(default_factory=lambda: _env_int(
        "MONITORING_QUEUE_DEPTH_THRESHOLD", 50
    ))

    # ── Existing: API / auth ─────────────────────────────────
    API_KEY: str = field(default_factory=lambda: _env("API_KEY", "trading_platform_secret_key"))
    API_AUTH_REQUIRED: bool = field(default_factory=lambda: _env_bool("API_AUTH_REQUIRED", False))
    API_HOST: str = field(default_factory=lambda: _env("API_HOST", "0.0.0.0"))
    API_PORT: int = field(default_factory=lambda: _env_int("API_PORT", 8000))
    API_DEBUG: bool = field(default_factory=lambda: _env_bool("API_DEBUG", False))

    # ── Existing: DB ─────────────────────────────────────────
    DB_HOST: str = field(default_factory=lambda: _env("DB_HOST", "localhost"))
    DB_PORT: int = field(default_factory=lambda: _env_int("DB_PORT", 5432))
    DB_USER: str = field(default_factory=lambda: _env("DB_USER", "trader"))
    DB_PASSWORD: str = field(default_factory=lambda: _env("DB_PASSWORD", "trader_password"))
    DB_NAME: str = field(default_factory=lambda: _env("DB_NAME", "trading"))
    DB_SCHEMA: str = field(default_factory=lambda: _env("DB_SCHEMA", "trading"))

    # ── Existing: Data / paths ───────────────────────────────
    DATA_DIR: str = field(default_factory=lambda: _env("DATA_DIR", "data"))
    RAW_DATA_DIR: str = field(default_factory=lambda: _env("RAW_DATA_DIR", "data/raw"))
    PROCESSED_DATA_DIR: str = field(default_factory=lambda: _env("PROCESSED_DATA_DIR", "data/processed"))
    FEATURE_STORE_PATH: str = field(default_factory=lambda: _env("FEATURE_STORE_PATH", "data/features"))
    MODEL_DIR: str = field(default_factory=lambda: _env("MODEL_DIR", "models"))
    AUDIT_LOG_DIR: str = field(default_factory=lambda: _env("AUDIT_LOG_DIR", "data/audit"))
    EXIT_PLAN_PATH: str = field(default_factory=lambda: _env("EXIT_PLAN_PATH", "data/exit_plan.json"))
    SIGNAL_HASH_DIR: str = field(default_factory=lambda: _env("SIGNAL_HASH_DIR", "data/signals"))
    DAILY_PNL_DIR: str = field(default_factory=lambda: _env("DAILY_PNL_DIR", "data/pnl"))
    DAILY_REPORT_PATH: str = field(default_factory=lambda: _env("DAILY_REPORT_PATH", "data/reports/daily.md"))
    MARKDOWN_TABLE_ENABLED: bool = field(default_factory=lambda: _env_bool("MARKDOWN_TABLE_ENABLED", False))

    # ── NEW (REDESIGN §10): Frontend ─────────────────────────
    FRONTEND_DIR: str = field(default_factory=lambda: _env("FRONTEND_DIR", "hft_frontend"))

    # ── NEW (REDESIGN §11): Deploy / backup ──────────────────
    BACKUP_DIR: str = field(default_factory=lambda: _env("BACKUP_DIR", "data/backups"))
    BACKUP_NIGHTLY_ENABLED: bool = field(default_factory=lambda: _env_bool("BACKUP_NIGHTLY_ENABLED", False))
    INSTRUMENT_SYNC_HOURLY_ENABLED: bool = field(default_factory=lambda: _env_bool("INSTRUMENT_SYNC_HOURLY_ENABLED", False))
    EOD_REPORT_ENABLED: bool = field(default_factory=lambda: _env_bool("EOD_REPORT_ENABLED", False))
    MODEL_DRIFT_CHECK_ENABLED: bool = field(default_factory=lambda: _env_bool("MODEL_DRIFT_CHECK_ENABLED", False))

    # ── NEW (REDESIGN §16): Multi-tenant ─────────────────────
    MULTI_TENANT_ENABLED: bool = field(default_factory=lambda: _env_bool("MULTI_TENANT_ENABLED", False))
    TENANT_RLS_ENABLED: bool = field(default_factory=lambda: _env_bool("TENANT_RLS_ENABLED", False))
    DEFAULT_TENANT_ID: str = field(default_factory=lambda: _env("DEFAULT_TENANT_ID", "tenant_default"))

    # ── NEW (REDESIGN §4.4): Strategy allocator ──────────────
    ALLOCATOR_ENABLED: bool = field(default_factory=lambda: _env_bool("ALLOCATOR_ENABLED", False))
    ALLOCATOR_REBALANCE_INTERVAL: int = field(default_factory=lambda: _env_int("ALLOCATOR_REBALANCE_INTERVAL", 3600))
    ALLOCATOR_MAX_GROSS_VOL_TARGET: float = field(default_factory=lambda: _env_float("ALLOCATOR_MAX_GROSS_VOL_TARGET", 0.15))
    ALLOCATOR_CORRELATION_HURDLE: float = field(default_factory=lambda: _env_float("ALLOCATOR_CORRELATION_HURDLE", 0.85))
    ALLOCATOR_MIN_PAPER_DAYS: int = field(default_factory=lambda: _env_int("ALLOCATOR_MIN_PAPER_DAYS", 30))

    # ── NEW (REDESIGN §4.4b): MLflow ─────────────────────────
    MLFLOW_TRACKING_URI: str = field(default_factory=lambda: _env("MLFLOW_TRACKING_URI", "http://localhost:5000"))
    MLFLOW_EXPERIMENT_NAME: str = field(default_factory=lambda: _env("MLFLOW_EXPERIMENT_NAME", "trading_platform"))
    MLFLOW_ENABLED: bool = field(default_factory=lambda: _env_bool("MLFLOW_ENABLED", False))

    # ── NEW (REDESIGN §4.4b): Optuna ─────────────────────────
    OPTUNA_STORAGE: str = field(default_factory=lambda: _env("OPTUNA_STORAGE", "sqlite:///data/optuna/optuna.db"))
    OPTUNA_ENABLED: bool = field(default_factory=lambda: _env_bool("OPTUNA_ENABLED", False))

    # ── NEW (REDESIGN §4.4): Evidently drift monitoring ─────
    EVIDENTLY_ENABLED: bool = field(default_factory=lambda: _env_bool("EVIDENTLY_ENABLED", False))
    EVIDENTLY_REPORT_DIR: str = field(default_factory=lambda: _env("EVIDENTLY_REPORT_DIR", "data/drift/reports"))
    EVIDENTLY_FEATURE_DRIFT_THRESHOLD: float = field(default_factory=lambda: _env_float(
        "EVIDENTLY_FEATURE_DRIFT_THRESHOLD", 0.1
    ))

    # ── NEW (REDESIGN §4.4a): Foundation models ──────────────
    CHRONOS_ENABLED: bool = field(default_factory=lambda: _env_bool("CHRONOS_ENABLED", False))
    TIMESFM_ENABLED: bool = field(default_factory=lambda: _env_bool("TIMESFM_ENABLED", False))
    KRONOS_ENABLED: bool = field(default_factory=lambda: _env_bool("KRONOS_ENABLED", False))

    # ── NEW (REDESIGN §6.4): Execution TCA ───────────────────
    TCA_ENABLED: bool = field(default_factory=lambda: _env_bool("TCA_ENABLED", False))
    TCA_REPORT_DIR: str = field(default_factory=lambda: _env("TCA_REPORT_DIR", "data/tca"))

    # ── NEW (REDESIGN §4.4): Polars / DuckDB research ───────
    DUCKDB_DATA_PATH: str = field(default_factory=lambda: _env("DUCKDB_DATA_PATH", "data/parquet"))

    # ── NEW (REDESIGN §16): Broker selection ─────────────────
    PRIMARY_BROKER: str = field(default_factory=lambda: _env("PRIMARY_BROKER", "angel_one"))
    SECONDARY_BROKER: str = field(default_factory=lambda: _env("SECONDARY_BROKER", "dhan"))

    # ── Properties ───────────────────────────────────────────

    @property
    def nse_session(self) -> str:
        """Current NSE session badge."""
        return _nse_session()

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def timescale_dsn(self) -> str:
        return (
            f"postgresql://{self.TIMESCALE_USER}:{self.TIMESCALE_PASSWORD}"
            f"@{self.TIMESCALE_HOST}:{self.TIMESCALE_PORT}/{self.TIMESCALE_DB}"
        )

    @property
    def redis_url(self) -> str:
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def qdrant_url(self) -> str:
        return f"http://{self.QDRANT_HOST}:{self.QDRANT_PORT}"

    @property
    def is_configured(self) -> bool:
        """Is ANY broker credential set complete enough to trade?

        This used to check only the UPPER_SNAKE_CASE fields
        (`ANGEL_ONE_FEED_API_KEY` / `ACCESS_TOKEN` / `CLIENT_LOG_ID`), which
        belong to the not-yet-wired sharded-feed adapters and are normally
        unset. The live app authenticates with the lower_snake_case set
        (`angel_one_configured` below) — so on a perfectly well-configured
        install this returned False and `__post_init__` logged
        "No broker credentials configured. Platform will run in simulation
        mode." on every single startup and script run.

        That warning was FALSE and actively harmful: the credentials work
        (verified 2026-08-08 by pulling real candles and a real NIFTY spot),
        and a trading system that cries wolf on its credential check teaches
        operators to ignore the message that matters. Accept either credential
        set, since either genuinely enables trading.
        """
        has_angel_feed = bool(
            self.ANGEL_ONE_API_KEY
            and self.ANGEL_ONE_FEED_API_KEY
            and self.ANGEL_ONE_ACCESS_TOKEN
            and self.ANGEL_ONE_CLIENT_LOG_ID
        )
        has_dhan = bool(self.DHAN_ACCESS_TOKEN)
        return has_angel_feed or has_dhan or self.angel_one_configured

    @property
    def angel_one_configured(self) -> bool:
        """RESTORED — the lower_snake_case check the running app actually calls
        (api/runtime.py, agent/trading_agent.py, broker/angel_one.py, ...).
        Distinct from `is_configured` above, which checks the newer
        UPPER_SNAKE_CASE credential fields used by the sharded-feed adapters."""
        return all(
            [
                self.angel_one_api_key,
                self.angel_one_client_code,
                self.angel_one_pin,
                self.angel_one_totp_secret,
            ]
        )

    @property
    def can_submit_live_orders(self) -> bool:
        """RESTORED — the actual LIVE-order gate (CLAUDE.md Safety invariants:
        EXECUTION_MODE=LIVE + LIVE_TRADING_ENABLED=true + confirmation phrase +
        configured Angel One credentials, all four required)."""
        return (
            self.execution_mode.value.startswith("LIVE")
            and self.live_trading_enabled
            and self.live_order_confirmation == LIVE_ORDER_CONFIRMATION_PHRASE
            and self.angel_one_configured
        )

    def __post_init__(self) -> None:
        """Validate and derive settings after init."""
        # Ensure directories exist
        import pathlib
        for d in [
            self.RAW_DATA_DIR,
            self.PROCESSED_DATA_DIR,
            self.FEATURE_STORE_PATH,
            self.MODEL_DIR,
            self.AUDIT_LOG_DIR,
            self.SIGNAL_HASH_DIR,
            self.DAILY_PNL_DIR,
            self.BACKUP_DIR,
            self.GOVERNANCE_LOG_DIR,
        ]:
            pathlib.Path(d).mkdir(parents=True, exist_ok=True)

        # Validate broker config
        if not self.is_configured:
            logger.warning(
                "No broker credentials configured. Platform will run in simulation mode."
            )

        # Derive market_data_source priority (TrueData abandoned — see above)
        if self.UPSTOX_ENABLED:
            self._data_source_priority = ["upstox", "angel_one"]
        else:
            self._data_source_priority = ["angel_one"]

    @property
    def data_source_priority(self) -> List[str]:
        return self._data_source_priority

    def __getattr__(self, name: str) -> Any:
        """Case-bridge for lower_snake_case reads of the UPPER_SNAKE_CASE fields above.

        Several newer modules (angel_gateway, upstox_feed, tenancy/*, ...) read
        settings in lower_snake_case while every field on this dataclass is
        UPPER_SNAKE_CASE. Only called when normal attribute lookup (including
        the lowercase @property methods above) already failed, so it can't
        shadow anything real — it just avoids a repo-wide rename.
        """
        upper = name.upper()
        if upper != name and upper in self.__dataclass_fields__:
            return getattr(self, upper)
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")


# ──────────────────────────────────────────────
# RESTORED: load_settings() factory — the entry point api/app.py, api/auth.py,
# api/runtime.py, and most tests actually call (not the bare singleton below).
# Every Settings() field already re-reads its own env var via default_factory,
# so this wrapper's job is the side effects + fail-closed validation the
# original had: install secret redaction, load .env/.env.local, and refuse to
# start on an unsafe/nonsensical config rather than silently limping along.
# ──────────────────────────────────────────────


def load_settings() -> Settings:
    # Nothing in this app's startup path ever called logging.basicConfig(),
    # so the root logger had no handler and every note_swallowed()/logger.warning()
    # call from trading_platform code was invisible in `docker logs` regardless
    # of volume (confirmed 2026-08-31: 1197 swallowed exceptions counted in
    # /health, 0 corresponding WARNING lines in 34h of container logs) — only
    # uvicorn's own separately-configured "uvicorn.access" logger was visible.
    # basicConfig() is a documented no-op if a handler is already attached to
    # root, so this is safe to call from every load_settings() invocation
    # (including the many per-test calls) without duplicating handlers.
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    install_secret_redaction()
    load_local_env_files()

    initial_capital = float(os.getenv("INITIAL_CAPITAL", "1000000"))
    max_drawdown = float(os.getenv("MAX_DRAWDOWN", "0.10"))
    max_daily_loss = float(os.getenv("MAX_DAILY_LOSS", "0.02"))
    max_position_pct = float(os.getenv("MAX_POSITION_PCT", "0.05"))
    max_margin_utilization = float(os.getenv("MAX_MARGIN_UTILIZATION", "0.60"))

    if initial_capital <= 0:
        raise ValueError(f"INITIAL_CAPITAL must be > 0, got {initial_capital}")
    if not (0 < max_drawdown <= 1):
        raise ValueError(f"MAX_DRAWDOWN must be between 0 and 1, got {max_drawdown}")
    if not (0 < max_daily_loss <= max_drawdown):
        raise ValueError(f"MAX_DAILY_LOSS must be between 0 and MAX_DRAWDOWN ({max_drawdown}), got {max_daily_loss}")
    if not (0 < max_position_pct <= 1):
        raise ValueError(f"MAX_POSITION_PCT must be between 0 and 1, got {max_position_pct}")
    if not (0 < max_margin_utilization <= 1):
        raise ValueError(f"MAX_MARGIN_UTILIZATION must be between 0 and 1, got {max_margin_utilization}")

    # Default is False so tests and fresh deployments work without env setup.
    # Production should set API_AUTH_REQUIRED=true and API_AUTH_TOKEN explicitly.
    api_auth_required = _bool_env("API_AUTH_REQUIRED", False)
    api_auth_token = os.getenv("API_AUTH_TOKEN", "")
    if api_auth_required and not api_auth_token:
        raise ValueError("API_AUTH_REQUIRED=true but API_AUTH_TOKEN is empty — set a token or disable auth")

    # Every field re-reads its own env var (default_factory), so a bare
    # Settings() already reflects the environment validated above.
    return Settings()


# ──────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────

# Load .env BEFORE constructing the import-time singleton.
#
# Every field reads its env var via default_factory, so a Settings() built
# before .env has been read sees an empty environment — it then logs
# "No broker credentials configured. Platform will run in simulation mode."
# on EVERY import, even on a fully-configured install, because this line runs
# at import time while `load_local_env_files()` only ran inside
# `load_settings()`. The warning was pure noise (the real runtime calls
# load_settings() and gets correct values), but it trained operators to ignore
# a message that should mean something. Loading the env files first makes this
# singleton agree with load_settings().
load_local_env_files()

settings = Settings()