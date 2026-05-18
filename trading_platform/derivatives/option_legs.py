"""Phase-1 option-leg domain model.

Two frozen dataclasses, one validator. Everything downstream — risk,
execution, monitoring — gets to ask one type one set of questions.

The strategy layer used to emit a single ``OrderIntent`` and call it a
"straddle." Now strategies emit an ``OptionStrategyPlan`` whose legs
are typed, whose max-loss is computed over a payoff grid, and whose
naked-short legs are blocked by default.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from trading_platform.agent.market_hours import now_ist
from trading_platform.domain.enums import OptionType, Side


# ──────────────────────────────────────────────────────────────────────
# OptionLeg
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OptionLeg:
    """A single leg of an option strategy.

    ``symbol`` and ``lot_size`` are resolved by the builder when it
    looks the leg up against the InstrumentMaster — they may be ``None``
    on hand-constructed legs (tests, validators).

    ``reference_premium`` is the per-share quote used for net-premium
    and max-loss arithmetic. The builder seeds it from the
    ``PremiumOracle``; live execution refreshes it before submission.
    """

    underlying: str
    expiry: date
    strike: float
    option_type: OptionType
    side: Side
    quantity: int
    hedge: bool = False
    min_open_interest: int | None = None
    max_spread_pct: float | None = None
    symbol: str | None = None
    lot_size: int | None = None
    reference_premium: float | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"OptionLeg.quantity must be > 0, got {self.quantity}")
        if self.strike <= 0:
            raise ValueError(f"OptionLeg.strike must be > 0, got {self.strike}")
        if self.reference_premium is not None and self.reference_premium < 0:
            raise ValueError(f"reference_premium must be ≥ 0, got {self.reference_premium}")
        if self.lot_size is not None and self.lot_size <= 0:
            raise ValueError(f"lot_size must be > 0, got {self.lot_size}")

    @property
    def is_short(self) -> bool:
        return self.side == Side.SELL

    @property
    def is_long(self) -> bool:
        return self.side == Side.BUY

    def payoff_at(self, spot: float) -> float:
        """Per-share P&L of this leg at expiry given underlying spot.

        Excludes premium; total P&L = payoff_at(spot) - reference_premium
        for long, or reference_premium - payoff_at(spot) for short.
        Multiplied by quantity × lot_size by callers that need rupee P&L.
        """
        if self.option_type == OptionType.CE:
            intrinsic = max(spot - self.strike, 0.0)
        else:
            intrinsic = max(self.strike - spot, 0.0)
        return intrinsic if self.is_long else -intrinsic

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": self.option_type.value,
            "side": self.side.value,
            "quantity": self.quantity,
            "hedge": self.hedge,
            "min_open_interest": self.min_open_interest,
            "max_spread_pct": self.max_spread_pct,
            "symbol": self.symbol,
            "lot_size": self.lot_size,
            "reference_premium": self.reference_premium,
        }


# ──────────────────────────────────────────────────────────────────────
# OptionStrategyPlan
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OptionStrategyPlan:
    """A multi-leg option strategy. Emits as a single object so the
    strategy/risk/execution boundary stops talking single legs.
    """

    strategy_name: str
    structure: str
    underlying: str
    legs: tuple[OptionLeg, ...]
    spot_at_creation: float
    allow_naked_short: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.legs) < 2:
            raise ValueError(
                f"OptionStrategyPlan needs ≥ 2 legs, got {len(self.legs)} "
                f"for {self.strategy_name}"
            )
        if self.spot_at_creation <= 0:
            raise ValueError(f"spot_at_creation must be > 0, got {self.spot_at_creation}")
        # Every leg must agree on underlying with the plan.
        for leg in self.legs:
            if leg.underlying != self.underlying:
                raise ValueError(
                    f"Leg underlying '{leg.underlying}' does not match plan "
                    f"underlying '{self.underlying}' in {self.strategy_name}"
                )

    # ── Inspection ────────────────────────────────────────────────────

    @property
    def short_legs(self) -> tuple[OptionLeg, ...]:
        return tuple(leg for leg in self.legs if leg.is_short)

    @property
    def long_legs(self) -> tuple[OptionLeg, ...]:
        return tuple(leg for leg in self.legs if leg.is_long)

    @property
    def is_calendar(self) -> bool:
        """True iff legs span ≥ 2 distinct expiries."""
        return len({leg.expiry for leg in self.legs}) >= 2

    @property
    def has_naked_short(self) -> bool:
        """A short leg is "naked" iff no same-type, same-expiry-or-later
        long protector covers it with ≥ the short quantity.
        """
        for short in self.short_legs:
            covering = sum(
                long.quantity
                for long in self.long_legs
                if long.option_type == short.option_type
                and long.expiry >= short.expiry
                and self._covers(long, short)
            )
            if covering < short.quantity:
                return True
        return False

    @staticmethod
    def _covers(long: OptionLeg, short: OptionLeg) -> bool:
        """A long leg of the same option type and same-or-later expiry
        bounds the loss of a short leg. Strike position changes the
        size of the bounded loss but not its boundedness:

          short CE @ Ks loses (S − Ks) as S → ∞
          long  CE @ Kl pays  (S − Kl) for S > Kl
          net at S → ∞: (S − Kl) − (S − Ks) = Ks − Kl, bounded.

        Symmetrically for PE as S → 0. Calendars (different expiries)
        get their own conservative path in compute_max_loss.
        """
        return (
            long.option_type == short.option_type
            and long.expiry >= short.expiry
        )

    # ── Premium and risk arithmetic ───────────────────────────────────

    def net_premium(self) -> float:
        """Net premium per lot of the strategy. Positive = debit (we pay),
        negative = credit (we receive). Legs without a reference premium
        contribute 0 — see ``has_complete_pricing``.
        """
        total = 0.0
        for leg in self.legs:
            if leg.reference_premium is None:
                continue
            sign = 1 if leg.is_long else -1
            total += sign * leg.reference_premium * leg.quantity
        return total

    @property
    def has_complete_pricing(self) -> bool:
        return all(leg.reference_premium is not None for leg in self.legs)

    def compute_max_loss(self, spot: float | None = None) -> float:
        """Worst per-strategy P&L at expiry over a spot grid.

        Returns ``math.inf`` when the plan has unbounded loss (any
        unprotected short leg). Otherwise: minimum P&L (in rupees,
        per unit ``quantity`` already baked in via leg.quantity ×
        leg.lot_size). Sign: returns a *positive* loss number, i.e.
        ``150_000`` means "max loss is ₹150,000".

        For calendars, falls back to net debit (the legs do not all
        expire on the same day, so an at-expiry payoff diagram is not
        defined — net debit is the conservative cap).
        """
        if self.has_naked_short and not self.allow_naked_short:
            return math.inf
        if not self.has_complete_pricing:
            # No reference premium → can't price the structure at all.
            return math.inf
        if self.is_calendar:
            net = self.net_premium()
            # Multiply by lot size (use the largest leg's lot_size as a
            # proxy; calendars share underlying so all legs share lot).
            lot = self._effective_lot_size()
            return max(0.0, net * lot)

        spot_anchor = spot if spot is not None else self.spot_at_creation
        grid = self._payoff_grid(spot_anchor)
        worst = min(self._pnl_at(s) for s in grid)
        return -worst if worst < 0 else 0.0

    def estimate_margin(self) -> float:
        """A pre-check estimate of margin. Broker-side
        (Angel One ``/margin``) remains source of truth. Cushion is
        20% over computed max-loss to cover slippage and bid-ask.
        """
        loss = self.compute_max_loss()
        if math.isinf(loss):
            return math.inf
        return loss * 1.20

    # ── Internal helpers ──────────────────────────────────────────────

    def _payoff_grid(self, spot: float) -> list[float]:
        """Spot grid: 0.5x to 1.5x spot in 200 steps + every strike (kinks).

        Kinks at strikes are where the payoff is non-differentiable —
        a fine-resolution grid that happens to skip a strike will miss
        the worst point. Including strikes guarantees correctness.
        """
        lo = max(spot * 0.5, 0.01)
        hi = spot * 1.5
        steps = 200
        step_size = (hi - lo) / steps
        grid = [lo + i * step_size for i in range(steps + 1)]
        # Add every leg strike so we never skip a kink.
        grid.extend(leg.strike for leg in self.legs)
        return sorted(set(grid))

    def _pnl_at(self, spot: float) -> float:
        """Total rupee P&L at the given spot (includes premium debit/credit)."""
        total = 0.0
        for leg in self.legs:
            lot = leg.lot_size if leg.lot_size is not None else self._effective_lot_size()
            payoff = leg.payoff_at(spot)
            premium_cost = 0.0
            if leg.reference_premium is not None:
                # Long pays the premium (subtract); short collects (add).
                sign = -1 if leg.is_long else 1
                premium_cost = sign * leg.reference_premium
            per_share = payoff + premium_cost
            total += per_share * leg.quantity * lot
        return total

    def _effective_lot_size(self) -> int:
        for leg in self.legs:
            if leg.lot_size is not None:
                return leg.lot_size
        return 1

    # ── Serialisation ─────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        loss = self.compute_max_loss()
        margin = self.estimate_margin()
        return {
            "strategy_name": self.strategy_name,
            "structure": self.structure,
            "underlying": self.underlying,
            "spot_at_creation": self.spot_at_creation,
            "allow_naked_short": self.allow_naked_short,
            "is_calendar": self.is_calendar,
            "has_naked_short": self.has_naked_short,
            "net_premium": self.net_premium() if self.has_complete_pricing else None,
            "max_loss": "INFINITE" if math.isinf(loss) else round(loss, 2),
            "estimated_margin": "INFINITE" if math.isinf(margin) else round(margin, 2),
            "legs": [leg.to_dict() for leg in self.legs],
            "metadata": dict(self.metadata),
        }


# ──────────────────────────────────────────────────────────────────────
# Validator
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlanValidation:
    ok: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reasons": list(self.reasons)}


def validate_plan(
    plan: OptionStrategyPlan,
    *,
    max_allowed_loss: float | None = None,
    min_days_to_expiry: int = 0,
    as_of: date | None = None,
) -> PlanValidation:
    """Collect every reason a plan should be rejected.

    Design rule: never short-circuit. Operators want the full list of
    failures, not just the first one. The risk engine consumes the
    full list to decide policy (one reason → maybe a warning; two →
    block).
    """
    reasons: list[str] = []

    if plan.has_naked_short and not plan.allow_naked_short:
        reasons.append("naked_short_blocked")

    if not plan.has_complete_pricing:
        reasons.append("missing_reference_premium")

    loss = plan.compute_max_loss()
    if math.isinf(loss):
        reasons.append("unbounded_loss")
    elif max_allowed_loss is not None and loss > max_allowed_loss:
        reasons.append(f"max_loss_exceeds_cap:{round(loss, 2)}>{round(max_allowed_loss, 2)}")

    if min_days_to_expiry > 0:
        anchor = as_of or now_ist().date()
        for leg in plan.legs:
            dte = (leg.expiry - anchor).days
            if dte < min_days_to_expiry:
                reasons.append(f"expiry_too_close:{leg.symbol or leg.strike}:{dte}d")
                break  # one is enough; don't spam reasons

    if plan.structure == "calendar_spread" and not plan.is_calendar:
        reasons.append("calendar_requires_distinct_expiries")

    if plan.is_calendar and plan.structure not in {"calendar_spread", "diagonal_spread"}:
        reasons.append("multi_expiry_in_non_calendar_structure")

    return PlanValidation(ok=not reasons, reasons=tuple(reasons))


__all__ = [
    "OptionLeg",
    "OptionStrategyPlan",
    "PlanValidation",
    "validate_plan",
]
