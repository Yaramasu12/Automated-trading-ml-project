from __future__ import annotations

from enum import Enum


class ExecutionMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"
    BFO = "BFO"


class Segment(str, Enum):
    CASH = "CASH"
    FUTURES = "FUTURES"
    OPTIONS = "OPTIONS"


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    INDEX = "INDEX"


class InstrumentType(str, Enum):
    EQUITY = "EQUITY"
    INDEX = "INDEX"
    FUTURE = "FUTURE"
    OPTION = "OPTION"


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self == Side.BUY else -1


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOPLOSS = "STOPLOSS"


class ProductType(str, Enum):
    INTRADAY = "INTRADAY"
    CARRYFORWARD = "CARRYFORWARD"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    RISK_REJECTED = "RISK_REJECTED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
