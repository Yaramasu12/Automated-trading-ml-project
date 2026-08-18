"""Cross-sectional factor research — ranking stocks against each other.

WHY THIS IS A DIFFERENT SURFACE (not another repeat)
-----------------------------------------------------
Everything rejected so far on this repo was **time-series**: when to be long or
short ONE instrument (index momentum, breakout, mean-reversion, Hurst, Kalman,
trend-following, short-vol timing). All failed or lost to buy-and-hold.

Cross-sectional asks a different question: given N stocks, which ones
out-perform the others? Long the top rank, short (or underweight) the bottom.
Two properties make this worth testing separately rather than assuming it fails
for the same reasons:

- It is **market-neutral-ish**. A long-top/short-bottom book has little net
  index exposure, so its return is largely independent of whether NIFTY rises —
  genuinely uncorrelated with both the short-vol sleeve and buy-and-hold.
- Cross-sectional momentum is among the most replicated anomalies in the
  literature (Jegadeesh & Titman 1993) and has survived out-of-sample across
  decades and markets — a much stronger prior than "MA crossover on an index".

Still gate-tested like everything else. A strong prior is not evidence.

SURVIVORSHIP BIAS — READ BEFORE BELIEVING ANY RESULT HERE
----------------------------------------------------------
The universe is today's liquid F&O names. Those stocks are liquid TODAY partly
*because* they performed well over the sample — companies that collapsed or got
delisted are absent. This biases long-side results upward, and there is no free
fix (a point-in-time historical constituent list is not available here).

Consequences, applied throughout:
  * long-short results are far more trustworthy than long-only, because the
    bias inflates both legs and largely cancels;
  * an equal-weight universe portfolio is reported as the benchmark, so a
    factor must beat *the biased universe itself*, not just cash;
  * any long-only result should be discounted, not taken at face value.
"""
from __future__ import annotations

import glob
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Sequence

from trading_platform.backtesting.short_vol_backtest import DailyBar, load_daily_closes

logger = logging.getLogger(__name__)

TRADING_DAYS = 252
# Round-trip cost per rebalance leg: brokerage+STT+exchange+GST+stamp on
# delivery equity, plus slippage. Cross-sectional strategies rebalance often
# and hold many names, so costs decide viability — modelled deliberately
# pessimistically rather than optimistically.
COST_PER_SIDE = 0.0015          # 15 bps per side


@dataclass
class FactorSpec:
    """A cross-sectional factor.

    `score_fn(history) -> float | None` is called per symbol with that symbol's
    bars UP TO AND INCLUDING the rebalance date. Higher score = more attractive
    (goes long). Return None to exclude the symbol that period (e.g. not enough
    history yet).
    """
    name: str
    score_fn: Callable[[Sequence[DailyBar]], float | None]
    description: str = ""


@dataclass
class FactorResult:
    name: str
    params: dict
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    benchmark_curve: list[tuple[date, float]] = field(default_factory=list)
    n_rebalances: int = 0
    total_costs: float = 0.0
    period_returns: list[float] = field(default_factory=list)
    starting_capital: float = 1_000_000.0

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.starting_capital

    @property
    def daily_returns(self) -> list[float]:
        v = [x for _, x in self.equity_curve]
        return [(v[i] - v[i - 1]) / v[i - 1] for i in range(1, len(v)) if v[i - 1] > 0]

    def cagr(self) -> float:
        if len(self.equity_curve) < 2 or self.final_equity <= 0:
            return 0.0
        yrs = (self.equity_curve[-1][0] - self.equity_curve[0][0]).days / 365.25
        return (self.final_equity / self.starting_capital) ** (1 / yrs) - 1 if yrs > 0 else 0.0

    def benchmark_cagr(self) -> float:
        if len(self.benchmark_curve) < 2:
            return 0.0
        yrs = (self.benchmark_curve[-1][0] - self.benchmark_curve[0][0]).days / 365.25
        end = self.benchmark_curve[-1][1]
        return (end / self.starting_capital) ** (1 / yrs) - 1 if yrs > 0 and end > 0 else 0.0

    def sharpe(self) -> float:
        r = self.daily_returns
        if len(r) < 2:
            return 0.0
        m = sum(r) / len(r)
        sd = math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
        return (m / sd) * math.sqrt(TRADING_DAYS) if sd > 0 else 0.0

    def max_drawdown(self) -> float:
        peak, mdd = -float("inf"), 0.0
        for _, v in self.equity_curve:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak)
        return mdd

    def to_dict(self) -> dict:
        return {
            "factor": self.name, "params": self.params,
            "cagr": round(self.cagr(), 4),
            "benchmark_cagr": round(self.benchmark_cagr(), 4),
            "excess": round(self.cagr() - self.benchmark_cagr(), 4),
            "sharpe": round(self.sharpe(), 3),
            "max_drawdown": round(self.max_drawdown(), 4),
            "rebalances": self.n_rebalances,
            "total_costs": round(self.total_costs, 2),
        }


