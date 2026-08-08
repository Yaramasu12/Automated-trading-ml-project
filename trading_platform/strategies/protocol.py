"""§4.1 Strategy protocol (REDESIGN_PROMPT.md) — signals-only strategy framework.

Every strategy implements the `Strategy` protocol:
- `on_tick` / `on_bar` → produces `Signal` objects (signals ONLY, never size/order)
- Every signal persists with full feature snapshot for attribution
- RiskService is the ONLY path to execution

This lives separately from `strategies/base.py` (the pre-existing `Strategy`/
`StrategyRiskEstimate`/`StrategyExitRules` classes that `derivatives.py`,
`equity.py`, and `factory.py` are built on) because the two protocols are not
compatible: `base.py`'s `Strategy` is a synchronous `generate_signal(...)`
producer wired into the live runtime; this one is the async `on_tick`/`on_bar`
protocol described in §4.1, adopted so far only by `short_vol_core.py`. Do not
merge them into one `Strategy` name — that collision is what broke the whole
`strategies` package the last time this was attempted (see git history /
memory `redesign-prompt-status`). Porting the existing strategies onto this
protocol is unstarted Phase-3 work, not a rename.
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SignalDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    COVER = "COVER"  # Close long
    SQUARED = "SQUARED"  # Close all


class SignalStructure(str, Enum):
    EQUITY = "EQUITY"
    PUT_SPREAD = "PUT_SPREAD"
    IRON_CONDOR = "IRON_CONDOR"
    STRANGLE = "STRANGLE"
    JADE_LIZARD = "JADE_LIZARD"
    CALENDAR = "CALENDAR"
    SINGLE_LEG = "SINGLE_LEG"


@dataclass
class Signal:
    """A trading signal — NOT an order. Produced by strategies, consumed by RiskService."""
    symbol: str
    exchange: str
    segment: str
    direction: SignalDirection
    structure: SignalStructure
    conviction: float  # 0-1, magnitude of edge
    features: dict[str, Any] = field(default_factory=dict)  # Full feature snapshot
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    strategy: str = ""
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ttl: Optional[int] = None  # Signal validity in seconds
    meta: dict[str, Any] = field(default_factory=dict)  # Strategy-specific metadata
    regime: str = ""  # Market regime at signal time
    agent_votes: dict[str, int] = field(default_factory=dict)  # LLM agent votes
    risk_adjusted_conviction: Optional[float] = None  # After RiskService adjustment

    @property
    def is_expired(self) -> bool:
        if self.ttl is None:
            return False
        age = (datetime.now(timezone.utc) - self.timestamp).total_seconds()
        return age > self.ttl

    def to_order_preview(
        self,
        side: str,
        quantity: int,
        price: Optional[float],
    ) -> dict:
        """Convert signal to order preview for UI confirmation."""
        return {
            "correlation_id": self.correlation_id,
            "symbol": self.symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "structure": self.structure.value,
            "conviction": self.conviction,
            "risk_adjusted_conviction": self.risk_adjusted_conviction,
            "regime": self.regime,
            "features": self.features,
        }


@dataclass
class StrategyState:
    """Runtime state for a strategy instance."""
    name: str
    version: str = "1.0.0"
    enabled: bool = True
    current_regime: str = "neutral"
    total_signals: int = 0
    active_positions: int = 0
    last_signal_time: Optional[datetime] = None
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_count: int = 0
    health: str = "healthy"  # healthy, degraded, dead


class Strategy(ABC):
    """Base class for §4.1-protocol strategies.

    Strategies produce signals ONLY — they never size, never order.
    The StrategyEngine routes signals through RiskService for execution.
    """

    def __init__(
        self,
        name: str,
        version: str = "1.0.0",
        enabled: bool = True,
        event_bus: Any = None,
    ) -> None:
        self._name = name
        self._version = version
        self._enabled = enabled
        self._event_bus = event_bus
        self._state = StrategyState(name=name)
        self._start_time = time.time()

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        self._state.enabled = value

    @property
    def state(self) -> StrategyState:
        return self._state

    @abstractmethod
    async def on_tick(self, tick: Any) -> list[Signal]:
        """Process a tick and produce signals.

        Args:
            tick: Normalized tick (Tick v2 model from market adapter)

        Returns:
            List of signals (usually empty)
        """
        ...

    @abstractmethod
    async def on_bar(self, bar: Any) -> list[Signal]:
        """Process a bar (1m/5m/15m) and produce signals.

        Args:
            bar: Bar data (OHLCV)

        Returns:
            List of signals (usually empty)
        """
        ...

    async def on_reconnect(self) -> None:
        """Called when the market data feed reconnects after a gap."""
        logger.info("[%s] Feed reconnected", self._name)

    async def on_market_open(self) -> None:
        """Called at market open."""
        logger.info("[%s] Market open", self._name)

    async def on_market_close(self) -> None:
        """Called at market close."""
        logger.info("[%s] Market close", self._name)

    async def on_error(self, error: Exception) -> None:
        """Called when the strategy encounters an error."""
        self._state.error_count += 1
        self._state.health = "degraded" if self._state.error_count < 10 else "dead"
        logger.error("[%s] Error: %s", self._name, error)

    def _emit_signal(self, signal: Signal) -> Signal:
        """Emit a signal — updates state and publishes to event bus."""
        self._state.total_signals += 1
        self._state.last_signal_time = datetime.now(timezone.utc)
        self._state.health = "healthy"

        if self._event_bus:
            # Publish signal event for observability
            pass  # In production: self._event_bus.publish(signal)

        return signal

    def _check_health(self) -> bool:
        """Check if the strategy is healthy enough to produce signals."""
        uptime = time.time() - self._start_time
        if uptime < 60 and self._state.error_count > 0:
            self._state.health = "dead"
            return False
        return True

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self._name} [{self._version}] health={self._state.health}>"
