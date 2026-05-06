from __future__ import annotations

from enum import Enum, IntEnum


class ExecutionMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    SHADOW_LIVE = "SHADOW_LIVE"
    LIVE = "LIVE"
    LIVE_MANUAL_APPROVAL = "LIVE_MANUAL_APPROVAL"
    LIVE_AUTO_LIMITED = "LIVE_AUTO_LIMITED"


class StrategyState(str, Enum):
    ENABLED = "ENABLED"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"


class ApprovalState(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class SquareOffScope(str, Enum):
    GLOBAL = "GLOBAL"
    STRATEGY = "STRATEGY"
    SYMBOL = "SYMBOL"


class LegStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"
    BFO = "BFO"
    MCX = "MCX"


class Segment(str, Enum):
    CASH = "CASH"
    FUTURES = "FUTURES"
    OPTIONS = "OPTIONS"


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    INDEX = "INDEX"
    COMMODITY = "COMMODITY"


class InstrumentType(str, Enum):
    EQUITY = "EQUITY"
    INDEX = "INDEX"
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    COMMODITY_FUTURE = "COMMODITY_FUTURE"


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
    COMPLIANCE_REJECTED = "COMPLIANCE_REJECTED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class OrderPriority(IntEnum):
    KILL_SWITCH = 1
    EMERGENCY_EXIT = 2
    STOP_LOSS = 3
    EXPIRY_EXIT = 4
    HEDGE = 5
    TARGET = 6
    TRAILING_STOP = 7
    PROTECTIVE_MULTI_LEG = 8
    ENTRY = 9
    MODIFICATION = 10
