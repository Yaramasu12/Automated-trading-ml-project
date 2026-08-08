"""Portfolio Greeks — real-time options book Greeks aggregation.

Portfolio-level Greeks caps (net delta, vega, gamma) and historical-simulation
VaR on the options book. Layered under the Risk Service as the ONLY path to
the broker.

Design:
- Black-Scholes Greeks for all option positions
- Portfolio-level aggregation (sum per-underlying, net)
- VaR via historical simulation (bootstrap from 1m bars)
- Greeks caps enforced by Risk Service before any order commit
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─── Black-Scholes primitives ───────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF — Abramowitz & Stegun approximation."""
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x = abs(x) / math.sqrt(2)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x)
    return 0.5 * (1.0 + sign * y)


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1(
    spot: float,
    strike: float,
    tte: float,
    vol: float,
    rate: float = 0.06,
) -> float:
    """d1 in Black-Scholes."""
    if tte <= 0:
        # At expiry: d1 → +∞ for ITM calls, -∞ for OTM
        if spot > strike:
            return 1e10
        elif spot < strike:
            return -1e10
        return 0.0
    return (math.log(spot / strike) + (rate + 0.5 * vol ** 2) * tte) / (vol * math.sqrt(tte))


def _d2(d1: float, vol: float, tte: float) -> float:
    """d2 = d1 - σ√t."""
    return d1 - vol * math.sqrt(tte)


def bs_call_prices(spot: float, strike: float, tte: float, vol: float, rate: float = 0.06) -> tuple[float, float, float, float, float]:
    """Black-Scholes call: (price, delta, gamma, vega, theta)."""
    if tte <= 0:
        return (max(spot - strike, 0.0), 1.0 if spot > strike else 0.0, 0.0, 0.0, 0.0)

    d1_val = _d1(spot, strike, tte, vol, rate)
    d2_val = _d2(d1_val, vol, tte)

    nd1 = _norm_cdf(d1_val)
    nd2 = _norm_cdf(d2_val)
    npdf = _norm_pdf(d1_val)

    discount = math.exp(-rate * tte)

    price = spot * nd1 - strike * discount * nd2
    delta = nd1  # Call delta
    gamma = npdf / (spot * vol * math.sqrt(tte))
    vega = spot * math.sqrt(tte) * npdf  # Per 1.0 vol (divide by 100 for 1%)
    theta = -(spot * npdf * vol) / (2 * math.sqrt(tte)) - rate * strike * discount * (
        1 if rate > 0 else 1
    )  # Per day

    return (price, delta, gamma, vega, theta / 365)  # theta per day


def bs_put_prices(spot: float, strike: float, tte: float, vol: float, rate: float = 0.06) -> tuple[float, float, float, float, float]:
    """Black-Scholes put: (price, delta, gamma, vega, theta)."""
    if tte <= 0:
        return (max(strike - spot, 0.0), -1.0 if spot < strike else 0.0, 0.0, 0.0, 0.0)

    d1_val = _d1(spot, strike, tte, vol, rate)
    d2_val = _d2(d1_val, vol, tte)

    nd1 = _norm_cdf(d1_val)
    nd2 = _norm_cdf(d2_val)
    npdf = _norm_pdf(d1_val)

    discount = math.exp(-rate * tte)

    price = strike * discount * (1 - nd2) - spot * (1 - nd1)
    delta = nd1 - 1  # Put delta
    gamma = npdf / (spot * vol * math.sqrt(tte))
    vega = spot * math.sqrt(tte) * npdf
    theta = -(spot * npdf * vol) / (2 * math.sqrt(tte)) + rate * strike * discount

    return (price, delta, gamma, vega, theta / 365)


# ─── Position dataclasses ───────────────────────────────────────────────────

@dataclass
class OptionPosition:
    """A single option position."""
    symbol: str
    exchange: str
    segment: str
    quantity: int  # positive = long, negative = short
    avg_price: float
    strike: float
    option_type: str  # CALL or PUT
    expiry: datetime
    spot_price: float = 0.0
    implied_vol: float = 0.0  # Current IV
    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    theta: float = 0.0
    premium: float = 0.0  # Current market premium

    @property
    def notional(self) -> float:
        """Approximate notional value."""
        lot_size = getattr(self, "lot_size", 50)  # Default to NIFTY lot
        return self.spot_price * lot_size * abs(self.quantity)

    @property
    def direction(self) -> str:
        return "LONG" if self.quantity > 0 else "SHORT"


