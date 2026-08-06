"""Portfolio-level option Greeks aggregation — REDESIGN_PROMPT.md §6.1's
"portfolio Greeks caps (net delta, vega, gamma-near-expiry)" gap.

Confirmed 2026-08-06 by auditing the whole codebase: `RiskEngine` has a
`gamma_exposure` parameter and a `max_gamma_near_expiry` threshold, but
nothing anywhere computed real portfolio gamma to feed it — every actual
order-submission path (`execution/router.py`, `decision/pipeline.py`) left
it at its default 0.0, so the check could never fire. There was no delta or
vega aggregation at all; `GreeksCalculator` was only ever used for
theoretical single-contract pricing (chain display, IV-surface synthesis),
never against the real open book.

This module closes the aggregation half of that gap: given the live
portfolio and a way to price the underlying + each option's own current
mark, it computes real per-position Greeks (implied vol inverted from the
position's OWN market price, the same "price real risk off the market's own
implied vol, never an assumed constant" discipline ShortVolExecutor already
follows for entries) and sums them into net portfolio delta/gamma/theta/vega.

This full-book aggregate (TradingRuntime.portfolio_greeks_snapshot) is
read-only/diagnostic and does not gate order flow. A narrower slice of it —
net gamma of just the near-expiry (<=1 DTE) positions — does optionally
feed RiskEngine's real order-blocking gate, but only behind
Settings.enable_gamma_exposure_gate (default off): see
TradingRuntime._near_expiry_gamma_exposure and that setting's docstring for
why enabling it needs the threshold checked against real data first.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from trading_platform.derivatives.engine import GreeksCalculator, ImpliedVolatilityCalculator
from trading_platform.domain.enums import Segment
from trading_platform.domain.models import Position

SpotPriceFn = Callable[[str], float | None]
MarkPriceFn = Callable[[Position], float | None]


@dataclass(frozen=True)
class PositionGreeks:
    symbol: str
    underlying: str
    quantity: int
    days_to_expiry: int
    implied_vol: float   # vol points (%), inverted from the position's own mark
    delta: float          # per-contract (one option, not lot-adjusted)
    gamma: float
    theta: float
    vega: float
    net_delta: float      # delta * quantity * lot_size — signed by position direction
    net_gamma: float
    net_theta: float
    net_vega: float


@dataclass(frozen=True)
class PortfolioGreeksSnapshot:
    positions: tuple[PositionGreeks, ...]
    net_delta: float
    net_gamma: float
    net_theta: float
    net_vega: float
    # Symbols we hold but couldn't price (no spot, no mark, or IV inversion
    # failed) — surfaced explicitly so a snapshot with gaps is never
    # mistaken for "this book has no risk," per this project's rule against
    # silently swallowing failures.
    skipped: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "net_delta": round(self.net_delta, 2),
            "net_gamma": round(self.net_gamma, 4),
            "net_theta": round(self.net_theta, 2),
            "net_vega": round(self.net_vega, 2),
            "position_count": len(self.positions),
            "skipped_count": len(self.skipped),
            "skipped": list(self.skipped),
            "positions": [
                {
                    "symbol": p.symbol, "underlying": p.underlying, "quantity": p.quantity,
                    "days_to_expiry": p.days_to_expiry, "implied_vol": p.implied_vol,
                    "delta": round(p.delta, 4), "gamma": round(p.gamma, 6),
                    "theta": round(p.theta, 4), "vega": round(p.vega, 4),
                    "net_delta": round(p.net_delta, 2), "net_gamma": round(p.net_gamma, 4),
                    "net_theta": round(p.net_theta, 2), "net_vega": round(p.net_vega, 2),
                }
                for p in self.positions
            ],
        }


class PortfolioGreeksCalculator:
    """Aggregates live per-position Greeks across all open OPTION positions.

    Two price sources are injected as callables so this stays testable and
    decoupled from the runtime:
      - `spot_price(underlying) -> float | None` — the underlying's current price.
      - `mark_price(position) -> float | None` — the option contract's own
        current mark, used to invert implied vol per-position rather than
        assume a fixed vol (the same approach ShortVolExecutor's
        `_atm_iv_and_lot` already uses for entries).

    Non-option positions (equities, futures) are skipped entirely — Greeks
    aggregation is meaningless for them; they simply don't appear in the
    snapshot (not counted as "skipped", since that's reserved for options
    this calculator tried and failed to price).
    """

    def __init__(self) -> None:
        self._greeks_calc = GreeksCalculator()
        self._iv_calc = ImpliedVolatilityCalculator()

    def compute(
        self,
        positions: dict[str, Position],
        spot_price: SpotPriceFn,
        mark_price: MarkPriceFn,
        as_of: date | None = None,
    ) -> PortfolioGreeksSnapshot:
        today = as_of or date.today()
        rows: list[PositionGreeks] = []
        skipped: list[str] = []
        for pos in positions.values():
            if pos.quantity == 0:
                continue
            inst = pos.instrument
            if (
                inst.segment != Segment.OPTIONS
                or inst.strike is None
                or inst.expiry is None
                or inst.option_type is None
            ):
                continue
            underlying = (inst.underlying or "").strip().upper()
            spot = spot_price(underlying) if underlying else None
            mark = mark_price(pos)
            if not spot or spot <= 0 or not mark or mark <= 0:
                skipped.append(inst.symbol)
                continue
            dte = max((inst.expiry - today).days, 1)
            try:
                iv = self._iv_calc.calculate(mark, spot, float(inst.strike), dte, inst.option_type)
                if not (0.01 < iv < 3.0):
                    skipped.append(inst.symbol)
                    continue
                greeks = self._greeks_calc.calculate(spot, float(inst.strike), dte, iv, inst.option_type)
            except Exception:
                skipped.append(inst.symbol)
                continue
            multiplier = pos.quantity * inst.lot_size
            rows.append(PositionGreeks(
                symbol=inst.symbol, underlying=underlying, quantity=pos.quantity,
                days_to_expiry=dte, implied_vol=round(iv * 100.0, 2),
                delta=greeks.delta, gamma=greeks.gamma, theta=greeks.theta, vega=greeks.vega,
                net_delta=greeks.delta * multiplier, net_gamma=greeks.gamma * multiplier,
                net_theta=greeks.theta * multiplier, net_vega=greeks.vega * multiplier,
            ))
        return PortfolioGreeksSnapshot(
            positions=tuple(rows),
            net_delta=sum(r.net_delta for r in rows),
            net_gamma=sum(r.net_gamma for r in rows),
            net_theta=sum(r.net_theta for r in rows),
            net_vega=sum(r.net_vega for r in rows),
            skipped=tuple(skipped),
        )
