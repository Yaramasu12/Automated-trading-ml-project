"""
trading_platform/strategies/strategy_engine.py — Strategy framework + portfolio allocator

Per §13 Phase 3: One Strategy protocol (signals only — never sizes, never orders).
Per §4.5: Portfolio allocator replaces goal_governor as capital brain.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Sequence, Type, Union

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Signal protocol
# ──────────────────────────────────────────────


@dataclass
class Signal:
    """A trading signal from a strategy (signals only — never sizes, never orders)."""
    instrument_id: str
    symbol: str
    strategy_id: str
    strategy_name: str
    direction: str  # LONG / SHORT / FLAT / COVER
    structure: str  # IRON_CONDOR / PUT_SPREAD / STRANGLE / JADE_LIZARD / CALENDAR / DIRECTIONAL
    conviction: float  # 0..1
    features: Dict[str, Any] = field(default_factory=dict)
    ttl: float = 300.0  # signal time-to-live in seconds (default 5 min)
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 300)
    correlation_id: str = ""
    signal_hash: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "direction": self.direction,
            "structure": self.structure,
            "conviction": self.conviction,
            "features": {k: (float(v) if isinstance(v, Decimal) else v) for k, v in self.features.items()},
            "ttl": self.ttl,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "correlation_id": self.correlation_id,
            "signal_hash": self.signal_hash,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Signal":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class SignalPersistence(Protocol):
    """Protocol for persisting signals."""
    async def store(self, signal: Signal) -> None: ...
    async def retrieve_pending(self, strategy_id: Optional[str] = None) -> List[Signal]: ...


# ──────────────────────────────────────────────
# Strategy protocol
# ──────────────────────────────────────────────


class Strategy(Protocol):
    """
    Strategy protocol: produces signals only (never sizes, never orders).
    on_bar/on_tick → list[Signal{instrument, direction/structure, conviction, features, ttl}]
    """

    @property
    def strategy_id(self) -> str: ...
    @property
    def strategy_name(self) -> str: ...
    @property
    def is_active(self) -> bool: ...
    @property
    def version(self) -> str: ...

    async def on_tick(self, tick: Any) -> List[Signal]: ...
    async def on_bar(self, bar: Any) -> List[Signal]: ...
    async def activate(self) -> None: ...
    async def deactivate(self) -> None: ...

    def get_status(self) -> Dict[str, Any]: ...


# ──────────────────────────────────────────────
# Strategy registry
# ──────────────────────────────────────────────


class StrategyRegistry:
    """Registry for strategy instances."""

    def __init__(self):
        self._strategies: Dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        if strategy.strategy_id in self._strategies:
            logger.warning(f"Strategy {strategy.strategy_id} already registered, replacing")
        self._strategies[strategy.strategy_id] = strategy
        logger.info(f"[STRATEGY] Registered: {strategy.strategy_id} ({strategy.strategy_name})")

    def get(self, strategy_id: str) -> Optional[Strategy]:
        return self._strategies.get(strategy_id)

    def get_active(self) -> List[Strategy]:
        return [s for s in self._strategies.values() if s.is_active]

    def get_by_name(self, name: str) -> List[Strategy]:
        return [s for s in self._strategies.values() if s.strategy_name == name]

    async def deactivate_all(self) -> None:
        for s in self._strategies.values():
            await s.deactivate()
        logger.info("[STRATEGY] All strategies deactivated")

    async def activate_all(self) -> None:
        for s in self._strategies.values():
            await s.activate()
        logger.info("[STRATEGY] All strategies activated")

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        return {sid: s.get_status() for sid, s in self._strategies.items()}


# ──────────────────────────────────────────────
# Strategy engine
# ──────────────────────────────────────────────


class StrategyEngine:
    """
    Strategy engine: runs strategies, collects signals, deduplicates, persists.
    The ONLY path to signals → Risk → Execution.
    """

    def __init__(
        self,
        registry: StrategyRegistry,
        persistence: Optional[SignalPersistence] = None,
        signal_dedup_window: float = 10.0,
        alert_callback=None,
    ):
        self.registry = registry
        self.persistence = persistence
        self.signal_dedup_window = signal_dedup_window
        self.alert_callback = alert_callback

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._seen_signals: Dict[str, float] = {}  # hash → timestamp
        self._signal_queue: List[Signal] = []

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        await self.registry.activate_all()
        logger.info("[ENGINE] Strategy engine started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.registry.deactivate_all()
        logger.info("[ENGINE] Strategy engine stopped")

    async def _run_loop(self) -> None:
        """Main engine loop: collect signals from all active strategies."""
        while self._running:
            try:
                active_strategies = self.registry.get_active()
                for strategy in active_strategies:
                    await self._process_strategy(strategy)

                # Purge expired signals
                now = time.time()
                self._seen_signals = {
                    h: ts for h, ts in self._seen_signals.items()
                    if now - ts < self.signal_dedup_window
                }

                await asyncio.sleep(0.5)  # Signal collection interval

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[ENGINE] Engine loop error: {exc}", exc_info=True)
                if self.alert_callback:
                    await self.alert_callback("WARN", "Engine loop error", str(exc))
                await asyncio.sleep(5)

    async def _process_strategy(self, strategy: Strategy) -> None:
        """Process signals from a single strategy (placeholder — actual processing
        happens via on_tick/on_bar hooks from the streaming layer)."""
        # In the full implementation, this would be called from the streaming
        # layer when new ticks/bars arrive. Here we just log status.
        status = strategy.get_status()
        if status.get("pending_signals", 0) > 0:
            logger.debug(f"[STRATEGY] {strategy.strategy_id} has {status['pending_signals']} pending signals")

    async def process_signal(self, signal: Signal) -> bool:
        """
        Process a single signal: deduplicate, validate, persist.
        Returns True if the signal was accepted, False if deduplicated.
        """
        # Generate signal hash
        signal.signal_hash = self._hash_signal(signal)

        # Dedup check
        if signal.signal_hash in self._seen_signals:
            return False
        self._seen_signals[signal.signal_hash] = time.time()

        # Expiry check
        if signal.is_expired():
            logger.debug(f"[SIGNAL] Signal {signal.signal_hash} expired, skipping")
            return False

        # Persist
        if self.persistence:
            await self.persistence.store(signal)

        self._signal_queue.append(signal)
        logger.info(f"[SIGNAL] Accepted: {signal.strategy_id} {signal.direction} "
                   f"{signal.symbol} ({signal.structure}) conviction={signal.conviction:.3f}")
        return True

    def get_pending_signals(self) -> List[Signal]:
        """Get pending (unprocessed) signals."""
        # Filter out expired
        return [s for s in self._signal_queue if not s.is_expired()]

    def clear_signals(self) -> None:
        """Clear processed signals."""
        self._signal_queue.clear()

    def _hash_signal(self, signal: Signal) -> str:
        """Generate a hash for deduplication."""
        raw = f"{signal.strategy_id}:{signal.symbol}:{signal.direction}:{signal.structure}:{signal.conviction:.3f}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ──────────────────────────────────────────────
# Portfolio allocator (replaces goal_governor)
# ──────────────────────────────────────────────


@dataclass
class AllocationDecision:
    """A capital allocation decision from the allocator."""
    strategy_id: str
    action: str  # INITIATE / INCREASE / MAINTAIN / DECREASE / REDUCE / EXIT
    target_notional: Decimal
    risk_units: int
    reason: str
    timestamp: float = field(default_factory=time.time)
    regime: str = "UNKNOWN"
    allocator_version: str = "v1.0"


class PortfolioAllocator:
    """
    Rolling risk-adjusted allocation across strategy instances.

    - Correlation-aware (short-vol variants highly correlated — cap combined vega)
    - Regime input from market_intelligence + HMM on realized vol/breadth
    - Volatility targeting at portfolio level
    - Drawdown-constrained fractional Kelly (cap 0.25×)
    """

    def __init__(
        self,
        initial_capital: Decimal = Decimal("1000000"),
        target_vol: float = 0.15,  # 15% annualized vol target
        max_drawdown: float = 0.10,  # 10% max drawdown
        kelly_fraction: float = 0.25,  # cap at 0.25× Kelly
        max_vega_per_underlying: float = 500.0,  # ₹500 vega per underlying
        correlation_lookback: int = 60,  # bars for correlation calculation
        alert_callback=None,
    ):
        self.initial_capital = initial_capital
        self.target_vol = target_vol
        self.max_drawdown = max_drawdown
        self.kelly_fraction = kelly_fraction
        self.max_vega_per_underlying = max_vega_per_underlying
        self.correlation_lookback = correlation_lookback

        self._capital = initial_capital
        self._peak_equity = initial_capital
        self._positions: Dict[str, Decimal] = {}  # strategy_id → notional
        self._allocation_history: List[AllocationDecision] = []

        self.alert_callback = alert_callback

        # Correlation matrix cache
        self._returns_cache: Dict[str, List[float]] = {}

    def allocate(self, signals: List[Signal], regime: str = "UNKNOWN") -> List[AllocationDecision]:
        """
        Make allocation decisions for all active strategies based on current signals.

        Returns list of AllocationDecision objects.
        """
        decisions: List[AllocationDecision] = []

        # Check drawdown guard
        current_equity = self._calculate_equity()
        drawdown = (self._peak_equity - current_equity) / self._peak_equity if self._peak_equity > 0 else 0

        if drawdown > self.max_drawdown:
            logger.warning(f"[ALLOCATOR] Drawdown {drawdown:.2%} exceeds limit {self.max_drawdown:.2%}")
            # Force all positions to REDUCE
            for strategy_id, notional in self._positions.items():
                decisions.append(AllocationDecision(
                    strategy_id=strategy_id,
                    action="REDUCE",
                    target_notional=Decimal("0"),
                    risk_units=0,
                    reason=f"Drawdown {drawdown:.2%} exceeds {self.max_drawdown:.2%} limit",
                    regime=regime,
                ))
            return decisions

        # Update peak equity
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity

        # Group signals by underlying for correlation-aware vega cap
        underlying_signals: Dict[str, List[Signal]] = {}
        for signal in signals:
            underlying = self._extract_underlying(signal.symbol)
            if underlying not in underlying_signals:
                underlying_signals[underlying] = []
            underlying_signals[underlying].append(signal)

        # Calculate vega exposure per underlying
        vega_exposure: Dict[str, float] = {}
        for underlying, sigs in underlying_signals.items():
            total_vega = sum(s.conviction * 100 for s in sigs)  # Simplified vega estimate
            vega_exposure[underlying] = total_vega

        # Allocate per strategy
        for signal in signals:
            underlying = self._extract_underlying(signal.symbol)
            vega_cap = self.max_vega_per_underlying
            current_vega = vega_exposure.get(underlying, 0)

            # Adjust conviction based on vega cap
            adjusted_conviction = signal.conviction
            if current_vega > vega_cap:
                # Scale down proportionally
                scale = vega_cap / current_vega
                adjusted_conviction = signal.conviction * scale
                logger.info(f"[ALLOCATOR] Vega cap hit for {underlying}: scaling conviction "
                          f"{signal.conviction:.3f} → {adjusted_conviction:.3f}")

            # Volatility targeting
            vol_target = self._calculate_vol_target(current_equity)

            # Kelly sizing
            kelly_size = self._calculate_kelly_size(signal, adjusted_conviction)
            target_notional = kelly_size * vol_target

            # Determine action
            current_notional = self._positions.get(signal.strategy_id, Decimal("0"))
            action = self._determine_action(current_notional, target_notional)

            decision = AllocationDecision(
                strategy_id=signal.strategy_id,
                action=action,
                target_notional=target_notional,
                risk_units=int(target_notional / signal.conviction) if signal.conviction > 0 else 0,
                reason=f"Regime={regime}, vol_target={vol_target:.4f}, kelly={kelly_size:.4f}",
                regime=regime,
            )
            decisions.append(decision)

            # Update positions
            self._positions[signal.strategy_id] = target_notional

        self._allocation_history.extend(decisions)
        return decisions

    def _calculate_equity(self) -> Decimal:
        """Calculate current equity (capital + unrealized P&L)."""
        # In the full implementation, this would query the portfolio ledger
        return self._capital

    def _calculate_vol_target(self, equity: Decimal) -> float:
        """Calculate volatility target scaling factor."""
        # Scale exposure to hit target vol
        # Simple: ratio of target vol to realized vol
        # In production: rolling realized vol from portfolio returns
        return 1.0  # Placeholder — full impl needs portfolio returns

    def _calculate_kelly_size(self, signal: Signal, conviction: float) -> float:
        """Calculate fractional Kelly position size."""
        # Kelly % = W - (1-W)/B
        # Where W = win probability, B = win/loss ratio
        # Simplified: use conviction as proxy for W
        w = max(0.5, conviction)  # Conviction as win prob estimate
        # Assume B ≈ 2 (typical for short-vol)
        b = 2.0
        kelly = w - (1 - w) / b
        # Apply fractional cap
        return min(kelly * self.kelly_fraction, 0.25)

    def _determine_action(self, current_notional: Decimal, target_notional: Decimal) -> str:
        """Determine allocation action based on current vs target."""
        if current_notional == 0:
            return "INITIATE"
        ratio = target_notional / current_notional if current_notional > 0 else 1.0
        if abs(ratio - 1.0) < 0.05:
            return "MAINTAIN"
        elif ratio > 1.1:
            return "INCREASE"
        elif ratio < 0.9:
            return "DECREASE"
        else:
            return "REDUCE"

    def _extract_underlying(self, symbol: str) -> str:
        """Extract underlying from symbol (NIFTY, BANKNIFTY, etc.)."""
        upper = symbol.upper()
        for underlying in ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "RELIANCE", "INFY"]:
            if underlying in upper:
                return underlying
        return "UNKNOWN"

    def get_status(self) -> Dict[str, Any]:
        """Get allocator status."""
        return {
            "capital": float(self._capital),
            "peak_equity": float(self._peak_equity),
            "current_equity": float(self._calculate_equity()),
            "positions": {k: float(v) for k, v in self._positions.items()},
            "drawdown": float((self._peak_equity - self._calculate_equity()) / self._peak_equity)
                if self._peak_equity > 0 else 0,
            "allocation_history_count": len(self._allocation_history),
        }

    def reset_peak(self) -> None:
        """Reset peak equity (e.g., at day start or equity milestone)."""
        self._peak_equity = self._calculate_equity()
        logger.info(f"[ALLOCATOR] Peak equity reset to ₹{self._peak_equity:,.2f}")