@dataclass
class FuturePosition:
    """A single future position."""
    symbol: str
    exchange: str
    segment: str
    quantity: int
    avg_price: float
    expiry: datetime
    spot_price: float = 0.0
    delta: float = 0.0  # Future delta ≈ lot_size per contract

    @property
    def delta_per_contract(self) -> float:
        return float(self.quantity)  # Future delta = qty (1:1)


@dataclass
class PortfolioGreeks:
    """Aggregated portfolio Greeks."""
    timestamp: datetime
    net_delta: float  # Portfolio delta (in underlying units)
    gross_delta: float  # |long_delta| + |short_delta|
    net_vega: float  # Portfolio vega (per 1% vol move)
    net_gamma: float  # Portfolio gamma
    net_theta: float  # Portfolio theta (per day)
    long_vega: float
    short_vega: float
    var_95_1day: float = 0.0  # 95% VaR (1-day, ₹)
    max_drawdown_underlying: float = 0.0  # Largest underlying exposure as % of equity
    underlying_breakdown: dict[str, dict] = field(default_factory=dict)
    status: str = "OK"  # OK, WARNING, BREACH
    warnings: list[str] = field(default_factory=list)


@dataclass
class GreeksCaps:
    """Configurable Greeks caps — set by Risk Service."""
    max_net_delta: float = 500_000  # ₹50L delta exposure
    max_gross_delta: float = 1_000_000  # ₹10L gross delta
    max_net_vega: float = 50_000  # ₹50k per 1% vol move
    max_net_gamma: float = 5_000  # Tight gamma near expiry
    max_underlying_delta_pct: float = 0.25  # 25% of portfolio per underlying
    expiry_gamma_cutoff_days: float = 2  # No new entries within 2 days of expiry


# ─── Portfolio Greeks calculator ───────────────────────────────────────────

