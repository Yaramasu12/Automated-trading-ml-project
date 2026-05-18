"""Phase-1 multi-leg builders.

Eight builders, one shape. Naked-short blocked by default.

Each builder is a pure function of ``BuilderContext``: instrument-master,
underlying, spot, expiry, premium oracle. Wing widths in *strike-step
counts*, not rupees — so a builder works the same on NIFTY (step 50)
as on TATAMOTORS (step 5).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from trading_platform.agent.market_hours import now_ist

from trading_platform.data.instrument_master import (
    EQUITY_FO_UNDERLYINGS,
    INDEX_UNDERLYINGS,
    InstrumentMaster,
)
from trading_platform.derivatives.option_legs import OptionLeg, OptionStrategyPlan
from trading_platform.domain.enums import OptionType, Segment, Side
from trading_platform.domain.models import Instrument


# ──────────────────────────────────────────────────────────────────────
# Premium oracle
# ──────────────────────────────────────────────────────────────────────

PremiumOracle = Callable[[Instrument, float], float]
"""Callable: ``(instrument, spot) → per-share premium``.

The builder doesn't care whether the oracle is a Black-Scholes
synthetic, a backtest stub, or a live Angel One option-chain reader.
That separation is the seam Phase 2 plugs the live oracle into.
"""


def synthetic_oracle(annual_vol: float = 0.18) -> PremiumOracle:
    """Brenner-Subrahmanyam ATM approximation, scaled by moneyness.

    Used by tests and backtests when no live chain is available.
    Returns a non-zero floor so zero-premium legs never enter the
    system.
    """

    def _oracle(instrument: Instrument, spot: float) -> float:
        strike = instrument.strike or spot
        expiry = instrument.expiry
        if expiry is None:
            dte_days = 7
        else:
            dte_days = max(1, (expiry - now_ist().date()).days)
        sigma = max(annual_vol, 0.10)
        atm = spot * sigma * math.sqrt(dte_days / 252.0) * 0.4
        # Decay away from ATM by exp(-((K-S)/(sigma*S))²/2). Cheap proxy.
        moneyness = (strike - spot) / max(spot * sigma, 1e-6)
        decay = math.exp(-0.5 * moneyness * moneyness)
        # CE has more value when spot > strike (already encoded by intrinsic).
        # We add intrinsic to time value.
        if instrument.option_type == OptionType.CE:
            intrinsic = max(spot - strike, 0.0)
        else:
            intrinsic = max(strike - spot, 0.0)
        return max(1.0, round(intrinsic + atm * decay, 2))

    return _oracle


# ──────────────────────────────────────────────────────────────────────
# BuilderContext
# ──────────────────────────────────────────────────────────────────────


def _resolve_strike_step(underlying: str, master: InstrumentMaster) -> int:
    """Strike step in rupees. Pulled from Angel One metadata when known,
    falls back to a heuristic based on observed strikes in the master.
    """
    if underlying in INDEX_UNDERLYINGS:
        return int(INDEX_UNDERLYINGS[underlying]["strike_step"])
    if underlying in EQUITY_FO_UNDERLYINGS:
        return int(EQUITY_FO_UNDERLYINGS[underlying]["strike_step"])
    options = [
        i.strike
        for i in master.by_underlying(underlying, Segment.OPTIONS)
        if i.strike is not None
    ]
    if len(options) >= 2:
        diffs = sorted({abs(a - b) for a in options for b in options if a != b})
        if diffs:
            return max(1, int(round(diffs[0])))
    return 50


def _resolve_lot_size(underlying: str, master: InstrumentMaster) -> int:
    if underlying in INDEX_UNDERLYINGS:
        return int(INDEX_UNDERLYINGS[underlying]["lot_size"])
    if underlying in EQUITY_FO_UNDERLYINGS:
        return int(EQUITY_FO_UNDERLYINGS[underlying]["lot_size"])
    derivs = master.by_underlying(underlying)
    for inst in derivs:
        if inst.lot_size and inst.lot_size > 1:
            return int(inst.lot_size)
    return 1


@dataclass
class BuilderContext:
    """Everything a builder needs to emit a plan."""

    master: InstrumentMaster
    underlying: str
    spot: float
    expiry: date
    oracle: PremiumOracle
    quantity: int = 1
    min_open_interest: int = 0
    max_spread_pct: float = 0.10
    far_expiry: date | None = None  # for calendars
    metadata: dict = field(default_factory=dict)

    def step(self) -> int:
        return _resolve_strike_step(self.underlying, self.master)

    def lot_size(self) -> int:
        return _resolve_lot_size(self.underlying, self.master)

    def atm(self) -> int:
        s = self.step()
        return int(round(self.spot / s) * s)

    def select(self, strike: int | float, option_type: OptionType,
               expiry: date | None = None) -> Instrument:
        """Resolve a leg to a real Instrument. Snaps to the closest
        strike that exists on the InstrumentMaster for the given
        expiry.
        """
        target_expiry = expiry or self.expiry
        candidates = [
            inst
            for inst in self.master.by_underlying(self.underlying, Segment.OPTIONS)
            if inst.expiry == target_expiry and inst.option_type == option_type
        ]
        if not candidates:
            # Fall back to nearest available expiry on or after target.
            future_expiries = sorted({
                i.expiry for i in self.master.by_underlying(self.underlying, Segment.OPTIONS)
                if i.expiry is not None and i.expiry >= target_expiry
            })
            if not future_expiries:
                raise ValueError(
                    f"No {option_type.value} options for {self.underlying} on/after {target_expiry}"
                )
            target_expiry = future_expiries[0]
            candidates = [
                inst
                for inst in self.master.by_underlying(self.underlying, Segment.OPTIONS)
                if inst.expiry == target_expiry and inst.option_type == option_type
            ]
        return min(candidates, key=lambda i: abs((i.strike or 0) - float(strike)))

    def leg(self, instrument: Instrument, side: Side, *, hedge: bool = False) -> OptionLeg:
        """Wrap an Instrument into an OptionLeg, pricing it via the oracle."""
        if instrument.expiry is None or instrument.strike is None or instrument.option_type is None:
            raise ValueError(
                f"Instrument {instrument.symbol} is not a complete option contract"
            )
        premium = self.oracle(instrument, self.spot)
        return OptionLeg(
            underlying=self.underlying,
            expiry=instrument.expiry,
            strike=float(instrument.strike),
            option_type=instrument.option_type,
            side=side,
            quantity=self.quantity,
            hedge=hedge,
            min_open_interest=self.min_open_interest or None,
            max_spread_pct=self.max_spread_pct,
            symbol=instrument.symbol,
            lot_size=instrument.lot_size or self.lot_size(),
            reference_premium=float(premium),
        )

    def select_far_expiry(self) -> date:
        """For calendar spreads. Returns ``self.far_expiry`` if set,
        else the next available option expiry strictly after
        ``self.expiry``.
        """
        if self.far_expiry is not None:
            return self.far_expiry
        future = sorted({
            i.expiry for i in self.master.by_underlying(self.underlying, Segment.OPTIONS)
            if i.expiry is not None and i.expiry > self.expiry
        })
        if not future:
            # Fabricate one ~30d out so the plan is at least well-formed.
            return self.expiry + timedelta(days=30)
        return future[0]


# ──────────────────────────────────────────────────────────────────────
# Builder protocol + registry
# ──────────────────────────────────────────────────────────────────────


class StrategyBuilder:
    """Base class. Override :py:meth:`build` and set ``structure``."""

    structure: str = ""
    allow_naked_short: bool = False
    requires_hedge: bool = True  # naked plans must opt out explicitly

    def build(self, ctx: BuilderContext) -> OptionStrategyPlan:  # pragma: no cover
        raise NotImplementedError


# ──────────────────────────────────────────────────────────────────────
# Concrete builders
# ──────────────────────────────────────────────────────────────────────


class LongStraddleBuilder(StrategyBuilder):
    structure = "long_straddle"

    def build(self, ctx: BuilderContext) -> OptionStrategyPlan:
        atm = ctx.atm()
        ce = ctx.select(atm, OptionType.CE)
        pe = ctx.select(atm, OptionType.PE)
        legs = (
            ctx.leg(ce, Side.BUY),
            ctx.leg(pe, Side.BUY),
        )
        return OptionStrategyPlan(
            strategy_name="long_straddle",
            structure=self.structure,
            underlying=ctx.underlying,
            legs=legs,
            spot_at_creation=ctx.spot,
            allow_naked_short=False,
            metadata={"atm": atm},
        )


class LongStrangleBuilder(StrategyBuilder):
    structure = "long_strangle"

    def __init__(self, wing_steps: int = 2) -> None:
        self.wing_steps = wing_steps

    def build(self, ctx: BuilderContext) -> OptionStrategyPlan:
        s, atm = ctx.step(), ctx.atm()
        ce = ctx.select(atm + s * self.wing_steps, OptionType.CE)
        pe = ctx.select(atm - s * self.wing_steps, OptionType.PE)
        legs = (
            ctx.leg(ce, Side.BUY),
            ctx.leg(pe, Side.BUY),
        )
        return OptionStrategyPlan(
            strategy_name="long_strangle",
            structure=self.structure,
            underlying=ctx.underlying,
            legs=legs,
            spot_at_creation=ctx.spot,
            allow_naked_short=False,
            metadata={"wing_steps": self.wing_steps},
        )


class ShortStraddleBuilder(StrategyBuilder):
    """Income-on-stable. **Requires explicit naked opt-in.**"""

    structure = "short_straddle"
    allow_naked_short = True

    def build(self, ctx: BuilderContext) -> OptionStrategyPlan:
        atm = ctx.atm()
        ce = ctx.select(atm, OptionType.CE)
        pe = ctx.select(atm, OptionType.PE)
        legs = (
            ctx.leg(ce, Side.SELL),
            ctx.leg(pe, Side.SELL),
        )
        return OptionStrategyPlan(
            strategy_name="short_straddle",
            structure=self.structure,
            underlying=ctx.underlying,
            legs=legs,
            spot_at_creation=ctx.spot,
            allow_naked_short=ctx.metadata.get("allow_naked_short", False),
            metadata={"atm": atm, "warning": "naked short — set allow_naked_short=True to ship"},
        )


class IronCondorBuilder(StrategyBuilder):
    """Defined-risk income substitute for short straddle.

    Hedge legs are flagged ``hedge=True`` so the
    ``MultiLegOrderManager`` fills wings before body — partial fills
    leave the position *more* protected, not less.
    """

    structure = "iron_condor"

    def __init__(self, body_steps: int = 2, wing_steps: int = 4) -> None:
        if wing_steps <= body_steps:
            raise ValueError("iron_condor: wing_steps must exceed body_steps")
        self.body_steps = body_steps
        self.wing_steps = wing_steps

    def build(self, ctx: BuilderContext) -> OptionStrategyPlan:
        s, atm = ctx.step(), ctx.atm()
        sce = ctx.select(atm + s * self.body_steps, OptionType.CE)
        lce = ctx.select(atm + s * self.wing_steps, OptionType.CE)
        spe = ctx.select(atm - s * self.body_steps, OptionType.PE)
        lpe = ctx.select(atm - s * self.wing_steps, OptionType.PE)
        legs = (
            # Hedges first so the multi-leg manager fills protection before risk.
            ctx.leg(lce, Side.BUY, hedge=True),
            ctx.leg(lpe, Side.BUY, hedge=True),
            ctx.leg(sce, Side.SELL),
            ctx.leg(spe, Side.SELL),
        )
        return OptionStrategyPlan(
            strategy_name="iron_condor",
            structure=self.structure,
            underlying=ctx.underlying,
            legs=legs,
            spot_at_creation=ctx.spot,
            allow_naked_short=False,
            metadata={"body_steps": self.body_steps, "wing_steps": self.wing_steps},
        )


class BullCallSpreadBuilder(StrategyBuilder):
    structure = "bull_call_spread"

    def __init__(self, width_steps: int = 2) -> None:
        self.width_steps = width_steps

    def build(self, ctx: BuilderContext) -> OptionStrategyPlan:
        s, atm = ctx.step(), ctx.atm()
        long_ce = ctx.select(atm, OptionType.CE)
        short_ce = ctx.select(atm + s * self.width_steps, OptionType.CE)
        legs = (
            ctx.leg(long_ce, Side.BUY, hedge=True),
            ctx.leg(short_ce, Side.SELL),
        )
        return OptionStrategyPlan(
            strategy_name="bull_call_spread",
            structure=self.structure,
            underlying=ctx.underlying,
            legs=legs,
            spot_at_creation=ctx.spot,
            allow_naked_short=False,
            metadata={"width_steps": self.width_steps},
        )


class BearPutSpreadBuilder(StrategyBuilder):
    structure = "bear_put_spread"

    def __init__(self, width_steps: int = 2) -> None:
        self.width_steps = width_steps

    def build(self, ctx: BuilderContext) -> OptionStrategyPlan:
        s, atm = ctx.step(), ctx.atm()
        long_pe = ctx.select(atm, OptionType.PE)
        short_pe = ctx.select(atm - s * self.width_steps, OptionType.PE)
        legs = (
            ctx.leg(long_pe, Side.BUY, hedge=True),
            ctx.leg(short_pe, Side.SELL),
        )
        return OptionStrategyPlan(
            strategy_name="bear_put_spread",
            structure=self.structure,
            underlying=ctx.underlying,
            legs=legs,
            spot_at_creation=ctx.spot,
            allow_naked_short=False,
            metadata={"width_steps": self.width_steps},
        )


class CalendarSpreadBuilder(StrategyBuilder):
    """Theta capture: short near-month, long far-month, both ATM."""

    structure = "calendar_spread"

    def build(self, ctx: BuilderContext) -> OptionStrategyPlan:
        atm = ctx.atm()
        far_expiry = ctx.select_far_expiry()
        if far_expiry == ctx.expiry:
            raise ValueError(
                f"calendar_spread: near and far expiries must differ; both={ctx.expiry}"
            )
        near_ce = ctx.select(atm, OptionType.CE, expiry=ctx.expiry)
        far_ce = ctx.select(atm, OptionType.CE, expiry=far_expiry)
        legs = (
            # Long-dated leg is the protector → hedge first.
            ctx.leg(far_ce, Side.BUY, hedge=True),
            ctx.leg(near_ce, Side.SELL),
        )
        return OptionStrategyPlan(
            strategy_name="calendar_spread",
            structure=self.structure,
            underlying=ctx.underlying,
            legs=legs,
            spot_at_creation=ctx.spot,
            allow_naked_short=False,
            metadata={"near_expiry": ctx.expiry.isoformat(), "far_expiry": far_expiry.isoformat()},
        )


class DeltaNeutralHedgeBuilder(StrategyBuilder):
    """Long straddle with a separate underlying-futures hedge held
    alongside. Phase 1 emits the option legs; the futures hedge is
    expressed via a metadata flag for the execution layer to consume
    in Phase 2.
    """

    structure = "delta_neutral_hedge"

    def build(self, ctx: BuilderContext) -> OptionStrategyPlan:
        atm = ctx.atm()
        ce = ctx.select(atm, OptionType.CE)
        pe = ctx.select(atm, OptionType.PE)
        legs = (
            ctx.leg(ce, Side.BUY),
            ctx.leg(pe, Side.BUY),
        )
        return OptionStrategyPlan(
            strategy_name="delta_neutral_hedge",
            structure=self.structure,
            underlying=ctx.underlying,
            legs=legs,
            spot_at_creation=ctx.spot,
            allow_naked_short=False,
            metadata={"requires_futures_hedge": True, "atm": atm},
        )


# ──────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────


_REGISTRY: dict[str, StrategyBuilder] = {
    "long_straddle": LongStraddleBuilder(),
    "long_strangle": LongStrangleBuilder(),
    "short_straddle": ShortStraddleBuilder(),
    "iron_condor": IronCondorBuilder(),
    "bull_call_spread": BullCallSpreadBuilder(),
    "bear_put_spread": BearPutSpreadBuilder(),
    "calendar_spread": CalendarSpreadBuilder(),
    "delta_neutral_hedge": DeltaNeutralHedgeBuilder(),
}


def get_builder(structure: str) -> StrategyBuilder:
    try:
        return _REGISTRY[structure]
    except KeyError as exc:
        raise KeyError(
            f"Unknown option structure '{structure}'. Available: {sorted(_REGISTRY)}"
        ) from exc


def available_structures() -> list[str]:
    return sorted(_REGISTRY)


def build_plan(structure: str, ctx: BuilderContext) -> OptionStrategyPlan:
    """Convenience: look up + invoke a builder by structure name."""
    return get_builder(structure).build(ctx)


__all__ = [
    "PremiumOracle",
    "synthetic_oracle",
    "BuilderContext",
    "StrategyBuilder",
    "LongStraddleBuilder",
    "LongStrangleBuilder",
    "ShortStraddleBuilder",
    "IronCondorBuilder",
    "BullCallSpreadBuilder",
    "BearPutSpreadBuilder",
    "CalendarSpreadBuilder",
    "DeltaNeutralHedgeBuilder",
    "get_builder",
    "available_structures",
    "build_plan",
]