class CrossSectionalBacktester:
    """Rank-and-hold factor backtest over a panel of daily bars."""

    def __init__(
        self,
        *,
        starting_capital: float = 1_000_000.0,
        n_long: int = 8,
        n_short: int = 8,
        long_short: bool = True,
        rebalance_days: int = 21,
        min_history: int = 260,
    ) -> None:
        self.starting_capital = starting_capital
        self.n_long = n_long
        self.n_short = n_short
        self.long_short = long_short
        self.rebalance_days = rebalance_days
        self.min_history = min_history

    def run(self, panel: dict[str, list[DailyBar]], spec: FactorSpec) -> FactorResult:
        # Common calendar so every holding is marked on the same days.
        day_sets = [set(b.day for b in bars) for bars in panel.values()]
        days = sorted(set.intersection(*day_sets)) if day_sets else []
        if len(days) < self.min_history + self.rebalance_days:
            return FactorResult(spec.name, {}, starting_capital=self.starting_capital)

        px = {s: {b.day: b.close for b in bars} for s, bars in panel.items()}
        hist = {s: [b for b in bars] for s, bars in panel.items()}
        idx = {s: {b.day: i for i, b in enumerate(bars)} for s, bars in panel.items()}

        res = FactorResult(
            spec.name,
            {"n_long": self.n_long, "n_short": self.n_short,
             "long_short": self.long_short, "rebalance_days": self.rebalance_days},
            starting_capital=self.starting_capital,
        )
        equity = self.starting_capital
        bench = self.starting_capital
        weights: dict[str, float] = {}

        for di in range(self.min_history, len(days)):
            day, prev = days[di], days[di - 1]

            # Mark the book on today's move using YESTERDAY's weights, so a
            # position never earns the return of the day it was chosen on.
            if weights:
                port_ret = 0.0
                for sym, w in weights.items():
                    p0, p1 = px[sym].get(prev), px[sym].get(day)
                    if p0 and p1 and p0 > 0:
                        port_ret += w * (p1 - p0) / p0
                equity *= (1.0 + port_ret)

            # Equal-weight universe benchmark (the biased universe itself).
            bret, n = 0.0, 0
            for sym in panel:
                p0, p1 = px[sym].get(prev), px[sym].get(day)
                if p0 and p1 and p0 > 0:
                    bret += (p1 - p0) / p0; n += 1
            if n:
                bench *= (1.0 + bret / n)

            if (di - self.min_history) % self.rebalance_days == 0:
                scores: list[tuple[str, float]] = []
                for sym, bars in hist.items():
                    i = idx[sym].get(day)
                    if i is None or i < self.min_history:
                        continue
                    try:
                        sc = spec.score_fn(bars[: i + 1])   # strictly past+today
                    except Exception:                        # noqa: BLE001
                        sc = None
                    if sc is not None and math.isfinite(sc):
                        scores.append((sym, float(sc)))

                new_w: dict[str, float] = {}
                if len(scores) >= (self.n_long + (self.n_short if self.long_short else 0)):
                    scores.sort(key=lambda t: t[1], reverse=True)
                    longs = scores[: self.n_long]
                    for sym, _ in longs:
                        new_w[sym] = 0.5 / len(longs) if self.long_short else 1.0 / len(longs)
                    if self.long_short and self.n_short > 0:
                        shorts = scores[-self.n_short:]
                        for sym, _ in shorts:
                            new_w[sym] = new_w.get(sym, 0.0) - 0.5 / len(shorts)

                turnover = sum(
                    abs(new_w.get(s, 0.0) - weights.get(s, 0.0))
                    for s in set(new_w) | set(weights)
                )
                cost = equity * turnover * COST_PER_SIDE
                equity -= cost
                res.total_costs += cost
                res.n_rebalances += 1
                weights = new_w

            res.equity_curve.append((day, equity))
            res.benchmark_curve.append((day, bench))
        return res


