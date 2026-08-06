from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

from trading_platform.data.instrument_master import InstrumentMaster
from trading_platform.domain.enums import InstrumentType, OptionType, Segment
from trading_platform.domain.models import Instrument


# ---------------------------------------------------------------------------
# Implied Volatility
# ---------------------------------------------------------------------------


class ImpliedVolatilityCalculator:
    """Newton-Raphson Black-Scholes inversion to recover implied volatility from a market price."""

    _MAX_ITER = 200
    _TOLERANCE = 1e-7

    def calculate(
        self,
        market_price: float,
        spot: float,
        strike: float,
        days_to_expiry: int,
        option_type: OptionType,
        risk_free_rate: float = 0.06,
    ) -> float:
        """Return annualised implied volatility.  Raises ValueError on bad inputs."""
        if market_price <= 0 or spot <= 0 or strike <= 0:
            raise ValueError("market_price, spot, and strike must all be positive")
        t = max(days_to_expiry, 1) / 365.0
        # Brenner-Subrahmanyam approximation as starting guess
        sigma = math.sqrt(2 * math.pi / t) * market_price / spot
        sigma = max(0.005, min(10.0, sigma))
        for _ in range(self._MAX_ITER):
            price = self._bs_price(spot, strike, t, sigma, option_type, risk_free_rate)
            vega = self._vega(spot, strike, t, sigma, risk_free_rate)
            diff = price - market_price
            if abs(diff) < self._TOLERANCE:
                break
            if abs(vega) < 1e-12:
                break
            sigma -= diff / vega
            sigma = max(0.001, min(10.0, sigma))
        return sigma

    def _bs_price(
        self,
        spot: float,
        strike: float,
        t: float,
        sigma: float,
        option_type: OptionType,
        r: float,
    ) -> float:
        if sigma <= 0 or t <= 0:
            return 0.0
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
        d2 = d1 - sigma * math.sqrt(t)
        if option_type == OptionType.CE:
            return spot * _normal_cdf(d1) - strike * math.exp(-r * t) * _normal_cdf(d2)
        return strike * math.exp(-r * t) * _normal_cdf(-d2) - spot * _normal_cdf(-d1)

    def _vega(self, spot: float, strike: float, t: float, sigma: float, r: float) -> float:
        if sigma <= 0 or t <= 0:
            return 0.0
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
        return spot * _normal_pdf(d1) * math.sqrt(t)


@dataclass(frozen=True)
class IVPoint:
    """One data-point on the IV surface."""

    strike: float
    option_type: str  # "CE" or "PE"
    days_to_expiry: int
    implied_volatility: float
    market_price: float


@dataclass(frozen=True)
class IVSurface:
    """Implied-volatility surface for one underlying at one snapshot in time."""

    underlying: str
    spot_price: float
    points: list[IVPoint]
    as_of: date

    def atm_iv(self, threshold_pct: float = 0.02) -> float:
        """Mean IV of strikes within `threshold_pct` of spot."""
        near = [p for p in self.points if abs(p.strike - self.spot_price) / self.spot_price <= threshold_pct]
        if not near:
            return 0.0
        return sum(p.implied_volatility for p in near) / len(near)

    def skew(self, otm_pct: float = 0.03) -> float:
        """Volatility skew: mean OTM-put IV minus mean OTM-call IV."""
        otm_calls = [p for p in self.points if p.option_type == "CE" and p.strike > self.spot_price * (1 + otm_pct)]
        otm_puts = [p for p in self.points if p.option_type == "PE" and p.strike < self.spot_price * (1 - otm_pct)]
        if not otm_calls or not otm_puts:
            return 0.0
        call_iv = sum(p.implied_volatility for p in otm_calls) / len(otm_calls)
        put_iv = sum(p.implied_volatility for p in otm_puts) / len(otm_puts)
        return put_iv - call_iv

    def term_structure(self) -> list[dict]:
        """Average IV grouped by days-to-expiry bucket."""
        buckets: dict[int, list[float]] = {}
        for p in self.points:
            buckets.setdefault(p.days_to_expiry, []).append(p.implied_volatility)
        return [
            {"days_to_expiry": dte, "mean_iv": sum(ivs) / len(ivs)}
            for dte, ivs in sorted(buckets.items())
        ]

    def to_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "spot_price": self.spot_price,
            "as_of": self.as_of.isoformat(),
            "atm_iv": self.atm_iv(),
            "skew": self.skew(),
            "point_count": len(self.points),
            "term_structure": self.term_structure(),
            "points": [
                {
                    "strike": p.strike,
                    "option_type": p.option_type,
                    "days_to_expiry": p.days_to_expiry,
                    "implied_volatility": p.implied_volatility,
                    "market_price": p.market_price,
                }
                for p in self.points
            ],
        }


