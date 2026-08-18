"""Time-series momentum (trend-following) on the index — a candidate SECOND edge.

WHY THIS SPECIFIC STRATEGY, AND NOT "MORE SHORT-VOL"
----------------------------------------------------
Measured evidence (see memory `short-vol-return-ceiling`): short-vol tops out
near 3%/yr on NIFTY, and every attempt to scale it — more frequent entries,
more underlyings, more concurrent slots — made it WORSE, not better, because
correlated short-vol slots are one bet at N times the size, not N bets.

So closing a return gap needs an edge that is *structurally different*, not
more leverage on the same one. Time-series momentum qualifies on the property
that actually matters here:

    short-vol is SHORT gamma  -> wins in calm, loses badly in sustained moves
    trend-following is LONG gamma-like -> loses in chop, wins in sustained moves

Their worst environments are opposites. Short-vol's disaster (a crash) is
trend-following's best case (it flips short and rides it down). That is real
diversification; two short-vol variants are not.

HONEST RELATIONSHIP TO THIS REPO'S "DAILY TA HAS ZERO EDGE" FINDING
--------------------------------------------------------------------
CLAUDE.md records that daily-bar TA features showed **zero** OOS edge (AUC
~0.50 on 2500 days) and that `neural/return_forecaster.py` therefore refuses to
ship. That finding is about a *classifier predicting next-day direction*.

Time-series momentum is a different claim and must be judged separately: it
does not predict direction accurately (its hit rate is typically ~50%), it
harvests the asymmetry of trends — a modest number of large winning runs paying
for many small losses. Whether that survives on NIFTY 2019-2026 net of costs is
an empirical question, answered by the SAME §5 gates as everything else. If it
fails the gates it does not ship, exactly like the return forecaster.

No claim is made here that this works. It is a candidate, gate-tested.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date

from trading_platform.backtesting.short_vol_backtest import DailyBar, TRADING_DAYS

logger = logging.getLogger(__name__)

# Index-futures round-trip cost as a fraction of notional. Components (NSE
# equity futures): STT 0.0125% on the sell leg, exchange txn ~0.0019%,
# GST 18% on (brokerage+txn), stamp 0.002% on buy, brokerage ~flat. ~0.03%
# per round trip is a realistic, slightly pessimistic aggregate — plus
# slippage, added separately below.
FUTURES_ROUNDTRIP_COST = 0.0003
SLIPPAGE_PER_SIDE = 0.0002          # ~2bps per side on a liquid index future


@dataclass
class TrendBacktestResult:
    underlying: str
    params: dict[str, float]
    starting_capital: float
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    n_trades: int = 0
    total_costs: float = 0.0
    trade_pnls: list[float] = field(default_factory=list)

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.starting_capital

    @property
    def daily_returns(self) -> list[float]:
        v = [x for _, x in self.equity_curve]
        return [(v[i] - v[i - 1]) / v[i - 1] for i in range(1, len(v)) if v[i - 1] > 0]

    @property
    def max_drawdown(self) -> float:
        peak, mdd = -float("inf"), 0.0
        for _, v in self.equity_curve:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak)
        return mdd

    def cagr(self) -> float:
        if not self.equity_curve or self.starting_capital <= 0 or self.final_equity <= 0:
            return 0.0
        yrs = (self.equity_curve[-1][0] - self.equity_curve[0][0]).days / 365.25
        return (self.final_equity / self.starting_capital) ** (1 / yrs) - 1 if yrs > 0 else 0.0

    def sharpe(self) -> float:
        r = self.daily_returns
        if len(r) < 2:
            return 0.0
        mean = sum(r) / len(r)
        var = sum((x - mean) ** 2 for x in r) / (len(r) - 1)
        sd = math.sqrt(var)
        return (mean / sd) * math.sqrt(TRADING_DAYS) if sd > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "params": self.params,
            "trades": self.n_trades,
            "net_pnl": round(self.final_equity - self.starting_capital, 2),
            "total_costs": round(self.total_costs, 2),
            "cagr": round(self.cagr(), 4),
            "sharpe": round(self.sharpe(), 3),
            "max_drawdown": round(self.max_drawdown, 4),
        }


class TrendFollowingBacktester:
    """Time-series momentum with volatility targeting (REDESIGN §4.4e).

    Rule, deliberately as simple as possible to limit overfitting surface:
      signal   = sign of the trailing `lookback`-day return
      exposure = signal * min(max_leverage, target_vol / realized_vol)

    Volatility targeting (rather than fixed notional) is what makes the return
    stream comparable across regimes and is explicitly called for in §4.4e.
    `allow_short=False` gives a long/flat variant, which is what a cash-equity
    account can actually implement without futures.
    """

    def __init__(
        self,
        *,
        underlying: str = "NIFTY",
        starting_capital: float = 1_000_000.0,
        lookback: int = 100,
        vol_window: int = 20,
        target_vol: float = 0.15,
        max_leverage: float = 1.0,
        allow_short: bool = True,
        rebalance_days: int = 5,
    ) -> None:
        self.underlying = underlying
        self.starting_capital = starting_capital
        self.lookback = lookback
        self.vol_window = vol_window
        self.target_vol = target_vol
        self.max_leverage = max_leverage
        self.allow_short = allow_short
        self.rebalance_days = max(1, rebalance_days)

    @staticmethod
    def _realized_vol(closes: list[float], window: int) -> float:
        if len(closes) < window + 1:
            return 0.0
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - window, len(closes))]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
        return math.sqrt(var) * math.sqrt(TRADING_DAYS)

    def run(self, bars: list[DailyBar]) -> TrendBacktestResult:
        result = TrendBacktestResult(
            underlying=self.underlying,
            starting_capital=self.starting_capital,
            params={
                "lookback": float(self.lookback),
                "vol_window": float(self.vol_window),
                "target_vol": self.target_vol,
                "max_leverage": self.max_leverage,
                "allow_short": float(self.allow_short),
                "rebalance_days": float(self.rebalance_days),
            },
        )
        equity = self.starting_capital
        exposure = 0.0          # signed fraction of equity
        warmup = max(self.lookback, self.vol_window) + 1
        closes: list[float] = []
        last_exposure_change_pnl = 0.0

        for i, bar in enumerate(bars):
            closes.append(bar.close)

            # Mark existing exposure against today's move BEFORE any rebalance,
            # so a position never earns the return of a day it wasn't held for.
            if i > 0 and exposure != 0.0:
                ret = (bar.close - bars[i - 1].close) / bars[i - 1].close
                pnl = equity * exposure * ret
                equity += pnl
                last_exposure_change_pnl += pnl

            if i >= warmup and i % self.rebalance_days == 0:
                past = closes[-(self.lookback + 1)]
                signal = 1.0 if bar.close > past else (-1.0 if self.allow_short else 0.0)
                rv = self._realized_vol(closes, self.vol_window)
                scale = min(self.max_leverage, self.target_vol / rv) if rv > 0 else 0.0
                target = signal * scale

                if abs(target - exposure) > 1e-9:
                    turnover = abs(target - exposure)
                    cost = equity * turnover * (FUTURES_ROUNDTRIP_COST / 2 + SLIPPAGE_PER_SIDE)
                    equity -= cost
                    result.total_costs += cost
                    result.n_trades += 1
                    if exposure != 0.0:
                        result.trade_pnls.append(last_exposure_change_pnl)
                    last_exposure_change_pnl = 0.0
                    exposure = target

            result.equity_curve.append((bar.day, equity))

        if exposure != 0.0:
            result.trade_pnls.append(last_exposure_change_pnl)
        return result


# ─── Combined book ───────────────────────────────────────────────────────────

def combine_equity_curves(
    curves: list[list[tuple[date, float]]],
    weights: list[float],
    starting_capital: float = 1_000_000.0,
) -> list[tuple[date, float]]:
    """Blend strategy equity curves into one portfolio curve.

    Combines DAILY RETURNS at fixed weights (rebalanced daily), not raw equity
    levels — adding equity curves would silently assume each sleeve got the
    full capital, overstating the book by the number of sleeves.
    """
    if not curves or not weights or len(curves) != len(weights):
        return []
    days = sorted(set.intersection(*(set(d for d, _ in c) for c in curves)))
    if not days:
        return []
    by_day = [{d: v for d, v in c} for c in curves]
    total_w = sum(weights) or 1.0
    w = [x / total_w for x in weights]

    out, equity = [], starting_capital
    for i, day in enumerate(days):
        if i > 0:
            blended = 0.0
            for series, weight in zip(by_day, w):
                prev, cur = series[days[i - 1]], series[day]
                blended += weight * ((cur - prev) / prev if prev > 0 else 0.0)
            equity *= (1.0 + blended)
        out.append((day, equity))
    return out
