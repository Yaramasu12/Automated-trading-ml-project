from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from trading_platform.domain.enums import ExecutionMode
from trading_platform.logging_safety import install_secret_redaction

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


@dataclass(frozen=True)
class Settings:
    execution_mode: ExecutionMode
    broker: str
    live_trading_enabled: bool
    initial_capital: float
    max_drawdown: float
    max_daily_loss: float
    max_position_pct: float
    max_margin_utilization: float
    live_order_confirmation: str
    angel_one_api_key: str
    angel_one_api_secret: str
    angel_one_client_code: str
    angel_one_pin: str
    angel_one_totp_secret: str
    angel_one_instrument_master_url: str
    angel_one_instrument_cache_path: str
    aws_region: str
    api_auth_token: str = ""
    api_cors_origins: tuple[str, ...] = ()
    api_auth_required: bool = True
    auto_start_agent: bool = False
    auto_start_live_feed: bool = False
    auto_load_instrument_cache: bool = True
    auto_load_models: bool = False
    premarket_refresh_instruments: bool = False
    live_feed_default_symbols: tuple[str, ...] = ()
    live_feed_max_symbols: int = 80

    # ── Phase 1–9 feature flags ──────────────────────────────────────────────────
    enable_ai_council: bool = False
    enable_neural_lab: bool = True    # MA forecaster is always available; safe to enable by default
    enable_quantum_lab: bool = False
    enable_marl_lab: bool = False
    enable_goal_governor: bool = False

    # Local LLM gateway
    local_llm_gateway: str = "disabled"          # disabled | stub | ollama | llama_cpp | vllm
    local_llm_runtime: str = "stub"
    local_llm_primary_model: str = "gemma4-31b"
    local_llm_fast_model: str = "gemma4-e4b"
    local_llm_coordinator_model: str = "gemma4-26b-moe"
    local_llm_base_url: str = "http://localhost:11434"
    local_llm_timeout_seconds: int = 15
    local_llm_max_output_tokens: int = 2048

    # Quantum lab
    quantum_backend: str = "classical"           # classical | qiskit | dwave
    quantum_timeout_seconds: int = 3
    quantum_max_candidates: int = 12
    quantum_risk_aversion: float = 1.0
    quantum_cardinality_limit: int = 4
    quantum_min_baseline_improvement: float = 0.0

    # Goal governor
    yearly_profit_target: float = 50_000_000.0   # 5 crore INR aspirational target

    # Database  (empty = SQLite fallback; set DATABASE_URL for PostgreSQL)
    database_url: str = ""

    @property
    def angel_one_configured(self) -> bool:
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
        return (
            self.execution_mode.value.startswith("LIVE")
            and self.live_trading_enabled
            and self.live_order_confirmation == LIVE_ORDER_CONFIRMATION_PHRASE
            and self.angel_one_configured
        )


