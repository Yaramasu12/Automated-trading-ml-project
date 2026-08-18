"""Portfolio-level short-vol book — REDESIGN_PROMPT.md §4.5 (allocator).

WHY THIS EXISTS (measured, not assumed)
---------------------------------------
The single-slot backtest in `short_vol_backtest.py` returned ~2-3%/yr on NIFTY
over 2019-2026. Diagnosing that number rather than tuning it showed the cause
is **capital utilization, not a weak edge**:

    return per trade on capital risked : +2.2%   (84.6% win rate)
    capital risked per trade           :  4.55% of equity
    time in market                     : 26%     (one slot, Mondays only)
    => effective utilization           : ~1.2% of capital

An edge that wins 2.2% per trade is healthy. Earning 2-3%/yr from it while
98.8% of capital sits idle is an *architecture* limitation, and unlike a weak
edge it is legitimately fixable: run the same validated strategy across several
underlyings and expiry slots at once, which is how a real premium-selling book
operates.

THE RISK THIS INTRODUCES — STATED PLAINLY
------------------------------------------
Short-vol positions are **highly correlated**. In a volatility shock every slot
loses at once, so N concurrent slots is NOT N independent bets; it behaves far
closer to one bet at N times the size. Scaling utilization scales the left tail
essentially linearly, and a fat left tail is exactly what destroys retail
short-vol accounts (Feb-2018, Mar-2020).

Two things keep that honest here:

1. `max_portfolio_risk_pct` caps **total simultaneous defined risk** across the
   whole book, not each slot in isolation — REDESIGN §4.5's "cap combined vega,
   not just per-strategy notional". Every structure stays defined-risk, so the
   book's worst case is bounded and knowable.
2. The Monte-Carlo drawdown gate is the referee. Push utilization too far and
   the 95% max-DD estimate breaches its limit and the gate FAILS. That is the
   design working, not an obstacle to route around — if a configuration only
   reaches a return target by breaching the drawdown gate, it has not earned
   promotion.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from trading_platform.backtesting.short_vol_backtest import (
    DEFAULT_SPREAD_POINTS,
    TRADING_DAYS,
    DailyBar,
    ShortVolBacktester,
    ShortVolTrade,
)
from trading_platform.strategies.short_vol import ShortVolStrategy

logger = logging.getLogger(__name__)


@dataclass
class PortfolioBacktestResult:
    starting_capital: float
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    trades: list[ShortVolTrade] = field(default_factory=list)
    params: dict[str, float] = field(default_factory=dict)
    peak_concurrent: int = 0
    peak_risk_pct: float = 0.0

    @property
    def trade_pnls(self) -> list[float]:
        return [t.pnl for t in self.trades]

    @property
    def total_charges(self) -> float:
        return sum(t.charges for t in self.trades)

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.starting_capital

    @property
    def daily_returns(self) -> list[float]:
        vals = [v for _, v in self.equity_curve]
        return [
            (vals[i] - vals[i - 1]) / vals[i - 1]
            for i in range(1, len(vals))
            if vals[i - 1] > 0
        ]

    @property
    def max_drawdown(self) -> float:
        peak, mdd = -float("inf"), 0.0
        for _, v in self.equity_curve:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak)
        return mdd

    def cagr(self) -> float:
        if not self.equity_curve or self.starting_capital <= 0:
            return 0.0
        years = (self.equity_curve[-1][0] - self.equity_curve[0][0]).days / 365.25
        if years <= 0 or self.final_equity <= 0:
            return 0.0
        return (self.final_equity / self.starting_capital) ** (1 / years) - 1

    def to_dict(self) -> dict:
        wins = [t for t in self.trades if t.pnl > 0]
        return {
            "params": self.params,
            "trades": len(self.trades),
            "win_rate": (len(wins) / len(self.trades)) if self.trades else 0.0,
            "net_pnl": round(self.final_equity - self.starting_capital, 2),
            "total_charges": round(self.total_charges, 2),
            "final_equity": round(self.final_equity, 2),
            "cagr": round(self.cagr(), 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "peak_concurrent_positions": self.peak_concurrent,
            "peak_portfolio_risk_pct": round(self.peak_risk_pct, 4),
        }


class ShortVolPortfolioBacktester:
    """Runs the SAME validated `ShortVolStrategy.decide()` across several
    underlyings and expiry slots concurrently, under one portfolio risk cap."""

    def __init__(
        self,
        *,
        underlyings: list[str] | None = None,
        starting_capital: float = 1_000_000.0,
        slots_per_underlying: int = 2,
        max_portfolio_risk_pct: float = 0.25,
        continuous_entry: bool = True,
        strategy_params: dict[str, float] | None = None,
        hold_days: int = 5,
        spread_points: float = DEFAULT_SPREAD_POINTS,
    ) -> None:
        self.underlyings = underlyings or ["NIFTY", "BANKNIFTY", "FINNIFTY"]
        self.starting_capital = starting_capital
        self.slots_per_underlying = slots_per_underlying
        self.max_portfolio_risk_pct = max_portfolio_risk_pct
        self.continuous_entry = continuous_entry
        self.hold_days = hold_days
        self.spread_points = spread_points
        self.strategy_params = strategy_params or {}

    def _engine(self, underlying: str) -> ShortVolBacktester:
        p = self.strategy_params
        return ShortVolBacktester(
            underlying=underlying,
            starting_capital=self.starting_capital,
            hold_days=self.hold_days,
            spread_points=self.spread_points,
            strategy=ShortVolStrategy(
                sd=p.get("sd"),
                min_vrp=p.get("min_vrp"),
                kelly_fraction=p.get("kelly_fraction"),
                risk_budget=p.get("risk_budget"),
            ),
        )

    def run(
        self,
        bars_by_underlying: dict[str, list[DailyBar]],
        vix_by_day: dict[date, float],
        *,
        warmup: int = 21,
    ) -> PortfolioBacktestResult:
        engines = {
            u: self._engine(u) for u in self.underlyings if u in bars_by_underlying
        }
        if not engines:
            return PortfolioBacktestResult(starting_capital=self.starting_capital)

        index_by_u = {
            u: {b.day: i for i, b in enumerate(bars_by_underlying[u])} for u in engines
        }
        # Trade only days every underlying has a bar for, so the book is always
        # marked on a consistent calendar.
        all_days = sorted(set.intersection(*(set(ix) for ix in index_by_u.values())))

        result = PortfolioBacktestResult(
            starting_capital=self.starting_capital,
            params={
                "underlyings": float(len(engines)),
                "slots_per_underlying": float(self.slots_per_underlying),
                "max_portfolio_risk_pct": self.max_portfolio_risk_pct,
                "continuous_entry": float(self.continuous_entry),
                **{k: float(v) for k, v in self.strategy_params.items() if v is not None},
            },
        )
        equity = self.starting_capital
        open_pos: list[dict] = []

        for day in all_days:
            vix = vix_by_day.get(day)
            if vix is None or vix <= 0:
                result.equity_curve.append((day, equity))
                continue

            # ---- mark / close open positions ---------------------------
            unrealized = 0.0
            still_open: list[dict] = []
            for pos in open_pos:
                u = pos["underlying"]
                eng, bars = engines[u], bars_by_underlying[u]
                i = index_by_u[u][day]
                bar = bars[i]
                dte_left = max(0, self.hold_days - (i - pos["entry_idx"]))
                close_cost = eng._structure_value(
                    pos["legs"], bar.close, vix / 100.0, dte_left / TRADING_DAYS
                ) + eng._leg_spread_cost(pos["legs"])
                trade = pos["trade"]
                credit = trade.entry_credit_points
                raw = credit - close_cost
                pnl_points = max(raw, -trade.max_loss_points)

                if raw >= 0.50 * credit:
                    reason = "profit_target"
                elif raw <= -1.5 * credit:
                    reason = "stop_loss"
                elif dte_left <= 0:
                    reason = "expiry"
                else:
                    reason = ""

                if reason:
                    charges = eng._charges(pos["legs"], trade.lots, credit, close_cost)
                    trade.exit_day = day
                    trade.exit_debit_points = close_cost
                    trade.pnl = pnl_points * trade.lots * eng.lot_size - charges
                    trade.charges = charges
                    trade.exit_reason = reason
                    equity += trade.pnl
                    result.trades.append(trade)
                else:
                    unrealized += pnl_points * trade.lots * eng.lot_size
                    still_open.append(pos)
            open_pos = still_open

            # ---- consider new entries ----------------------------------
            open_risk = sum(
                p["trade"].max_loss_points * p["trade"].lots * engines[p["underlying"]].lot_size
                for p in open_pos
            )
            risk_cap = self.max_portfolio_risk_pct * equity

            if self.continuous_entry or day.weekday() == 0:
                for u, eng in engines.items():
                    if sum(1 for p in open_pos if p["underlying"] == u) >= self.slots_per_underlying:
                        continue
                    i = index_by_u[u][day]
                    if i < warmup:
                        continue
                    bars = bars_by_underlying[u]
                    bar = bars[i]
                    decision = eng.strategy.decide(
                        spot=bar.close,
                        vix=vix,
                        closes=[b.close for b in bars[max(0, i - 60): i + 1]],
                        capital=equity,
                        lot_size=eng.lot_size,
                        strike_step=eng.strike_step,
                        wing_width=eng.wing,
                        hold_days=self.hold_days,
                        structure="condor",
                    )
                    if not (decision.enter and decision.legs and decision.lots > 0):
                        continue
                    entry_credit = decision.net_credit - eng._leg_spread_cost(decision.legs)
                    if entry_credit <= 0:
                        continue
                    max_loss_pts = eng.wing - entry_credit
                    add_risk = max_loss_pts * decision.lots * eng.lot_size
                    # Correlation-aware cap: every short-vol slot loses together,
                    # so the binding constraint is TOTAL simultaneous defined
                    # risk, never per-slot sizing.
                    if open_risk + add_risk > risk_cap:
                        continue
                    open_pos.append({
                        "underlying": u,
                        "legs": decision.legs,
                        "entry_idx": i,
                        "trade": ShortVolTrade(
                            entry_day=day, underlying=u, structure="condor",
                            lots=decision.lots, entry_credit_points=entry_credit,
                            max_loss_points=max_loss_pts,
                        ),
                    })
                    open_risk += add_risk

            result.peak_concurrent = max(result.peak_concurrent, len(open_pos))
            if equity > 0:
                result.peak_risk_pct = max(result.peak_risk_pct, open_risk / equity)
            result.equity_curve.append((day, equity + unrealized))

        return result