# ─── Factor library ──────────────────────────────────────────────────────────
# Classic, well-documented factors. Each is deliberately simple: the point is
# to test whether a KNOWN effect survives on this universe net of costs, not to
# invent something clever and then discover it was curve-fit.

def momentum(lookback: int = 252, skip: int = 21) -> FactorSpec:
    """12-1 momentum (Jegadeesh-Titman): trailing return, skipping the most
    recent month to avoid the well-documented short-term reversal effect."""
    def score(bars: Sequence[DailyBar]) -> float | None:
        if len(bars) < lookback + 1:
            return None
        end = bars[-1 - skip].close if skip and len(bars) > skip else bars[-1].close
        start = bars[-1 - lookback].close
        return (end / start - 1.0) if start > 0 else None
    return FactorSpec(f"momentum_{lookback}_{skip}", score, "12-1 cross-sectional momentum")


def short_term_reversal(lookback: int = 21) -> FactorSpec:
    """Buy recent losers. Documented counterpart to momentum at short horizons."""
    def score(bars: Sequence[DailyBar]) -> float | None:
        if len(bars) < lookback + 1:
            return None
        s, e = bars[-1 - lookback].close, bars[-1].close
        return -((e / s) - 1.0) if s > 0 else None
    return FactorSpec(f"reversal_{lookback}", score, "short-term reversal")


def low_volatility(window: int = 126) -> FactorSpec:
    """Low-volatility anomaly: low-vol stocks have historically delivered
    better risk-adjusted returns than CAPM predicts."""
    def score(bars: Sequence[DailyBar]) -> float | None:
        if len(bars) < window + 1:
            return None
        c = [b.close for b in bars[-(window + 1):]]
        rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c)) if c[i - 1] > 0]
        if len(rets) < 2:
            return None
        m = sum(rets) / len(rets)
        sd = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1))
        return -sd
    return FactorSpec(f"low_vol_{window}", score, "low-volatility anomaly")


def load_panel(pattern: str = "data/historical/*__ONE_DAY_deep.csv",
               exclude: Sequence[str] = ("NIFTY", "BANKNIFTY", "FINNIFTY", "INDIAVIX"),
               min_bars: int = 1000) -> dict[str, list[DailyBar]]:
    """Load every deep-history CSV into a symbol->bars panel, dropping indices
    (they are benchmarks/vol measures, not cross-sectional constituents)."""
    panel: dict[str, list[DailyBar]] = {}
    for path in sorted(glob.glob(pattern)):
        sym = os.path.basename(path).replace("__ONE_DAY_deep.csv", "")
        if sym in exclude:
            continue
        try:
            bars = load_daily_closes(path)
        except Exception as exc:                              # noqa: BLE001
            logger.warning("skipping %s: %s", sym, exc)
            continue
        if len(bars) >= min_bars:
            panel[sym] = bars
    return panel
