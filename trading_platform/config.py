from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from trading_platform.domain.enums import ExecutionMode

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


def load_settings() -> Settings:
    load_local_env_files()
    return Settings(
        execution_mode=ExecutionMode(os.getenv("EXECUTION_MODE", "BACKTEST").upper()),
        broker=os.getenv("BROKER", "ANGEL_ONE"),
        live_trading_enabled=_bool_env("LIVE_TRADING_ENABLED", False),
        initial_capital=float(os.getenv("INITIAL_CAPITAL", "1000000")),
        max_drawdown=float(os.getenv("MAX_DRAWDOWN", "0.10")),
        max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "0.02")),
        max_position_pct=float(os.getenv("MAX_POSITION_PCT", "0.05")),
        max_margin_utilization=float(os.getenv("MAX_MARGIN_UTILIZATION", "0.60")),
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
        api_auth_token=os.getenv("API_AUTH_TOKEN", ""),
        api_cors_origins=_parse_cors_origins(
            os.getenv("API_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
        ),
        api_auth_required=_bool_env("API_AUTH_REQUIRED", True),
    )
