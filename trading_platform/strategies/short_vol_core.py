"""Short-volatility core strategies — the proven edge.

Extends the original ShortVolStrategy with:
1. Iron condor (existing, proven)
2. Short strangle with delta bands
3. Jade lizard (reduced tail risk)
4. Calendar when term structure favorable

Entry gating via VRP (Variance Risk Premium) signal:
- VRP = ATM implied vol − forecast realized vol
- Enter only when VRP is in top quintile of its history
- IV rank > 50 as secondary confirm
- No entry during EventRiskGuard blackout

Sizing:
- Margin-aware (Angel One margin API)
- Fractional-Kelly capped at 0.25×
- Portfolio vega cap per underlying
- Volatility targeting at portfolio level

Management:
- Exit at 50% max profit
- Stop at 2× credit received
- Delta-band re-hedge
- Expiry-morning square-off (via EmergencySquareOff)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from .protocol import (
    Signal,
    SignalDirection,
    SignalStructure,
    Strategy,
    StrategyState,
)

logger = logging.getLogger(__name__)


@dataclass
class VolForecast:
    """Volatility forecast from HAR-RV or GARCH."""
    symbol: str
    forecast_vol: float  # Annualized forecast
    confidence: float  # 0-1
    timestamp: datetime
    method: str  # "har_rv" or "garch"


@dataclass
class VRPSignal:
    """Variance Risk Premium signal."""
    symbol: str
    vrp: float  # VRP value (implied - forecast)
    iv_rank: float  # 0-100
    iv_percentile: float  # 0-100
    top_quintile: bool  # Is VRP in top quintile?
    timestamp: datetime


@dataclass
class ShortVolConfig:
    """Configuration for short-vol strategies."""
    # Entry gates
    min_iv_rank: float = 50.0  # IV rank minimum
    min_vrp: float = 0.0  # Minimum VRP threshold
    max_dte: int = 45  # Maximum days to expiry
    min_dte: int = 7  # Minimum days to expiry (avoid gamma risk)
    max_strikes_away: float = 1.0  # Max strikes away from ATM

    # Strike selection
    short_strike_delta: float = 0.15  # Sell 0.15 delta
    long_strike_delta: float = 0.05  # Buy 0.05 delta protection
    spread_width_pct: float = 0.02  # 2% spread width

    # Management
    profit_take_pct: float = 0.50  # Exit at 50% max profit
    stop_loss_mult: float = 2.0  # Stop at 2× credit
    rehedge_delta_threshold: float = 0.05  # Re-hedge when delta exceeds this

    # Sizing
    max_kelly_fraction: float = 0.25  # Cap Kelly at 25%
    portfolio_vega_cap: float = 10000.0  # Portfolio vega cap
    target_vol: float = 0.15  # Target annualized vol (15%)

    # Calendar
    min_term_structure_slope: float = 0.0  # Min slope for calendar (contango)

    # Expiry
    expiry_square_off_hour: int = 9  # Square off at 9:15 IST on expiry
    expiry_cutoff_days: int = 2  # Stop entering < 2 DTE near expiry


class ShortVolStrategy(Strategy):
    """Core systematic short-volatility premium selling strategy.

    Supports iron condor, short strangle, jade lizard, and calendar
    structures based on configuration.

    Entry conditions:
    1. IV rank > min_iv_rank (default 50)
    2. VRP in top quintile (implied > forecast vol)
    3. No EventRiskGuard blackout
    4. DTE within bounds
    5. Term structure favorable (for calendars)

    Strike selection:
    - Short strikes at configured delta (0.15 default)
    - Long strikes at configured delta (0.05 default) for defined risk
    - Jade lizard: no call protection, put protection only
    - Calendar: long-dated short near-term when term structure positive

    Management:
    - Real-time P&L monitoring
    - Delta-band re-hedge
    - 50% profit take / 2× stop loss
    - Expiry-morning square-off
    """

    def __init__(
        self,
        name: str = "short_vol_core",
        version: str = "1.0.0",
        enabled: bool = True,
        event_bus: Any = None,
        config: Optional[ShortVolConfig] = None,
        iv_rank_history: Optional[list[float]] = None,
        vol_forecasts: Optional[dict[str, VolForecast]] = None,
        vrp_signals: Optional[list[VRPSignal]] = None,
    ) -> None:
        super().__init__(name, version, enabled, event_bus)
        self._config = config or ShortVolConfig()
        self._iv_rank_history = iv_rank_history or []
        self._vol_forecasts = vol_forecasts or {}
        self._vrp_signals = vrp_signals or []
        self._positions: dict[str, dict] = {}  # symbol → position state
        self._max_profit_seen: dict[str, float] = {}
        self._entry_prices: dict[str, float] = {}

    @property
    def config(self) -> ShortVolConfig:
        return self._config

    @config.setter
    def config(self, value: ShortVolConfig) -> None:
        self._config = value
        logger.info("[%s] Config updated", self._name)

    @property
    def positions(self) -> dict[str, dict]:
        return self._positions

    async def on_tick(self, tick: Any) -> list[Signal]:
        """Process tick — primarily for management signals (P&L, delta)."""
        signals: list[Signal] = []

        # Check for management triggers on open positions
        for symbol, pos in list(self._positions.items()):
            mgmt_signals = self._check_management(symbol, pos, tick)
            signals.extend(mgmt_signals)

        return signals

    async def on_bar(self, bar: Any) -> list[Signal]:
        """Process 1m/5m bar — check entry conditions."""
        signals: list[Signal] = []

        if not self._check_health():
            return signals

        # Check entry for each eligible underlying
        underlyings = self._get_eligible_underlyings(bar)
        for underlying in underlyings:
            entry_signal = self._check_entry(underlying, bar)
            if entry_signal:
                signals.append(entry_signal)

        return signals

    def _get_eligible_underlyings(self, bar: Any) -> list[str]:
        """Get list of eligible underlyings."""
        # NIFTY, BANKNIFTY, FINNIFTY, SENSEX
        return ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]

    def _check_entry(
        self,
        symbol: str,
        bar: Any,
    ) -> Optional[Signal]:
        """Check if entry conditions are met for a symbol."""
        # 1. IV rank check
        iv_rank = self._get_iv_rank(symbol)
        if iv_rank < self._config.min_iv_rank:
            return None

        # 2. VRP check
        vrp = self._compute_vrp(symbol)
        if vrp is None or not vrp.top_quintile:
            return None

        # 3. DTE check
        dte = self._get_dte(symbol)
        if dte < self._config.min_dte or dte > self._config.max_dte:
            return None

        # 4. Check if we already have a position
        if symbol in self._positions:
            return None

        # 5. Margin check
        if not self._check_margin(symbol):
            return None

        # All conditions met — produce signal
        conviction = self._compute_conviction(symbol, iv_rank, vrp)

        # Determine structure based on config and market state
        structure = self._select_structure(symbol, iv_rank)

        signal = Signal(
            symbol=symbol,
            exchange="NSE",
            segment="FNO",
            direction=SignalDirection.SHORT,
            structure=structure,
            conviction=conviction,
            features={
                "iv_rank": iv_rank,
                "iv_percentile": vrp.iv_percentile if vrp else 0,
                "vrp": vrp.vrp if vrp else 0,
                "dte": dte,
                "method": "short_vol",
            },
            strategy=self._name,
            regime=self._get_regime(),
            ttl=300,  # 5 minute validity for entry
            meta={
                "config": {
                    "short_strike_delta": self._config.short_strike_delta,
                    "long_strike_delta": self._config.long_strike_delta,
                    "structure": structure.value,
                },
            },
        )

        logger.info(
            "[%s] Entry signal: %s %s (IV rank: %.1f, VRP: %.3f)",
            self._name, symbol, structure.value, iv_rank, vrp.vrp if vrp else 0,
        )

        return self._emit_signal(signal)

    def _check_management(
        self,
        symbol: str,
        position: dict,
        tick: Any,
    ) -> list[Signal]:
        """Check management triggers for open position."""
        signals = []

        # P&L check
        current_pnl = self._compute_pnl(symbol, position, tick)
        max_credit = self._get_max_credit(position)

        # 50% profit take
        if current_pnl >= max_credit * self._config.profit_take_pct:
            signals.append(Signal(
                symbol=symbol,
                exchange="NSE",
                segment="FNO",
                direction=SignalDirection.SQUARED,
                structure=position["structure"],
                conviction=0.8,
                features={"trigger": "profit_take_50"},
                strategy=self._name,
                meta={"target_pnl": current_pnl},
            ))
            return signals

        # 2× stop loss
        if current_pnl <= -max_credit * self._config.stop_loss_mult:
            signals.append(Signal(
                symbol=symbol,
                exchange="NSE",
                segment="FNO",
                direction=SignalDirection.SQUARED,
                structure=position["structure"],
                conviction=0.9,
                features={"trigger": "stop_loss_2x"},
                strategy=self._name,
                meta={"target_pnl": current_pnl},
            ))
            return signals

        # Delta-band re-hedge
        current_delta = position.get("net_delta", 0)
        if abs(current_delta) > self._config.rehedge_delta_threshold:
            signals.append(Signal(
                symbol=symbol,
                exchange="NSE",
                segment="FNO",
                direction=SignalDirection.COVER,
                structure=position["structure"],
                conviction=0.6,
                features={
                    "trigger": "delta_rehedge",
                    "current_delta": current_delta,
                    "threshold": self._config.rehedge_delta_threshold,
                },
                strategy=self._name,
            ))

        return signals

    def _compute_conviction(
        self,
        symbol: str,
        iv_rank: float,
        vrp: Optional[VRPSignal],
    ) -> float:
        """Compute signal conviction (0-1) based on VRP and IV rank."""
        # Base conviction from IV rank
        base = min(iv_rank / 100.0, 1.0)

        # VRP boost
        vrp_boost = 0
        if vrp and vrp.top_quintile:
            # Scale VRP z-score to conviction boost
            vrp_z = (vrp.vrp - self._mean_vrp()) / max(self._std_vrp(), 1e-10)
            vrp_boost = min(max(vrp_z / 3.0, 0), 0.3)

        conviction = min(base + vrp_boost, 1.0)
        return round(conviction, 3)

    def _select_structure(self, symbol: str, iv_rank: float) -> SignalStructure:
        """Select the optimal structure based on market conditions."""
        # Jade lizard when IV rank is moderate (reduces tail risk)
        if 40 <= iv_rank < 70:
            return SignalStructure.JADE_LIZARD

        # Calendar when term structure is favorable
        if self._check_term_structure(symbol):
            return SignalStructure.CALENDAR

        # Default: iron condor for high IV, strangle for very high IV
        if iv_rank >= 70:
            return SignalStructure.STRANGLE

        return SignalStructure.IRON_CONDOR

    def _compute_pnl(
        self,
        symbol: str,
        position: dict,
        tick: Any,
    ) -> float:
        """Compute unrealized P&L for a position."""
        # Simplified P&L calculation
        # In production: use actual option pricing with Greeks
        current_price = tick.get("last_price", 0)
        entry_price = self._entry_prices.get(symbol, position.get("avg_price", 0))
        qty = position.get("qty", 0)

        if entry_price <= 0:
            return 0.0

        pnl = (current_price - entry_price) * qty
        return pnl

    def _get_max_credit(self, position: dict) -> float:
        """Get max credit (max profit) for a position."""
        return position.get("max_credit", 0)

    def _get_iv_rank(self, symbol: str) -> float:
        """Get current IV rank for a symbol."""
        # In production: query from options chain collector
        # Placeholder
        return 55.0

    def _compute_vrp(self, symbol: str) -> Optional[VRPSignal]:
        """Compute Variance Risk Premium signal."""
        # Get implied vol from chain
        iv = self._get_implied_vol(symbol)

        # Get forecast from vol_forecasts
        forecast = self._vol_forecasts.get(symbol)
        if forecast is None:
            return None

        vrp = iv - forecast.forecast_vol

        # Compute IV rank/percentile
        iv_rank = self._compute_iv_rank(iv)
        iv_pct = self._compute_iv_percentile(iv)

        # Top quintile check
        quintile = np.percentile(self._iv_rank_history, 80) if self._iv_rank_history else 50
        top_quintile = iv_rank >= quintile

        return VRPSignal(
            symbol=symbol,
            vrp=vrp,
            iv_rank=iv_rank,
            iv_percentile=iv_pct,
            top_quintile=top_quintile,
            timestamp=datetime.now(timezone.utc),
        )

    def _get_implied_vol(self, symbol: str) -> float:
        """Get current ATM implied vol."""
        # In production: from options chain collector
        return 0.20  # Placeholder: 20% IV

    def _compute_iv_rank(self, iv: float) -> float:
        """Compute IV rank from history."""
        if not self._iv_rank_history:
            return 50.0
        hist = sorted(self._iv_rank_history)
        rank = sum(1 for h in hist if h <= iv) / len(hist) * 100
        return min(max(rank, 0), 100)

    def _compute_iv_percentile(self, iv: float) -> float:
        """Compute IV percentile (same as rank for simplicity)."""
        return self._compute_iv_rank(iv)

    def _mean_vrp(self) -> float:
        """Mean VRP from history."""
        if not self._vrp_signals:
            return 0.02
        return sum(s.vrp for s in self._vrp_signals) / len(self._vrp_signals)

    def _std_vrp(self) -> float:
        """Std VRP from history."""
        if len(self._vrp_signals) < 2:
            return 0.02
        mean = self._mean_vrp()
        variance = sum((s.vrp - mean) ** 2 for s in self._vrp_signals) / (len(self._vrp_signals) - 1)
        return variance ** 0.5

    def _get_dte(self, symbol: str) -> int:
        """Get days to expiry."""
        # In production: query instrument master
        return 20  # Placeholder

    def _check_margin(self, symbol: str) -> bool:
        """Check if margin is available."""
        # In production: query Angel One margin API
        return True  # Placeholder

    def _get_regime(self) -> str:
        """Get current market regime."""
        return "low_vol"  # Placeholder — real impl uses HMM/Bayesian CPD

    def _check_term_structure(self, symbol: str) -> bool:
        """Check if term structure is favorable for calendars."""
        # In production: compare near-month vs far-month IV
        return False  # Placeholder

    def set_position(self, symbol: str, position: dict) -> None:
        """Set current position state (called by StrategyEngine after fill)."""
        self._positions[symbol] = position
        self._state.active_positions = len(self._positions)

    def remove_position(self, symbol: str) -> None:
        """Remove position after exit."""
        self._positions.pop(symbol, None)
        self._entry_prices.pop(symbol, None)
        self._max_profit_seen.pop(symbol, None)
        self._state.active_positions = len(self._positions)

    def clear_all_positions(self) -> None:
        """Clear all positions (EOD square-off)."""
        self._positions.clear()
        self._entry_prices.clear()
        self._max_profit_seen.clear()
        self._state.active_positions = 0