class IVSurfaceBuilder:
    """Build an IVSurface from an option chain and a market-price dictionary."""

    def __init__(self) -> None:
        self._iv_calc = ImpliedVolatilityCalculator()

    def build(
        self,
        underlying: str,
        option_chain: OptionChain,
        spot_price: float,
        market_prices: dict[str, float],  # symbol -> premium
        as_of: date,
        risk_free_rate: float = 0.06,
    ) -> IVSurface:
        points: list[IVPoint] = []
        for instrument in [*option_chain.calls, *option_chain.puts]:
            mp = market_prices.get(instrument.symbol)
            if mp is None or mp <= 0:
                continue
            if instrument.expiry is None or instrument.strike is None or instrument.option_type is None:
                continue
            dte = max((instrument.expiry - as_of).days, 1)
            try:
                iv = self._iv_calc.calculate(
                    market_price=mp,
                    spot=spot_price,
                    strike=instrument.strike,
                    days_to_expiry=dte,
                    option_type=instrument.option_type,
                    risk_free_rate=risk_free_rate,
                )
                points.append(
                    IVPoint(
                        strike=instrument.strike,
                        option_type=instrument.option_type.value,
                        days_to_expiry=dte,
                        implied_volatility=iv,
                        market_price=mp,
                    )
                )
            except (ValueError, ZeroDivisionError, OverflowError):
                continue
        return IVSurface(underlying=underlying, spot_price=spot_price, points=points, as_of=as_of)


# ---------------------------------------------------------------------------
# Black-Scholes pricing (public, reusable) — same formula ImpliedVolatility-
# Calculator._bs_price and ShortVolStrategy._bs each already carry privately;
# this one is the public entry point for anything that needs to reprice an
# option under a hypothetical spot/vol (e.g. VaR scenario repricing) rather
# than invert or invert-then-price. Not a refactor of those two — they stay
# as-is to avoid touching working, tested code for an unrelated task.
# ---------------------------------------------------------------------------