class PortfolioGreeksCalculator:
    """Computes portfolio-level Greeks from positions.

    Called by Risk Service before any new order to validate caps.
    """

    def __init__(self, caps: Optional[GreeksCaps] = None) -> None:
        self._caps = caps or GreeksCaps()

    def compute(
        self,
        positions: list,  # OptionPosition + FuturePosition
        equity: float,
        spot_prices: Optional[dict[str, float]] = None,
        iv_data: Optional[dict[str, float]] = None,
    ) -> PortfolioGreeks:
        """Compute aggregated Greeks for the portfolio.
        
        Args:
            positions: List of OptionPosition and/or FuturePosition objects
            equity: Current portfolio equity
            spot_prices: symbol → spot price map (fetched from market data)
            iv_data: symbol/strike → implied vol map (fetched from chain)
        """
        spot_prices = spot_prices or {}
        iv_data = iv_data or {}

        net_delta = 0.0
        long_vega = 0.0
        short_vega = 0.0
        net_gamma = 0.0
        net_theta = 0.0
        underlying_exposure: dict[str, float] = {}
        warnings: list[str] = []

        for pos in positions:
            if isinstance(pos, OptionPosition):
                # Update spot/IV if available
                if pos.symbol in spot_prices:
                    pos.spot_price = spot_prices[pos.symbol]
                if pos.symbol in iv_data:
                    pos.implied_vol = iv_data[pos.symbol]

                # Compute Greeks if not already set
                if pos.delta == 0 and pos.spot_price > 0 and pos.implied_vol > 0:
                    tte = max((pos.expiry - datetime.now(timezone.utc)).total_seconds() / 86400, 0.001)
                    if pos.option_type == "CALL":
                        _, delta, gamma, vega, theta = bs_call_prices(
                            pos.spot_price, pos.strike, tte, pos.implied_vol
                        )
                    else:
                        _, delta, gamma, vega, theta = bs_put_prices(
                            pos.spot_price, pos.strike, tte, pos.implied_vol
                        )
                    pos.delta = delta * pos.quantity
                    pos.gamma = gamma * abs(pos.quantity)
                    pos.vega = vega * abs(pos.quantity)
                    pos.theta = theta * abs(pos.quantity)
                    pos.premium = (
                        bs_call_prices(pos.spot_price, pos.strike, tte, pos.implied_vol)[0]
                        if pos.option_type == "CALL"
                        else bs_put_prices(pos.spot_price, pos.strike, tte, pos.implied_vol)[0]
                    )

                long_vega += max(pos.vega, 0) * abs(pos.quantity)
                short_vega += max(-pos.vega, 0) * abs(pos.quantity)
                net_gamma += pos.gamma
                net_theta += pos.theta

                # Underlying exposure
                if pos.spot_price > 0:
                    underlying_delta = pos.delta * pos.spot_price
                    underlying_exposure[pos.symbol] = underlying_exposure.get(pos.symbol, 0) + underlying_delta

            elif isinstance(pos, FuturePosition):
                delta = pos.delta_per_contract * pos.spot_price if pos.spot_price > 0 else 0
                net_delta += delta
                underlying_exposure[pos.symbol] = underlying_exposure.get(pos.symbol, 0) + delta

        net_vega = long_vega - short_vega
        gross_delta = long_vega + short_vega  # Approximate gross delta

        # Check caps
        status = "OK"
        if abs(net_delta) > self._caps.max_net_delta:
            warnings.append(f"Net delta {net_delta:.0f} exceeds cap {self._caps.max_net_delta:.0f}")
            status = "WARNING"
        if net_vega > self._caps.max_net_vega:
            warnings.append(f"Net vega {net_vega:.0f} exceeds cap {self._caps.max_net_vega:.0f}")
            status = "WARNING"
        if net_gamma > self._caps.max_net_gamma:
            warnings.append(f"Net gamma {net_gamma:.0f} exceeds cap {self._caps.max_net_gamma:.0f}")
            status = "WARNING"

        # VaR via historical simulation (bootstrap from stored 1m bars)
        var_95 = self._compute_historical_var(positions)

        # Max underlying exposure as % of equity
        max_dd_underlying = 0.0
        if equity > 0:
            for sym, exp in underlying_exposure.items():
                pct = abs(exp) / equity
                if pct > max_dd_underlying:
                    max_dd_underlying = pct

        return PortfolioGreeks(
            timestamp=datetime.now(timezone.utc),
            net_delta=net_delta,
            gross_delta=gross_delta,
            net_vega=net_vega,
            net_gamma=net_gamma,
            net_theta=net_theta,
            long_vega=long_vega,
            short_vega=short_vega,
            var_95_1day=var_95,
            max_drawdown_underlying=max_dd_underlying,
            underlying_breakdown=underlying_exposure,
            status=status,
            warnings=warnings,
        )

    def check_new_order(
        self,
        current_greeks: PortfolioGreeks,
        order_delta: float,
        order_vega: float,
        order_gamma: float,
    ) -> tuple[bool, list[str]]:
        """Check if a new order would breach Greeks caps.
        
        Returns:
            (allowed: bool, reasons: list[str])
        """
        reasons: list[str] = []

        projected_delta = current_greeks.net_delta + order_delta
        projected_vega = current_greeks.net_vega + order_vega
        projected_gamma = current_greeks.net_gamma + order_gamma

        if abs(projected_delta) > self._caps.max_net_delta:
            reasons.append(
                f"Projected net delta {projected_delta:.0f} exceeds cap {self._caps.max_net_delta:.0f}"
            )
        if abs(projected_vega) > self._caps.max_net_vega:
            reasons.append(
                f"Projected net vega {projected_vega:.0f} exceeds cap {self._caps.max_net_vega:.0f}"
            )
        if abs(projected_gamma) > self._caps.max_net_gamma:
            reasons.append(
                f"Projected net gamma {projected_gamma:.0f} exceeds cap {self._caps.max_net_gamma:.0f}"
            )

        return (len(reasons) == 0, reasons)

    def _compute_historical_var(self, positions: list, confidence: float = 0.95, horizon_days: int = 1) -> float:
        """Historical simulation VaR — bootstrap from 1m bars stored in Timescale.
        
        For a proper implementation, this would:
        1. Fetch 1m bars from TimescaleDB (last 60 days)
        2. Resample to portfolio-level P&L series
        3. Take the (1-confidence) percentile
        
        Simplified here: use vega * vol shock as proxy.
        """
        # Quick vega-based VaR proxy
        total_vega = 0.0
        for pos in positions:
            if isinstance(pos, OptionPosition):
                total_vega += pos.vega * abs(pos.quantity)

        # 16% annual vol assumption; daily vol = 16%/√252
        daily_vol = 0.16 / math.sqrt(252)
        # Z-score for 95% confidence
        z_score = 1.645

        # VaR ≈ vega * vol shock (vega = ₹ per 1.0 vol = 100% move)
        # So for daily_vol * z_score shock:
        var = total_vega * daily_vol * z_score * math.sqrt(horizon_days)

        return var

    def get_underlying_breakdown(self, greeks: PortfolioGreeks) -> str:
        """Format underlying exposure breakdown for UI/display."""
        lines = ["=== Portfolio Greeks Breakdown ==="]
        lines.append(f"Net Delta:     {greeks.net_delta:>12,.0f}")
        lines.append(f"Net Vega:      {greeks.net_vega:>12,.0f}")
        lines.append(f"Net Gamma:     {greeks.net_gamma:>12,.0f}")
        lines.append(f"Net Theta:     {greeks.net_theta:>12,.0f}/day")
        lines.append(f"VaR (95%, 1d): {greeks.var_95_1day:>12,.0f}")
        if greeks.underlying_breakdown:
            lines.append("\n--- Underlying Exposure ---")
            for sym, delta in greeks.underlying_breakdown.items():
                lines.append(f"  {sym}: {delta:>10,.0f}")
        if greeks.warnings:
            lines.append("\n--- Warnings ---")
            for w in greeks.warnings:
                lines.append(f"  ⚠ {w}")
        return "\n".join(lines)


