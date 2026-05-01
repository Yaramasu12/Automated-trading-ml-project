from __future__ import annotations

import os
from dataclasses import dataclass

from trading_platform.domain.enums import ExecutionMode


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


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
    angel_one_api_key: str
    angel_one_client_code: str
    angel_one_pin: str
    angel_one_totp_secret: str
    aws_region: str

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
            self.execution_mode == ExecutionMode.LIVE
            and self.live_trading_enabled
            and self.angel_one_configured
        )


def load_settings() -> Settings:
    return Settings(
        execution_mode=ExecutionMode(os.getenv("EXECUTION_MODE", "BACKTEST").upper()),
        broker=os.getenv("BROKER", "ANGEL_ONE"),
        live_trading_enabled=_bool_env("LIVE_TRADING_ENABLED", False),
        initial_capital=float(os.getenv("INITIAL_CAPITAL", "1000000")),
        max_drawdown=float(os.getenv("MAX_DRAWDOWN", "0.10")),
        max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "0.02")),
        max_position_pct=float(os.getenv("MAX_POSITION_PCT", "0.05")),
        max_margin_utilization=float(os.getenv("MAX_MARGIN_UTILIZATION", "0.60")),
        angel_one_api_key=os.getenv("ANGEL_ONE_API_KEY", ""),
        angel_one_client_code=os.getenv("ANGEL_ONE_CLIENT_CODE", ""),
        angel_one_pin=os.getenv("ANGEL_ONE_PIN", ""),
        angel_one_totp_secret=os.getenv("ANGEL_ONE_TOTP_SECRET", ""),
        aws_region=os.getenv("AWS_REGION", "ap-south-1"),
    )

