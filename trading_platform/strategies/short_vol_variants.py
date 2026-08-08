"""
trading_platform/strategies/short_vol_variants.py — Short-volatility strategy variants

Per §4.2 (REDESIGN_PROMPT): Extend the short-vol suite with:
- Short strangle with delta bands (margin-aware)
- Jade lizard (reduced risk, premium-focused)
- Calendar spreads (when term structure favorable)
- Delta-band management on all variants
- VRP entry gating integrated

All strategies implement the Strategy protocol (base.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Strategy types
# ──────────────────────────────────────────────


class VolStrategyType(str, Enum):
    IRON_CONDOR = "iron_condor"
    PUT_SPREAD = "put_spread"
    SHORT_STRANGLE = "short_strangle"
    JADE_LIZARD = "jade_lizard"
    CALENDAR = "calendar"


# ──────────────────────────────────────────────
# Strategy configuration
# ──────────────────────────────────────────────


@dataclass
class ShortVolConfig:
    """Configuration for a short-volatility strategy instance."""
    strategy_type: VolStrategyType
    underlying: str  # NIFTY, BANKNIFTY, FINNIFTY, SENSEX
    expiry_days: int  # 7, 14, 21, 30 DTE
    lot_size: int
    max_notional: float  # max notional per trade
    kelly_fraction: float = 0.25  # fractional Kelly cap
    kelly_floor: float = 0.0  # floor on Kelly on regime change
    entry_iv_rank_threshold: float = 50.0  # IV rank > 50
    entry_vrp_zscore_threshold: float = 0.5  # VRP z-score
    exit_pct_profit: float = 0.50  # exit at 50% max profit
    stop_pct_credit: float = 2.0  # stop at 2x credit
    delta_upper_band: float = 0.15  # delta bands for strangle
    delta_lower_band: float = 0.15
    delta_rehedge_threshold: float = 0.05  # re-hedge when delta moves
    expiry_cutoff_days: int = 2  # emergency square-off 2 days before expiry
    min_credit: float = 50.0  # minimum credit to enter (INR)
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"{self.strategy_type.value}_{self.underlying}"


# ──────────────────────────────────────────────
# Signal schema (matches base.py Signal)
# ──────────────────────────────────────────────


@dataclass
class Signal:
    """A trading signal from any strategy."""
    instrument: str
    direction: str  # "long", "short", "sell"
    structure: VolStrategyType
    conviction: float  # 0-1
    features: dict[str, Any] = field(default_factory=dict)
    ttl: Optional[int] = None  # signal time-to-live (seconds)
    metadata: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────
# VRP (Variance Risk Premium) signal
# ──────────────────────────────────────────────


@dataclass
class VRPSignal:
    """Variance Risk Premium calculation."""
    underlying: str
    atm_iv: float  # ATM implied volatility
    forecast_rv: float  # forecast realized volatility (HAR-RV or GARCH)
    vrp: float  # VRP = ATM IV - forecast RV
    vrp_zscore: float  # z-score of VRP vs historical
    iv_rank: float  # IV rank (0-100)
    is_rich: bool  # VRP in top quintile
    timestamp: float  # Unix timestamp


def compute_vrp(
    atm_iv: float,
    forecast_rv: float,
    historical_vrp_series: list[float],
    underlying: str = "",
    timestamp: float = 0.0,
) -> VRPSignal:
    """Compute VRP (Variance Risk Premium) signal per §4.4a.

    VRP = ATM implied vol − forecast realized vol.
    This is the *formal* reason short-vol makes money.
    Enter premium-selling only when VRP is rich (top-quintile of its history).
    Size proportional to VRP z-score.

    Args:
        atm_iv: ATM implied volatility (annualized, e.g. 0.20 for 20%)
        forecast_rv: Forecast realized volatility (HAR-RV or GARCH output)
        historical_vrp_series: Historical VRP series for percentile computation
        underlying: Underlying instrument (NIFTY, BANKNIFTY, etc.)
        timestamp: Unix timestamp of the signal

    Returns:
        VRPSignal with VRP, z-score, IV rank, and richness flag.
    """
    vrp = atm_iv - forecast_rv

    # Compute z-score vs historical VRP series
    # Requires minimum warmup period for statistically meaningful z-score
    min_samples = 30  # ~30 trading days of hourly data or ~6 hours of 1-min data
    if len(historical_vrp_series) >= min_samples:
        mean_vrp = float(np.mean(historical_vrp_series))
        std_vrp = float(np.std(historical_vrp_series))
        vrp_zscore = (vrp - mean_vrp) / max(std_vrp, 1e-8)
        # IV rank: percentile of current VRP within its own historical distribution
        iv_rank = (sum(1 for v in historical_vrp_series if v <= vrp) / len(historical_vrp_series)) * 100
    else:
        # Insufficient history — neutral signal
        vrp_zscore = 0.0
        iv_rank = 50.0

    # Top quintile threshold: VRP in top 20% of its distribution
    # Corresponds approximately to z > 0.84 for normal distribution
    # We use 0.52 for slightly earlier entry (more sensitive)
    is_rich = vrp_zscore > 0.52

    return VRPSignal(
        underlying=underlying,
        atm_iv=atm_iv,
        forecast_rv=forecast_rv,
        vrp=vrp,
        vrp_zscore=vrp_zscore,
        iv_rank=iv_rank,
        is_rich=is_rich,
        timestamp=timestamp,
    )


def compute_vrp_with_har_rv(
    atm_iv: float,
    realized_vol_history: list[float],
    historical_vrp_series: list[float],
    underlying: str = "",
    timestamp: float = 0.0,
    har_window_1min: int = 5,
    har_window_1hour: int = 60,
    har_window_daily: int = 1440,
) -> VRPSignal:
    """Compute VRP using HAR-RV forecast per §4.4a.

    HAR-RV (Heterogeneous Autoregression Realized Volatility) is the strongest
    simple RV baseline in the literature. It combines 1-minute, 1-hour, and
    daily realized vol components.

    Formula: RV_forecast = β₁·RV_1min + β₂·RV_1hour + β₃·RV_daily + α

    Args:
        atm_iv: ATM implied volatility
        realized_vol_history: Historical 1-minute realized vol series (needs har_window_daily samples)
        historical_vrp_series: Historical VRP series for percentile
        underlying: Underlying instrument
        timestamp: Unix timestamp
        har_window_1min: Window for 1-min component (default 5)
        har_window_1hour: Window for 1-hour component (default 60)
        har_window_daily: Window for daily component (default 1440 = 24h × 60min)

    Returns:
        VRPSignal with HAR-RV forecast integrated.
    """
    if len(realized_vol_history) < har_window_daily:
        # Insufficient data for HAR-RV — fall back to simple average
        forecast_rv = float(np.mean(realized_vol_history)) if realized_vol_history else atm_iv * 0.8
        return compute_vrp(atm_iv, forecast_rv, historical_vrp_series, underlying, timestamp)

    # Take the last N windows of 1-min realized vol
    rv_1min = float(np.mean(realized_vol_history[-har_window_1min:]))
    rv_1hour = float(np.mean(realized_vol_history[-har_window_1hour:]))
    rv_daily = float(np.mean(realized_vol_history[-har_window_daily:]))

    # HAR-RV coefficients (standard calibration — should be periodically re-estimated)
    beta_1min = 0.065   # 1-minute component (short-term volatility persistence)
    beta_1hour = 0.485  # 1-hour component (intraday persistence)
    beta_daily = 0.362  # Daily component (overnight + long-term)
    alpha = 0.0          # Intercept (usually near zero for well-calibrated models)

    # HAR-RV forecast
    forecast_rv = beta_1min * rv_1min + beta_1hour * rv_1hour + beta_daily * rv_daily + alpha

    # Clamp to reasonable bounds (volatility can't be negative or > 200%)
    forecast_rv = max(0.001, min(forecast_rv, 2.0))

    return compute_vrp(
        atm_iv=atm_iv,
        forecast_rv=forecast_rv,
        historical_vrp_series=historical_vrp_series,
        underlying=underlying,
        timestamp=timestamp,
    )


def compute_vrp_zscore_position(
    vrp_signal: VRPSignal,
    max_position_fraction: float = 0.25,
) -> float:
    """Compute position sizing fraction from VRP z-score.

    Size scales proportionally with VRP z-score, capped at max_position_fraction.
    This converts the current "sell when IV rank > 50" heuristic into a
    measured edge with a tracked hit rate.

    Args:
        vrp_signal: VRP signal from compute_vrp or compute_vrp_with_har_rv
        max_position_fraction: Maximum position fraction (default 0.25 = 25%)

    Returns:
        Position sizing fraction in [0, max_position_fraction].
    """
    if not vrp_signal.is_rich:
        return 0.0  # Don't enter when VRP is not rich

    # Scale linearly with z-score, normalize by 2.0 (very rich VRP)
    scaled = (vrp_signal.vrp_zscore / 2.0) * max_position_fraction

    # Floor at 0, cap at max
    return max(0.0, min(scaled, max_position_fraction))


# ──────────────────────────────────────────────
# Short strangle with delta bands
# ──────────────────────────────────────────────


class ShortStrangleStrategy:
    """Short strangle with delta-band management.

    Only where margin allows. Sells OTM calls + puts, manages
    delta bands, exits at 50% profit / 2x credit stop.
    """

    def __init__(self, config: ShortVolConfig) -> None:
        self.config = config
        self._position: Optional[dict[str, Any]] = None
        self._avg_credit = 0.0

    def on_tick(self, tick_data: dict[str, Any]) -> list[Signal]:
        """Process tick → check entry/management signals."""
        if self._position is None:
            return self._check_entry(tick_data)
        return self._check_management(tick_data)

    def _check_entry(self, data: dict[str, Any]) -> list[Signal]:
        """Check entry conditions for short strangle."""
        iv_rank = data.get("iv_rank", 0)
        vrp = data.get("vrp", VRPSignal("", 0, 0, 0, 0, 0, False, 0))

        # Entry gating: VRP-rich condition
        if not getattr(vrp, "is_rich", False):
            return []
        if iv_rank < self.config.entry_iv_rank_threshold:
            return []

        # Check blackout (EventRiskGuard)
        if data.get("blackout", False):
            return []

        # Compute strike selection from SVI surface (§4.4a)
        iv_surface = data.get("iv_surface", {})
        call_strike = self._select_call_strike(iv_surface, "call")
        put_strike = self._select_put_strike(iv_surface, "put")

        if call_strike is None or put_strike is None:
            return []

        # Compute expected credit
        call_premium = iv_surface.get(call_strike, {}).get("iv", 0)
        put_premium = iv_surface.get(put_strike, {}).get("iv", 0)
        expected_credit = (call_premium + put_premium) * self.config.lot_size * 100  # approximate

        if expected_credit < self.config.min_credit:
            return []

        # Margin check
        estimated_margin = self._estimate_margin(call_strike, put_strike)
        if estimated_margin > self.config.max_notional:
            logger.warning(f"Margin required ({estimated_margin}) exceeds max_notional ({self.config.max_notional})")
            return []

        # Generate entry signal
        conviction = min(1.0, (iv_rank / 100) * (getattr(vrp, "vrp_zscore", 0) / 2.0))
        return [Signal(
            instrument=self.config.underlying,
            direction="short",
            structure=VolStrategyType.SHORT_STRANGLE,
            conviction=conviction,
            features={
                "call_strike": call_strike,
                "put_strike": put_strike,
                "iv_rank": iv_rank,
                "vrp_zscore": getattr(vrp, "vrp_zscore", 0),
                "expected_credit": expected_credit,
            },
            ttl=300,  # 5 min TTL for entry
            metadata={"margin_estimate": estimated_margin},
        )]

    def _check_management(self, data: dict[str, Any]) -> list[Signal]:
        """Check management signals (profit-taking, stop, delta-band re-hedge)."""
        if self._position is None:
            return []

        current_pnl = data.get("current_pnl", 0)
        max_profit = self._position.get("max_credit", 0) * self.config.exit_pct_profit
        stop_loss = self._position.get("avg_credit", 0) * self.config.stop_pct_credit

        signals = []

        # Exit at 50% profit
        if current_pnl >= max_profit and max_profit > 0:
            signals.append(Signal(
                instrument=self.config.underlying,
                direction="exit",
                structure=VolStrategyType.SHORT_STRANGLE,
                conviction=1.0,
                features={"reason": "profit_target_50pct", "pnl": current_pnl},
            ))
            return signals

        # Stop loss at 2x credit
        if current_pnl <= -stop_loss:
            signals.append(Signal(
                instrument=self.config.underlying,
                direction="exit",
                structure=VolStrategyType.SHORT_STRANGLE,
                conviction=1.0,
                features={"reason": "stop_loss_2x_credit", "pnl": current_pnl},
            ))
            return signals

        # Delta-band re-hedge
        current_delta = data.get("position_delta", 0)
        if abs(current_delta) > self.config.delta_rehedge_threshold:
            signals.append(Signal(
                instrument=self.config.underlying,
                direction="rebalance",
                structure=VolStrategyType.SHORT_STRANGLE,
                conviction=0.5,
                features={"current_delta": current_delta},
            ))

        return signals

    def _select_call_strike(self, iv_surface: dict, side: str) -> Optional[float]:
        """Select OTM call strike based on delta bands."""
        # Pick strike ~15 delta (out of the money)
        for strike, data in iv_surface.items():
            if data.get("option_type") == "call" and data.get("delta", 0) < self.config.delta_upper_band:
                return strike
        return None

    def _select_put_strike(self, iv_surface: dict, side: str) -> Optional[float]:
        """Select OTM put strike based on delta bands."""
        for strike, data in iv_surface.items():
            if data.get("option_type") == "put" and data.get("delta", 0) > -self.config.delta_lower_band:
                return strike
        return None

    def _estimate_margin(self, call_strike: float, put_strike: float) -> float:
        """Estimate margin requirement (simplified)."""
        # Angel One typically requires ~₹40k-60k per spread
        return call_strike * 100 * 0.2 + put_strike * 100 * 0.2  # rough estimate


# ──────────────────────────────────────────────
# Jade lizard strategy
# ──────────────────────────────────────────────


class JadeLizardStrategy:
    """Jade lizard strategy.

    Combines:
    - Short ATM call
    - Short OTM put (2 deltas below ATM)
    - Long further OTM put for protection

    Benefits: Reduced risk vs strangle, premium-focused.
    """

    def __init__(self, config: ShortVolConfig) -> None:
        self.config = config
        self._position: Optional[dict[str, Any]] = None

    def on_tick(self, tick_data: dict[str, Any]) -> list[Signal]:
        """Process tick → check entry/management."""
        if self._position is None:
            return self._check_entry(tick_data)
        return self._check_management(tick_data)

    def _check_entry(self, data: dict[str, Any]) -> list[Signal]:
        """Check jade lizard entry conditions."""
        iv_rank = data.get("iv_rank", 0)
        vrp = data.get("vrp", VRPSignal("", 0, 0, 0, 0, 0, False, 0))

        if not getattr(vrp, "is_rich", False):
            return []
        if iv_rank < self.config.entry_iv_rank_threshold:
            return []
        if data.get("blackout", False):
            return []

        atm_iv = data.get("atm_iv", 0)
        spot = data.get("spot_price", 0)
        if spot == 0:
            return []

        # Jade lizard strike selection
        call_strike = round(spot, -2)  # ATM call
        put_strike = spot * (1 - 0.02)  # 2 delta below ATM
        protective_put = spot * (1 - 0.04)  # 4 delta below ATM for protection

        # Expected credit (rough estimate)
        expected_credit = atm_iv * spot * 100 * 0.15  # ~15% of notional

        if expected_credit < self.config.min_credit:
            return []

        conviction = min(1.0, iv_rank / 100)
        return [Signal(
            instrument=self.config.underlying,
            direction="short",
            structure=VolStrategyType.JADE_LIZARD,
            conviction=conviction,
            features={
                "call_strike": call_strike,
                "put_strike": put_strike,
                "protective_put": protective_put,
                "iv_rank": iv_rank,
                "expected_credit": expected_credit,
            },
            ttl=300,
        )]

    def _check_management(self, data: dict[str, Any]) -> list[Signal]:
        """Management signals."""
        current_pnl = data.get("current_pnl", 0)

        signals = []
        if self._position and self._position.get("max_credit", 0) > 0:
            max_profit = self._position["max_credit"] * self.config.exit_pct_profit
            if current_pnl >= max_profit:
                signals.append(Signal(
                    instrument=self.config.underlying,
                    direction="exit",
                    structure=VolStrategyType.JADE_LIZARD,
                    conviction=1.0,
                    features={"reason": "profit_target_50pct", "pnl": current_pnl},
                ))

        return signals


# ──────────────────────────────────────────────
# Calendar spread strategy
# ──────────────────────────────────────────────


class CalendarStrategy:
    """Calendar spread strategy.

    Exploits term structure anomalies:
    - Sell near-month option
    - Buy far-month option (same strike)
    - Enter when term structure is in normal contango
    """

    def __init__(self, config: ShortVolConfig) -> None:
        self.config = config
        self._position: Optional[dict[str, Any]] = None

    def on_tick(self, tick_data: dict[str, Any]) -> list[Signal]:
        """Process tick → check entry/management."""
        if self._position is None:
            return self._check_entry(tick_data)
        return self._check_management(tick_data)

    def _check_entry(self, data: dict[str, Any]) -> list[Signal]:
        """Check calendar spread entry conditions."""
        term_structure = data.get("term_structure", {})
        if not term_structure:
            return []

        # Normal contango: far-month IV > near-month IV
        near_iv = term_structure.get("near_month_iv", 0)
        far_iv = term_structure.get("far_month_iv", 0)

        if far_iv <= near_iv:
            return []  # Inverted term structure — don't enter

        # IV rank check
        iv_rank = data.get("iv_rank", 0)
        if iv_rank < self.config.entry_iv_rank_threshold:
            return []

        # Check term structure slope
        slope = (far_iv - near_iv) / near_iv
        if slope < 0.01:  # Less than 1% slope
            return []

        spot = data.get("spot_price", 0)
        if spot == 0:
            return []

        strike = round(spot, -2)  # ATM calendar

        expected_credit = (far_iv - near_iv) * spot * 100 * 0.1  # approximate

        if expected_credit < self.config.min_credit:
            return []

        return [Signal(
            instrument=self.config.underlying,
            direction="short",
            structure=VolStrategyType.CALENDAR,
            conviction=min(1.0, slope * 10),
            features={
                "strike": strike,
                "near_month_iv": near_iv,
                "far_month_iv": far_iv,
                "term_structure_slope": slope,
                "iv_rank": iv_rank,
            },
            ttl=300,
        )]

    def _check_management(self, data: dict[str, Any]) -> list[Signal]:
        """Management signals."""
        current_pnl = data.get("current_pnl", 0)
        signals = []

        if self._position and self._position.get("max_credit", 0) > 0:
            max_profit = self._position["max_credit"] * self.config.exit_pct_profit
            if current_pnl >= max_profit:
                signals.append(Signal(
                    instrument=self.config.underlying,
                    direction="exit",
                    structure=VolStrategyType.CALENDAR,
                    conviction=1.0,
                    features={"reason": "profit_target_50pct", "pnl": current_pnl},
                ))

        return signals


# ──────────────────────────────────────────────
# Strategy factory
# ──────────────────────────────────────────────


def create_short_vol_strategy(config: ShortVolConfig):
    """Factory to create the appropriate short-vol strategy."""
    strategies = {
        VolStrategyType.IRON_CONDOR: None,  # From short_vol_core.py
        VolStrategyType.PUT_SPREAD: None,  # From short_vol_core.py
        VolStrategyType.SHORT_STRANGLE: ShortStrangleStrategy(config),
        VolStrategyType.JADE_LIZARD: JadeLizardStrategy(config),
        VolStrategyType.CALENDAR: CalendarStrategy(config),
    }
    return strategies.get(config.strategy_type)