def _parse_cors_origins(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    parts = [p.strip() for p in raw.split(",")]
    return tuple(p for p in parts if p)


def _parse_csv_tuple(raw: str) -> tuple[str, ...]:
    return tuple(part.strip().upper() for part in raw.split(",") if part.strip())


def load_settings() -> Settings:
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

    return Settings(
        execution_mode=ExecutionMode(os.getenv("EXECUTION_MODE", "BACKTEST").upper()),
        broker=os.getenv("BROKER", "ANGEL_ONE"),
        live_trading_enabled=_bool_env("LIVE_TRADING_ENABLED", False),
        initial_capital=initial_capital,
        max_drawdown=max_drawdown,
        max_daily_loss=max_daily_loss,
        max_position_pct=max_position_pct,
        max_margin_utilization=max_margin_utilization,
        live_order_confirmation=os.getenv("LIVE_ORDER_CONFIRMATION", ""),
        angel_one_api_key=os.getenv("ANGEL_ONE_API_KEY", ""),
        angel_one_api_secret=os.getenv("ANGEL_ONE_API_SECRET", ""),
        angel_one_client_code=os.getenv("ANGEL_ONE_CLIENT_CODE", ""),
        angel_one_pin=os.getenv("ANGEL_ONE_PIN", ""),
        angel_one_totp_secret=os.getenv("ANGEL_ONE_TOTP_SECRET", ""),
        angel_one_instrument_master_url=os.getenv(
            "ANGEL_ONE_INSTRUMENT_MASTER_URL",
            "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
        ),
        angel_one_instrument_cache_path=os.getenv(
            "ANGEL_ONE_INSTRUMENT_CACHE_PATH",
            "data/processed/angel_one_instruments.json",
        ),
        aws_region=os.getenv("AWS_REGION", "ap-south-1"),
        api_auth_token=api_auth_token,
        api_cors_origins=_parse_cors_origins(
            os.getenv("API_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
        ),
        api_auth_required=api_auth_required,
        auto_start_agent=_bool_env("AUTO_START_AGENT", False),
        auto_start_live_feed=_bool_env("AUTO_START_LIVE_FEED", False),
        auto_load_instrument_cache=_bool_env("AUTO_LOAD_INSTRUMENT_CACHE", True),
        auto_load_models=_bool_env("AUTO_LOAD_MODELS", True),
        premarket_refresh_instruments=_bool_env("PREMARKET_REFRESH_INSTRUMENTS", False),
        live_feed_default_symbols=_parse_csv_tuple(
            os.getenv(
                "LIVE_FEED_DEFAULT_SYMBOLS",
                "NIFTY,BANKNIFTY,FINNIFTY,SENSEX,RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK,SBIN",
            )
        ),
        live_feed_max_symbols=max(1, int(os.getenv("LIVE_FEED_MAX_SYMBOLS", "80"))),
        # Phase 1-9 flags
        enable_ai_council=_bool_env("ENABLE_AI_COUNCIL", False),
        enable_neural_lab=_bool_env("ENABLE_NEURAL_LAB", True),
        enable_quantum_lab=_bool_env("ENABLE_QUANTUM_LAB", False),
        enable_marl_lab=_bool_env("ENABLE_MARL_LAB", False),
        enable_goal_governor=_bool_env("ENABLE_GOAL_GOVERNOR", False),
        local_llm_gateway=os.getenv("LOCAL_LLM_GATEWAY", "disabled"),
        local_llm_runtime=os.getenv("LOCAL_LLM_RUNTIME", "stub"),
        local_llm_primary_model=os.getenv("LOCAL_LLM_PRIMARY_MODEL", "gemma4-31b"),
        local_llm_fast_model=os.getenv("LOCAL_LLM_FAST_MODEL", "gemma4-e4b"),
        local_llm_coordinator_model=os.getenv("LOCAL_LLM_COORDINATOR_MODEL", "gemma4-26b-moe"),
        local_llm_base_url=os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434"),
        local_llm_timeout_seconds=int(os.getenv("LOCAL_LLM_TIMEOUT_SECONDS", "15")),
        local_llm_max_output_tokens=int(os.getenv("LOCAL_LLM_MAX_OUTPUT_TOKENS", "2048")),
        quantum_backend=os.getenv("QUANTUM_BACKEND", "classical"),
        quantum_timeout_seconds=int(os.getenv("QUANTUM_TIMEOUT_SECONDS", "3")),
        quantum_max_candidates=int(os.getenv("QUANTUM_MAX_CANDIDATES", "12")),
        quantum_risk_aversion=float(os.getenv("QUANTUM_RISK_AVERSION", "1.0")),
        quantum_cardinality_limit=int(os.getenv("QUANTUM_CARDINALITY_LIMIT", "4")),
        quantum_min_baseline_improvement=float(os.getenv("QUANTUM_MIN_BASELINE_IMPROVEMENT", "0.0")),
        yearly_profit_target=float(os.getenv("YEARLY_PROFIT_TARGET", "50000000")),
        database_url=os.getenv("DATABASE_URL", ""),
    )