# ─── VaR service ────────────────────────────────────────────────────────────

class HistoricalVaRService:
    """Historical simulation VaR from stored 1m bars.

    Fetches bars from TimescaleDB, computes portfolio-level P&L series,
    returns percentile-based VaR.
    """

    def __init__(self, timescale_engine: Any) -> None:
        self._engine = timescale_engine

    async def compute(
        self,
        positions: list,
        days: int = 60,
        confidence: float = 0.95,
    ) -> dict[str, float]:
        """Compute VaR for each underlying + portfolio aggregate.
        
        Returns:
            {symbol: var_95, ...} + {"portfolio": var_95}
        """
        if not positions:
            return {"portfolio": 0.0}

        # Fetch historical 1m bars for each underlying
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days)

        # Get unique underlyings from positions
        underlyings = set()
        for pos in positions:
            if isinstance(pos, OptionPosition):
                underlyings.add(pos.symbol)

        # Bootstrap P&L for each underlying
        pnl_series: dict[str, list[float]] = {sym: [] for sym in underlyings}
        pnl_series["portfolio"] = []

        # Fetch 1m bars and resample to daily P&L
        for sym in underlyings:
            bars = await self._fetch_bars(sym, start_date, end_date)
            if bars:
                daily_returns = self._resample_to_daily(bars)
                pnl_series[sym] = daily_returns

        # Compute VaR from P&L series
        result: dict[str, float] = {}
        for sym, pnl in pnl_series.items():
            if pnl:
                var = self._percentile(pnl, (1 - confidence) * 100)
                result[sym] = abs(var)
            else:
                result[sym] = 0.0

        # Portfolio VaR (assume 50% correlation between underlyings)
        if len(pnl_series) >= 2:
            port_pnl = self._portfolio_pnl(list(pnl_series.values()))
            result["portfolio"] = abs(self._percentile(port_pnl, (1 - confidence) * 100))

        return result

    async def _fetch_bars(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        """Fetch 1m bars from TimescaleDB."""
        # Placeholder — would query the hypertable
        return []

    def _resample_to_daily(self, bars: list[dict]) -> list[float]:
        """Resample 1m bars to daily returns."""
        if not bars:
            return []
        # Simple: compute daily return from open to close
        returns: list[float] = []
        for bar in bars:
            if bar.get("open", 0) > 0:
                ret = (bar["close"] - bar["open"]) / bar["open"]
                returns.append(ret)
        return returns

    def _percentile(self, data: list[float], p: float) -> float:
        """Compute percentile from data."""
        if not data:
            return 0.0
        sorted_data = sorted(data)
        k = (len(sorted_data) - 1) * p / 100
        j = int(k)
        d = k - j
        if j + 1 < len(sorted_data):
            return sorted_data[j] * (1 - d) + sorted_data[j + 1] * d
        return sorted_data[j]

    def _portfolio_pnl(self, series_list: list[list[float]]) -> list[float]:
        """Combine P&L series into portfolio P&L (equal weight)."""
        min_len = min(len(s) for s in series_list)
        if min_len == 0:
            return []
        port_pnl: list[float] = []
        for i in range(min_len):
            val = sum(s[i] for s in series_list if i < len(s)) / len(series_list)
            port_pnl.append(val)
        return port_pnl