"""Hypothesis harness — the edge-discovery assembly line.

WHY THIS EXISTS
---------------
Edges are small, rare, and they decay. You cannot design your way to one; you
can only *search* efficiently. So the number that governs the outcome is
**hypothesis throughput** — how many independent ideas get honestly validated
per week.

Before this module that number was ~1 per working session, because every
candidate needed a bespoke backtest hand-built around it (see
`short_vol_backtest.py`, `trend_backtest.py` — each is a few hundred lines,
and most of it is the same loop). The §5 gates were already built and already
proven to discriminate (they passed short-vol and rejected trend-following),
so the missing piece was never quality control. It was the conveyor belt.

A hypothesis here is ~20 lines: a function mapping bars to a desired exposure
series. The harness supplies everything else — costs, equity curve, parameter
sweep, DSR/PBO/Monte-Carlo gates, and a recorded verdict.

WHAT THIS DOES NOT DO
---------------------
It does not make edges appear. It makes real ones cheaper to find and fake
ones cheaper to kill. A hypothesis that passes here has cleared the same bar
short-vol cleared; one that fails is genuinely rejected, exactly as
trend-following was (PBO 0.571). Expect most candidates to fail. That is the
point — the value is in rejecting 19 ideas cheaply enough that testing the
20th is still affordable.

USAGE
-----
    def momentum(bars, params):
        lb = int(params["lookback"])
        out = [0.0] * len(bars)
        for i in range(lb, len(bars)):
            out[i] = 1.0 if bars[i].close > bars[i - lb].close else 0.0
        return out

    spec = HypothesisSpec(
        name="price_momentum",
        exposure_fn=momentum,
        param_grid=[{"lookback": lb} for lb in (20, 50, 100)],
    )
    verdict = HypothesisHarness().evaluate(spec, bars)
    print(verdict.summary())
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Sequence

from trading_platform.backtesting.short_vol_backtest import TRADING_DAYS, DailyBar

logger = logging.getLogger(__name__)

# Round-trip cost as a fraction of traded notional, charged on |Δexposure|.
# Index futures: ~0.03% all-in (STT sell leg, exchange txn, GST, stamp) plus
# ~2bps/side slippage. Deliberately on the pessimistic side — a harness that
# flatters candidates is worse than no harness, because it converts "cheap to
# test" into "cheap to fool yourself".
DEFAULT_COST_PER_TURN = 0.0005

# An exposure function returns the DESIRED position per bar, as a signed
# fraction of equity: +1.0 fully long, -1.0 fully short, 0.0 flat.
ExposureFn = Callable[[Sequence[DailyBar], dict], Sequence[float]]


@dataclass
class HypothesisSpec:
    """One candidate edge, plus the parameter space actually searched.

    `param_grid` is not decoration: DSR and PBO are *selection-bias*
    statistics. They answer "given that I searched N configurations and kept
    the best, how much of the winner is luck?" — so the grid must be the
    honest set of configurations considered. Padding it inflates the
    deflation; trimming it after seeing results hides the search and is the
    single easiest way to fool these gates.
    """
    name: str
    exposure_fn: ExposureFn
    param_grid: list[dict] = field(default_factory=lambda: [{}])
    description: str = ""
    cost_per_turn: float = DEFAULT_COST_PER_TURN


@dataclass
class VariantRun:
    params: dict
    equity_curve: list[tuple[date, float]]
    n_turns: int
    total_costs: float
    trade_pnls: list[float]

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else 0.0

    @property
    def daily_returns(self) -> list[float]:
        v = [x for _, x in self.equity_curve]
        return [(v[i] - v[i - 1]) / v[i - 1] for i in range(1, len(v)) if v[i - 1] > 0]

    def sharpe(self) -> float:
        r = self.daily_returns
        if len(r) < 2:
            return 0.0
        m = sum(r) / len(r)
        sd = math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
        return (m / sd) * math.sqrt(TRADING_DAYS) if sd > 0 else 0.0

    def cagr(self, starting_capital: float) -> float:
        if not self.equity_curve or self.final_equity <= 0:
            return 0.0
        yrs = (self.equity_curve[-1][0] - self.equity_curve[0][0]).days / 365.25
        return (self.final_equity / starting_capital) ** (1 / yrs) - 1 if yrs > 0 else 0.0

    def max_drawdown(self) -> float:
        peak, mdd = -float("inf"), 0.0
        for _, v in self.equity_curve:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak)
        return mdd


@dataclass
class HypothesisVerdict:
    name: str
    n_variants: int
    passed: bool
    gates: Any                       # BacktestGateResults
    best_params: dict
    best_cagr: float
    best_sharpe: float
    best_max_drawdown: float
    benchmark_cagr: float | None = None
    runs: list[VariantRun] = field(default_factory=list)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def beats_benchmark(self) -> bool | None:
        """Passing the gates is NOT the same as being worth trading. A long-
        biased hypothesis in a bull market can clear every statistical gate
        and still lose to buy-and-hold — measured on this repo: trend-following
        returned 8.23%/yr against the index's 11.23%. None means no benchmark
        was supplied."""
        if self.benchmark_cagr is None:
            return None
        return self.best_cagr > self.benchmark_cagr

    def summary(self) -> str:
        lines = [
            f"hypothesis : {self.name}",
            f"variants   : {self.n_variants}",
            f"best params: {self.best_params}",
            f"best       : CAGR {self.best_cagr * 100:.2f}%  "
            f"Sharpe {self.best_sharpe:.2f}  maxDD {self.best_max_drawdown * 100:.1f}%",
        ]
        if self.benchmark_cagr is not None:
            verdict = "BEATS" if self.beats_benchmark() else "LOSES TO"
            lines.append(
                f"benchmark  : {self.benchmark_cagr * 100:.2f}%/yr  -> {verdict} benchmark"
            )
        for gate in (self.gates.dsr, self.gates.pbo,
                     self.gates.monte_carlo, self.gates.cost_model):
            if gate is not None:
                lines.append(f"  {gate.gate_name:16} {gate.result.value:5} {gate.message}")
        lines.append(f"VERDICT    : {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


class HypothesisHarness:
    """Turns an exposure function into a gated verdict."""

    def __init__(
        self,
        *,
        starting_capital: float = 1_000_000.0,
        settings: object | None = None,
    ) -> None:
        self.starting_capital = starting_capital
        self.settings = settings

    # -- simulation ---------------------------------------------------------

    def _simulate(self, bars: Sequence[DailyBar], exposures: Sequence[float],
                  cost_per_turn: float) -> VariantRun:
        """Convert an exposure series into an equity curve, charging cost on
        every change in position.

        Exposure for bar i is the position held INTO bar i+1 — i.e. a signal
        computed from bar i's close can only earn bar i+1's return. Shifting
        by one bar here is what prevents the single most common backtest
        error: paying today's exposure today's return, which silently grants
        perfect foresight.
        """
        equity = self.starting_capital
        curve: list[tuple[date, float]] = []
        prev_exposure, turns, costs = 0.0, 0, 0.0
        trade_pnls: list[float] = []
        running_pnl = 0.0

        for i, bar in enumerate(bars):
            if i > 0:
                held = exposures[i - 1]          # position taken at the PREVIOUS close
                if held != 0.0:
                    ret = (bar.close - bars[i - 1].close) / bars[i - 1].close
                    pnl = equity * held * ret
                    equity += pnl
                    running_pnl += pnl
                target = exposures[i]
                if abs(target - held) > 1e-12:
                    turn = abs(target - held)
                    cost = equity * turn * cost_per_turn
                    equity -= cost
                    costs += cost
                    turns += 1
                    if held != 0.0:
                        trade_pnls.append(running_pnl)
                        running_pnl = 0.0
                prev_exposure = target
            curve.append((bar.day, equity))

        if prev_exposure != 0.0 and abs(running_pnl) > 0:
            trade_pnls.append(running_pnl)
        return VariantRun(params={}, equity_curve=curve, n_turns=turns,
                          total_costs=costs, trade_pnls=trade_pnls)

    # -- evaluation ---------------------------------------------------------

    def evaluate(
        self,
        spec: HypothesisSpec,
        bars: Sequence[DailyBar],
        *,
        benchmark_cagr: float | None = None,
    ) -> HypothesisVerdict:
        import numpy as np

        from trading_platform.validation.gates import GateEvaluator

        runs: list[VariantRun] = []
        for params in spec.param_grid:
            try:
                exposures = list(spec.exposure_fn(bars, params))
            except Exception as exc:
                logger.warning("hypothesis %s failed on %s: %s", spec.name, params, exc)
                continue
            if len(exposures) != len(bars):
                raise ValueError(
                    f"{spec.name}: exposure_fn returned {len(exposures)} values "
                    f"for {len(bars)} bars — must be one exposure per bar"
                )
            run = self._simulate(bars, exposures, spec.cost_per_turn)
            run.params = dict(params)
            runs.append(run)

        evaluator = GateEvaluator(settings=self.settings)
        bt_id = f"hyp-{spec.name}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        if not runs:
            gates = evaluator.finalize(bt_id, spec.name)
            return HypothesisVerdict(spec.name, 0, False, gates, {}, 0.0, 0.0, 0.0,
                                     benchmark_cagr)

        series = [r.daily_returns for r in runs]
        width = min((len(s) for s in series), default=0)
        sharpes = [r.sharpe() for r in runs]
        best_i = max(range(len(runs)), key=lambda i: sharpes[i])
        best = runs[best_i]

        if width >= 30 and len(runs) >= 2:
            matrix = np.column_stack([np.asarray(s[:width], float) for s in series])
            evaluator.evaluate_dsr(sharpes[best_i], sharpes, matrix[:, best_i])
            evaluator.evaluate_pbo(matrix)

        evaluator.evaluate_monte_carlo(
            [{"pnl": p} for p in best.trade_pnls],
            starting_capital=self.starting_capital,
        )
        evaluator.evaluate_cost_model(
            best.final_equity - self.starting_capital, best.total_costs
        )
        gates = evaluator.finalize(bt_id, spec.name)

        # A hypothesis that never takes a position trivially satisfies every
        # gate — no trades means no drawdown, no losses, nothing to overfit —
        # and would be reported as a PASS while earning exactly nothing.
        # Caught 2026-08-09 when an LLM-proposed hypothesis "passed" holdout
        # with CAGR 0.00% / Sharpe 0.00 because it was flat for the entire
        # period (its param grid errored on every combination, and a strategy
        # whose code raises is flat by default). Liveness is a precondition
        # for the gates to mean anything, so it is checked here rather than
        # inside them.
        traded = len(best.trade_pnls) > 0 and any(p != 0.0 for p in best.trade_pnls)
        earned_anything = abs(best.final_equity - self.starting_capital) > 1e-9
        is_live = traded and earned_anything

        return HypothesisVerdict(
            name=spec.name,
            n_variants=len(runs),
            passed=gates.all_passed and is_live,
            gates=gates,
            best_params=best.params,
            best_cagr=best.cagr(self.starting_capital),
            best_sharpe=sharpes[best_i],
            best_max_drawdown=best.max_drawdown(),
            benchmark_cagr=benchmark_cagr,
            runs=runs,
        )

    def evaluate_all(
        self,
        specs: Sequence[HypothesisSpec],
        bars: Sequence[DailyBar],
        *,
        benchmark_cagr: float | None = None,
    ) -> list[HypothesisVerdict]:
        """Run a batch and return verdicts sorted best-Sharpe first.

        Note the multiple-comparisons trap this creates: testing 20 hypotheses
        and reporting the best one is itself a search, and each hypothesis's
        own DSR/PBO only deflates for ITS OWN parameter grid, not for how many
        sibling hypotheses were tried. Treat a winner here as a candidate for
        dedicated out-of-sample testing, never as a promotion-ready result.
        """
        out = [self.evaluate(s, bars, benchmark_cagr=benchmark_cagr) for s in specs]
        return sorted(out, key=lambda v: v.best_sharpe, reverse=True)


def buy_and_hold_cagr(bars: Sequence[DailyBar]) -> float:
    """Benchmark every hypothesis must clear to be worth trading at all."""
    if len(bars) < 2 or bars[0].close <= 0:
        return 0.0
    yrs = (bars[-1].day - bars[0].day).days / 365.25
    return (bars[-1].close / bars[0].close) ** (1 / yrs) - 1 if yrs > 0 else 0.0