def black_scholes_price(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    volatility: float,
    option_type: OptionType,
    risk_free_rate: float = 0.06,
) -> float:
    """European option price. Returns intrinsic value at expiry/zero-vol
    (T<=0 or sigma<=0) rather than raising — a scenario-repricing caller
    (e.g. VaR shocking spot across many scenarios near expiry) needs a
    sane value for every scenario, not an exception."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if time_to_expiry_years <= 0 or volatility <= 0:
        intrinsic = (spot - strike) if option_type == OptionType.CE else (strike - spot)
        return max(0.0, intrinsic)
    t = time_to_expiry_years
    sigma = volatility
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if option_type == OptionType.CE:
        return spot * _normal_cdf(d1) - strike * math.exp(-risk_free_rate * t) * _normal_cdf(d2)
    return strike * math.exp(-risk_free_rate * t) * _normal_cdf(-d2) - spot * _normal_cdf(-d1)


# ---------------------------------------------------------------------------
# IV Rank / IV Percentile
# ---------------------------------------------------------------------------

# Below this many historical observations, a rank/percentile is noise, not
# signal — matches this codebase's "don't fabricate signal from insufficient
# data" discipline (CLAUDE.md's honesty-discipline rules): decline rather
# than return a misleadingly precise number off a handful of samples.
MIN_IV_RANK_OBSERVATIONS = 20


@dataclass(frozen=True)
class IVRankResult:
    current: float
    rank: float           # 0-100: position within [min(history), max(history)]
    percentile: float      # 0-100: % of historical observations current exceeds
    lookback_n: int
    lookback_min: float
    lookback_max: float


def compute_iv_rank(current: float, history: list[float]) -> IVRankResult | None:
    """IV rank + IV percentile of `current` against a trailing `history`
    series (same units — vol points/%, e.g. India VIX closes or ATM IV%).

    Two related but distinct industry terms, both computed since traders use
    them inconsistently:

    - **Rank**: where `current` sits within [min(history), max(history)], as
      a percentage. 0 = at the lookback low, 100 = at the lookback high.
    - **Percentile**: the percentage of historical observations `current`
      exceeds. Diverges from rank when the distribution is skewed — e.g. one
      extreme spike year compresses rank even when `current` sits high
      relative to most of the actual distribution.

    Returns `None` with fewer than `MIN_IV_RANK_OBSERVATIONS` valid (positive)
    history points, rather than a rank computed off too little data.
    """
    clean = [float(v) for v in history if v is not None and v > 0]
    if len(clean) < MIN_IV_RANK_OBSERVATIONS or current <= 0:
        return None
    lo, hi = min(clean), max(clean)
    rank = 50.0 if hi <= lo else (current - lo) / (hi - lo) * 100.0
    rank = max(0.0, min(100.0, rank))
    below = sum(1 for v in clean if v < current)
    percentile = (below / len(clean)) * 100.0
    return IVRankResult(
        current=round(current, 2), rank=round(rank, 1), percentile=round(percentile, 1),
        lookback_n=len(clean), lookback_min=round(lo, 2), lookback_max=round(hi, 2),
    )


@dataclass(frozen=True)
class OptionChain:
    underlying: str
    expiry: date
    calls: list[Instrument]
    puts: list[Instrument]

    @property
    def strikes(self) -> list[float]:
        return sorted({instrument.strike for instrument in [*self.calls, *self.puts] if instrument.strike is not None})

    def liquid_strikes(self, spot_price: float, max_distance_pct: float = 0.08) -> list[float]:
        lower = spot_price * (1 - max_distance_pct)
        upper = spot_price * (1 + max_distance_pct)
        return [strike for strike in self.strikes if lower <= strike <= upper]


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float
    vega: float


@dataclass(frozen=True)
class RolloverPlan:
    action: str
    reason: str
    current_contract: str
    next_contract: str | None
    days_to_expiry: int | None


class ExpiryCalendar:
    def __init__(self, instrument_master: InstrumentMaster):
        self.instrument_master = instrument_master

    def expiries(self, underlying: str, as_of: date | None = None) -> list[date]:
        expiries = self.instrument_master.expiries(underlying)
        if as_of is not None:
            expiries = [expiry for expiry in expiries if expiry >= as_of]
        return expiries

    def nearest(self, underlying: str, as_of: date) -> date:
        return self.instrument_master.nearest_expiry(underlying, as_of)

    def next_after_nearest(self, underlying: str, as_of: date) -> date:
        expiries = self.expiries(underlying, as_of)
        if len(expiries) < 2:
            raise ValueError(f"No next expiry available for {underlying} from {as_of}")
        return expiries[1]

    def monthly(self, underlying: str, as_of: date) -> date:
        expiries = self.expiries(underlying, as_of)
        monthly_candidates = [
            expiry for expiry in expiries if expiry.day >= 23
        ]
        if not monthly_candidates:
            return self.nearest(underlying, as_of)
        return monthly_candidates[0]


class ContractSelector:
    def __init__(self, instrument_master: InstrumentMaster):
        self.instrument_master = instrument_master
        self.expiry_calendar = ExpiryCalendar(instrument_master)

    def select_future(self, underlying: str, as_of: date, expiry_preference: str = "nearest") -> Instrument:
        if expiry_preference == "next":
            expiry = self.expiry_calendar.next_after_nearest(underlying, as_of)
        elif expiry_preference == "monthly":
            expiry = self.expiry_calendar.monthly(underlying, as_of)
        else:
            expiry = self.expiry_calendar.nearest(underlying, as_of)
        futures = [
            instrument
            for instrument in self.instrument_master.by_underlying(underlying, Segment.FUTURES)
            if instrument.expiry == expiry
        ]
        if not futures:
            raise ValueError(f"No future found for {underlying} {expiry}")
        return futures[0]

    def select_option(
        self,
        underlying: str,
        as_of: date,
        spot_price: float,
        option_type: OptionType,
        moneyness_steps: int = 0,
        expiry_preference: str = "nearest",
    ) -> Instrument:
        if expiry_preference == "next":
            expiry = self.expiry_calendar.next_after_nearest(underlying, as_of)
        elif expiry_preference == "monthly":
            expiry = self.expiry_calendar.monthly(underlying, as_of)
        else:
            expiry = self.expiry_calendar.nearest(underlying, as_of)
        chain = OptionChainBuilder(self.instrument_master).build(underlying, expiry)
        side = chain.calls if option_type == OptionType.CE else chain.puts
        if not side:
            raise ValueError(f"No {option_type.value} contracts for {underlying} {expiry}")
        strikes = chain.liquid_strikes(spot_price)
        candidates = [instrument for instrument in side if instrument.strike in strikes] or side
        ordered = sorted(candidates, key=lambda instrument: abs((instrument.strike or 0) - spot_price))
        atm_index = min(max(moneyness_steps, 0), len(ordered) - 1)
        return ordered[atm_index]


class OptionChainBuilder:
    def __init__(self, instrument_master: InstrumentMaster):
        self.instrument_master = instrument_master

    def build(self, underlying: str, expiry: date) -> OptionChain:
        options = [
            instrument
            for instrument in self.instrument_master.by_underlying(underlying, Segment.OPTIONS)
            if instrument.expiry == expiry
        ]
        calls = sorted(
            [instrument for instrument in options if instrument.option_type == OptionType.CE],
            key=lambda instrument: instrument.strike or 0,
        )
        puts = sorted(
            [instrument for instrument in options if instrument.option_type == OptionType.PE],
            key=lambda instrument: instrument.strike or 0,
        )
        return OptionChain(underlying=underlying, expiry=expiry, calls=calls, puts=puts)


class GreeksCalculator:
    def calculate(
        self,
        spot_price: float,
        strike: float,
        days_to_expiry: int,
        volatility: float,
        option_type: OptionType,
        risk_free_rate: float = 0.06,
    ) -> Greeks:
        if spot_price <= 0 or strike <= 0 or volatility <= 0:
            raise ValueError("spot, strike, and volatility must be positive")
        t = max(days_to_expiry, 1) / 365
        d1 = (math.log(spot_price / strike) + (risk_free_rate + 0.5 * volatility**2) * t) / (volatility * math.sqrt(t))
        d2 = d1 - volatility * math.sqrt(t)
        pdf = _normal_pdf(d1)
        if option_type == OptionType.CE:
            delta = _normal_cdf(d1)
            theta = (
                -spot_price * pdf * volatility / (2 * math.sqrt(t))
                - risk_free_rate * strike * math.exp(-risk_free_rate * t) * _normal_cdf(d2)
            ) / 365
        else:
            delta = _normal_cdf(d1) - 1
            theta = (
                -spot_price * pdf * volatility / (2 * math.sqrt(t))
                + risk_free_rate * strike * math.exp(-risk_free_rate * t) * _normal_cdf(-d2)
            ) / 365
        gamma = pdf / (spot_price * volatility * math.sqrt(t))
        vega = spot_price * pdf * math.sqrt(t) / 100
        return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega)


class RolloverPlanner:
    def __init__(self, instrument_master: InstrumentMaster):
        self.instrument_master = instrument_master
        self.selector = ContractSelector(instrument_master)

    def plan(self, instrument: Instrument, as_of: date, allow_rollover: bool, min_days_to_expiry: int = 2) -> RolloverPlan:
        if instrument.expiry is None or instrument.underlying is None:
            return RolloverPlan("HOLD", "not_expiry_sensitive", instrument.symbol, None, None)
        days_to_expiry = (instrument.expiry - as_of).days
        if days_to_expiry > min_days_to_expiry:
            return RolloverPlan("HOLD", "expiry_not_near", instrument.symbol, None, days_to_expiry)
        if not allow_rollover:
            return RolloverPlan("EXIT", "rollover_not_allowed", instrument.symbol, None, days_to_expiry)
        if instrument.instrument_type == InstrumentType.FUTURE:
            next_contract = self.selector.select_future(instrument.underlying, as_of, "next")
        elif instrument.instrument_type == InstrumentType.OPTION and instrument.option_type is not None:
            next_contract = self.selector.select_option(
                instrument.underlying,
                as_of,
                instrument.strike or 0,
                instrument.option_type,
                expiry_preference="next",
            )
        else:
            return RolloverPlan("EXIT", "unsupported_rollover_contract", instrument.symbol, None, days_to_expiry)
        return RolloverPlan("ROLL", "expiry_near", instrument.symbol, next_contract.symbol, days_to_expiry)


def _normal_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2 * math.pi)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))

