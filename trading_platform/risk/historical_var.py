"""Historical-simulation VaR on the options book — REDESIGN_PROMPT.md
§6.1's remaining explicit gap (confirmed 2026-08-06: no VaR of any kind,
historical or parametric, existed anywhere in the repo — the only
tail-risk metric, CVaR in validation/monte_carlo.py, belongs to the
strategy-promotion backtest validator, not the live book).

Methodology: historical simulation, not parametric. Each of the last N
trading days' ACTUAL log returns for every underlying in the book is
applied to today's spot, every open option position is repriced at that
shocked spot (Black-Scholes, same strike/DTE/IV as now), and the whole
book's P&L is summed per scenario. VaR is the desired percentile of the
resulting P&L distribution. No assumption that returns are normally
distributed — whatever actually happened (fat tails, skew, the 2020-style
gap this codebase's own short-vol docstring warns the backtest sample
never saw) shows up directly in the scenario set, which is the entire
point of historical over parametric VaR.

Scenarios are aligned by "N trading days ago" across underlyings (not by
calendar date) — a deliberate simplification that holds because every
underlying here is an NSE/BSE index derivative sharing the same trading
calendar, so the Nth-most-recent trading day is the same calendar day for
all of them. This makes a market-wide stress day (e.g. a crash) hit every
position in the same scenario together, correctly capturing correlation
instead of treating each underlying's tail risk as independent.

Scope decision (documented, not hidden): shocks apply to SPOT only, not
to implied vol. A joint spot+IV historical shock (using each day's actual
realized IV change too, not just its price return) would capture vega risk
under stress more completely and is a legitimate future enhancement —
today's number understates tail risk to the extent IV itself would have
spiked alongside the price move (which for short-vol books, it would).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Callable

from trading_platform.derivatives.engine import ImpliedVolatilityCalculator, black_scholes_price
from trading_platform.domain.enums import Segment
from trading_platform.domain.models import Position

SpotPriceFn = Callable[[str], float | None]
MarkPriceFn = Callable[[Position], float | None]
HistoricalReturnsFn = Callable[[str], list[float]]

# Below this many aligned scenarios, a percentile is noise, not signal — same
# discipline as derivatives.engine.MIN_IV_RANK_OBSERVATIONS. VaR conventionally
# wants ~252 trading days; this is only the floor below which the result is
# refused outright, not the recommended lookback.
MIN_VAR_OBSERVATIONS = 20


@dataclass(frozen=True)
class HistoricalVarResult:
    var_95: float          # 95% 1-day historical VaR, rupees — a positive number is a potential loss
    var_99: float
    scenario_count: int
    worst_case_pnl: float   # most negative scenario P&L (can exceed var_99 in a real fat tail)
    best_case_pnl: float
    priced_position_count: int
    skipped: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "available": True,
            "var_95": round(self.var_95, 2),
            "var_99": round(self.var_99, 2),
            "scenario_count": self.scenario_count,
            "worst_case_pnl": round(self.worst_case_pnl, 2),
            "best_case_pnl": round(self.best_case_pnl, 2),
            "priced_position_count": self.priced_position_count,
            "skipped_count": len(self.skipped),
            "skipped": list(self.skipped),
        }


class HistoricalVarCalculator:
    """See module docstring for methodology. Two price sources plus a
    historical-returns source are injected as callables, same pattern as
    PortfolioGreeksCalculator, so this stays testable and decoupled from
    the runtime."""

    def __init__(self) -> None:
        self._iv_calc = ImpliedVolatilityCalculator()

    def compute(
        self,
        positions: dict[str, Position],
        spot_price: SpotPriceFn,
        mark_price: MarkPriceFn,
        historical_returns: HistoricalReturnsFn,
        as_of: date | None = None,
    ) -> HistoricalVarResult | None:
        today = as_of or date.today()
        priced: list[tuple] = []  # (underlying, spot, strike, T, iv, option_type, multiplier, current_price)
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
            except Exception:
                skipped.append(inst.symbol)
                continue
            priced.append((
                underlying, spot, float(inst.strike), dte / 365.0, iv, inst.option_type,
                pos.quantity * inst.lot_size, mark,
            ))

        if not priced:
            return None

        returns_by_underlying: dict[str, list[float]] = {}
        for underlying, *_ in priced:
            if underlying not in returns_by_underlying:
                returns_by_underlying[underlying] = list(historical_returns(underlying) or [])

        usable_lengths = [len(r) for r in returns_by_underlying.values() if r]
        if not usable_lengths:
            return None
        n = min(usable_lengths)
        if n < MIN_VAR_OBSERVATIONS:
            return None

        scenario_pnls: list[float] = []
        for i in range(n):
            total_pnl = 0.0
            for underlying, spot, strike, t, iv, option_type, multiplier, current_price in priced:
                returns = returns_by_underlying[underlying]
                # Align from the end: index -(n - i) is "the same trading day
                # i steps back" across every underlying's own series, even if
                # one has more total history than another.
                log_return = returns[len(returns) - n + i]
                shocked_spot = spot * math.exp(log_return)
                shocked_price = black_scholes_price(shocked_spot, strike, t, iv, option_type)
                total_pnl += (shocked_price - current_price) * multiplier
            scenario_pnls.append(total_pnl)

        scenario_pnls.sort()
        idx_95 = int(0.05 * len(scenario_pnls))
        idx_99 = int(0.01 * len(scenario_pnls))
        return HistoricalVarResult(
            var_95=-scenario_pnls[idx_95],
            var_99=-scenario_pnls[idx_99],
            scenario_count=len(scenario_pnls),
            worst_case_pnl=scenario_pnls[0],
            best_case_pnl=scenario_pnls[-1],
            priced_position_count=len(priced),
            skipped=tuple(skipped),
        )
