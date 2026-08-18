from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np

from trading_platform.backtesting.engine import BacktestConfig, BacktestEngine
from trading_platform.backtesting.metrics import PerformanceMetrics
from trading_platform.strategies.factory import StrategyFactory


@dataclass(frozen=True)
class StrategyScore:
    strategy_name: str
    family: str
    score: float
    rank: int
    metrics: PerformanceMetrics
    trade_count: int
    approved_orders: int
    rejected_orders: int
    # REDESIGN §5 validation gates need these — deliberately NOT in to_dict(),
    # consumed only by backtesting.evaluator.evaluate_sweep_gates(). Keeping
    # them off the API response avoids bloating every /strategies/evaluate call.
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    trade_pnls: list[float] = field(default_factory=list)
    total_charges: float = 0.0

    def to_dict(self) -> dict:
        return {
            "strategy_name": self.strategy_name,
            "family": self.family,
            "score": self.score,
            "rank": self.rank,
            "metrics": asdict(self.metrics),
            "trade_count": self.trade_count,
            "approved_orders": self.approved_orders,
            "rejected_orders": self.rejected_orders,
        }


@dataclass(frozen=True)
class StrategyEvaluationResult:
    start: date
    days: int
    underlyings: tuple[str, ...]
    leaderboard: list[StrategyScore]

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "days": self.days,
            "underlyings": list(self.underlyings),
            "leaderboard": [score.to_dict() for score in self.leaderboard],
            "best_strategy": self.leaderboard[0].strategy_name if self.leaderboard else None,
        }


class StrategyEvaluator:
    def __init__(self, backtest_engine: BacktestEngine, strategy_factory: StrategyFactory | None = None):
        self.backtest_engine = backtest_engine
        self.strategy_factory = strategy_factory or StrategyFactory()

    def evaluate(
        self,
        start: date,
        days: int,
        underlyings: tuple[str, ...],
        starting_capital: float,
        max_drawdown: float,
        strategy_names: tuple[str, ...] | None = None,
    ) -> StrategyEvaluationResult:
        names = strategy_names or tuple(self.strategy_factory.names())
        scores: list[StrategyScore] = []
        for name in names:
            strategy = self.strategy_factory.get(name)
            config = BacktestConfig(
                starting_capital=starting_capital,
                start=start,
                days=days,
                underlyings=underlyings,
                max_drawdown=max_drawdown,
                strategy_names=(name,),
            )
            result = self.backtest_engine.run(config)
            approved = sum(1 for report in result.reports if report.risk_decision.approved)
            rejected = len(result.reports) - approved
            raw_score = self._score(result.metrics)
            scores.append(
                StrategyScore(
                    strategy_name=name,
                    family=strategy.family,
                    score=raw_score,
                    rank=0,
                    metrics=result.metrics,
                    trade_count=result.metrics.trade_count,
                    approved_orders=approved,
                    rejected_orders=rejected,
                    equity_curve=result.equity_curve,
                    trade_pnls=result.trade_pnls,
                    total_charges=result.total_charges,
                )
            )
        ranked = sorted(scores, key=lambda item: item.score, reverse=True)
        ranked = [
            StrategyScore(
                strategy_name=item.strategy_name,
                family=item.family,
                score=item.score,
                rank=index + 1,
                metrics=item.metrics,
                trade_count=item.trade_count,
                approved_orders=item.approved_orders,
                rejected_orders=item.rejected_orders,
                equity_curve=item.equity_curve,
                trade_pnls=item.trade_pnls,
                total_charges=item.total_charges,
            )
            for index, item in enumerate(ranked)
        ]
        return StrategyEvaluationResult(start=start, days=days, underlyings=underlyings, leaderboard=ranked)

    def _score(self, metrics: PerformanceMetrics) -> float:
        # Shift by 1 so factor<1.0 penalises and factor>1.0 rewards.
        profit_quality = (min(3.0, metrics.profit_factor) - 1.0) * 0.15
        risk_adjusted = metrics.sharpe_like * 0.10
        return metrics.return_pct + profit_quality + risk_adjusted - (metrics.max_drawdown * 1.5)


# ---------------------------------------------------------------------------
# REDESIGN §5 validation gates over a strategy sweep
# ---------------------------------------------------------------------------


def _aligned_returns_matrix(leaderboard: list) -> tuple:
    """Build a (T periods x N variants) per-period return matrix from each
    StrategyScore's equity curve, aligned on the timestamps common to ALL
    variants (so every column covers the same periods — required for CSCV).

    Returns (matrix, timestamps). Empty matrix if fewer than 2 variants have
    usable (>=2-point) curves.
    """
    usable = [s for s in leaderboard if len(s.equity_curve) >= 2]
    if len(usable) < 2:
        return np.empty((0, 0)), []

    common: set | None = None
    for score in usable:
        stamps = {ts for ts, _ in score.equity_curve}
        common = stamps if common is None else (common & stamps)
    timestamps = sorted(common or set())
    if len(timestamps) < 3:
        return np.empty((0, 0)), []

    columns = []
    for score in usable:
        by_ts = dict(score.equity_curve)
        equity = np.array([float(by_ts[ts]) for ts in timestamps], dtype=np.float64)
        prev = equity[:-1]
        # Period-over-period simple return; guard div-by-zero on a wiped-out curve.
        rets = np.divide(
            np.diff(equity), prev, out=np.zeros_like(prev), where=prev != 0,
        )
        columns.append(rets)
    # `usable` is returned so callers can map matrix columns back to the
    # StrategyScore they came from — column i is usable[i], which is NOT
    # necessarily leaderboard[i] once thin curves are filtered out.
    return np.column_stack(columns), usable


def evaluate_sweep_gates(evaluation: "StrategyEvaluationResult", settings: Any = None):
    """REDESIGN §5: DSR / PBO / Monte-Carlo-DD / cost-model gates over a
    StrategyEvaluationResult's leaderboard — the N strategy variants evaluated
    over the same window are the "sweep" (the N trials) these statistics need.

    Gates that need >=2 comparable variants (DSR, PBO) are recorded as SKIP
    rather than crashing or fabricating a value when the leaderboard is too
    thin. Monte-Carlo-DD and the cost model apply to the winning variant.
    """
    from trading_platform.validation.gates import GateEvaluator, GateOutcome, GateResult

    evaluator = GateEvaluator(settings=settings)
    leaderboard = list(evaluation.leaderboard)
    if not leaderboard:
        return evaluator.finalize("", "")

    best = leaderboard[0]  # already rank-sorted by StrategyEvaluator.evaluate()
    matrix, usable = _aligned_returns_matrix(leaderboard)

    if matrix.size and matrix.shape[1] >= 2:
        # Per-period Sharpe (NOT annualised) — DSR's T is the number of periods
        # in this same series, so both must be on the same footing.
        trial_sharpes = []
        for col in range(matrix.shape[1]):
            series = matrix[:, col]
            sd = float(np.std(series, ddof=1)) if len(series) > 1 else 0.0
            trial_sharpes.append(float(np.mean(series) / sd) if sd > 0 else 0.0)
        # The DSR subject is the leaderboard winner IF it survived the
        # usable-curve filter; otherwise fall back to the best-Sharpe column.
        try:
            best_col = usable.index(best)
        except ValueError:
            best_col = int(np.argmax(trial_sharpes))
        evaluator.evaluate_dsr(trial_sharpes[best_col], trial_sharpes, matrix[:, best_col])
        evaluator.evaluate_pbo(matrix)
    else:
        reason = "need >=2 leaderboard variants with overlapping equity curves"
        evaluator.results.dsr = GateOutcome(
            gate_name="deflated_sharpe", result=GateResult.SKIP, metric=0.0, threshold=0.0,
            message=f"SKIPPED: {reason}",
        )
        evaluator.results.pbo = GateOutcome(
            gate_name="pbo", result=GateResult.SKIP, metric=0.0, threshold=0.0,
            message=f"SKIPPED: {reason}",
        )

    if best.trade_pnls:
        evaluator.evaluate_monte_carlo(
            [{"pnl": p} for p in best.trade_pnls],
            starting_capital=best.metrics.starting_capital,
        )
    else:
        evaluator.results.monte_carlo = GateOutcome(
            gate_name="monte_carlo_dd", result=GateResult.SKIP, metric=0.0, threshold=0.0,
            message="SKIPPED: winning variant has no closed round-trip trades",
        )

    evaluator.evaluate_cost_model(best.metrics.total_pnl, best.total_charges)
    evaluator.results.promotion_ladder = None  # set by finalize_ladder below
    results = evaluator.finalize("", best.strategy_name)
    evaluator.evaluate_promotion_ladder(results.all_passed)
    return results


@dataclass(frozen=True)
class WalkForwardFittedParams:
    """Parameters fitted on the train window and frozen before test evaluation.

    These are the *only* values learned during the train phase. They form a
    minimal but real fit-then-predict step so the walk-forward run can
    honestly call itself "training" rather than just running two disjoint
    backtests.
    """

    confidence_floor: float
    expected_edge: float
    train_profit_factor: float
    train_sharpe: float
    train_trade_count: int
    accept_strategy: bool

    def to_dict(self) -> dict:
        return {
            "confidence_floor": self.confidence_floor,
            "expected_edge": self.expected_edge,
            "train_profit_factor": self.train_profit_factor,
            "train_sharpe": self.train_sharpe,
            "train_trade_count": self.train_trade_count,
            "accept_strategy": self.accept_strategy,
        }


@dataclass(frozen=True)
class WalkForwardWindow:
    """One train/test split in a walk-forward run."""

    window_index: int
    strategy_name: str
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    train_metrics: PerformanceMetrics
    test_metrics: PerformanceMetrics
    fitted_params: WalkForwardFittedParams
    test_skipped: bool

    def to_dict(self) -> dict:
        return {
            "window_index": self.window_index,
            "strategy_name": self.strategy_name,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "test_end": self.test_end.isoformat(),
            "train_metrics": asdict(self.train_metrics),
            "test_metrics": asdict(self.test_metrics),
            "fitted_params": self.fitted_params.to_dict(),
            "test_skipped": self.test_skipped,
        }


@dataclass(frozen=True)
class WalkForwardResult:
    strategy_name: str
    total_days: int
    train_days: int
    test_days: int
    underlyings: tuple[str, ...]
    windows: list[WalkForwardWindow]

    @property
    def mean_test_sharpe(self) -> float:
        if not self.windows:
            return 0.0
        return sum(w.test_metrics.sharpe_like for w in self.windows) / len(self.windows)

    @property
    def mean_test_return(self) -> float:
        if not self.windows:
            return 0.0
        return sum(w.test_metrics.return_pct for w in self.windows) / len(self.windows)

    @property
    def degradation_detected(self) -> bool:
        """True if the last window's test Sharpe is materially below the average."""
        if len(self.windows) < 2:
            return False
        avg = self.mean_test_sharpe
        last = self.windows[-1].test_metrics.sharpe_like
        return last < avg - 0.5

    def to_dict(self) -> dict:
        return {
            "strategy_name": self.strategy_name,
            "total_days": self.total_days,
            "train_days": self.train_days,
            "test_days": self.test_days,
            "underlyings": list(self.underlyings),
            "window_count": len(self.windows),
            "mean_test_sharpe": self.mean_test_sharpe,
            "mean_test_return": self.mean_test_return,
            "degradation_detected": self.degradation_detected,
            "windows": [w.to_dict() for w in self.windows],
        }


class WalkForwardEvaluator:
    """Honest train-then-test walk-forward evaluator for a single strategy.

    For each window:
      1. Run the strategy on the train slice and *fit* `WalkForwardFittedParams`
         from the realised trades — i.e. learn (a) a per-strategy expected edge
         (mean trade return) and (b) a confidence floor calibrated so that the
         lowest-quartile train signals are filtered out on the test slice.
      2. *Freeze* those params before the test slice runs.
      3. If the train fit fails the acceptance gate (no trades, negative edge,
         or profit factor < 1.0) the test window is *skipped* (zeroed metrics)
         to avoid pretending an unfit strategy generalised. This makes the
         training step have an actually measurable effect on the holdout.

    The strategies in this repo are rule-based with no learnable parameters,
    so we deliberately do not claim to be fitting model weights — we are
    calibrating a thin acceptance/threshold layer on top of the rule. That
    layer is real, frozen between train and test, and observable in the
    output (`fitted_params`, `test_skipped`).
    """

    def __init__(self, backtest_engine: BacktestEngine) -> None:
        self.backtest_engine = backtest_engine

    def evaluate(
        self,
        strategy_name: str,
        start: date,
        total_days: int,
        underlyings: tuple[str, ...],
        starting_capital: float,
        max_drawdown: float,
        train_days: int = 20,
        test_days: int = 10,
    ) -> WalkForwardResult:
        windows: list[WalkForwardWindow] = []
        window_index = 0
        cursor = start

        while True:
            train_start = cursor
            test_start = cursor + _dt.timedelta(days=train_days)
            test_end = test_start + _dt.timedelta(days=test_days - 1)

            if (test_end - start).days >= total_days:
                break

            train_cfg = BacktestConfig(
                starting_capital=starting_capital,
                start=train_start,
                days=train_days,
                underlyings=underlyings,
                max_drawdown=max_drawdown,
                strategy_names=(strategy_name,),
            )
            train_result = self.backtest_engine.run(train_cfg)
            fitted = self._fit_from_train(train_result)

            if fitted.accept_strategy:
                test_cfg = BacktestConfig(
                    starting_capital=starting_capital,
                    start=test_start,
                    days=test_days,
                    underlyings=underlyings,
                    max_drawdown=max_drawdown,
                    strategy_names=(strategy_name,),
                )
                test_result = self.backtest_engine.run(
                    test_cfg, signal_filter=fitted_signal_filter(fitted)
                )
                test_metrics = test_result.metrics
                test_skipped = False
            else:
                # Honestly skip: no fit, no holdout claim.
                test_metrics = _zero_metrics(starting_capital)
                test_skipped = True

            windows.append(
                WalkForwardWindow(
                    window_index=window_index,
                    strategy_name=strategy_name,
                    train_start=train_start,
                    train_end=train_start + _dt.timedelta(days=train_days - 1),
                    test_start=test_start,
                    test_end=test_end,
                    train_metrics=train_result.metrics,
                    test_metrics=test_metrics,
                    fitted_params=fitted,
                    test_skipped=test_skipped,
                )
            )

            cursor = test_start
            window_index += 1

        return WalkForwardResult(
            strategy_name=strategy_name,
            total_days=total_days,
            train_days=train_days,
            test_days=test_days,
            underlyings=underlyings,
            windows=windows,
        )

    @staticmethod
    def _fit_from_train(train_result) -> WalkForwardFittedParams:
        """Derive frozen params from the train backtest. Pure function of train_result."""
        metrics = train_result.metrics
        confidences = [
            report.order.intent.signal.confidence
            for report in train_result.reports
            if report.risk_decision.approved
            and getattr(report.order.intent.signal, "confidence", None) is not None
        ]
        if confidences:
            confidences_sorted = sorted(confidences)
            # Drop the bottom quartile: index len//4 is the true Q1 boundary.
            q1_index = len(confidences_sorted) // 4
            confidence_floor = confidences_sorted[q1_index]
        else:
            confidence_floor = 0.55

        # Expected edge: simple mean realized return per trade in train.
        expected_edge = 0.0
        if metrics.trade_count > 0:
            # PerformanceMetrics.return_pct is whole-portfolio; per-trade edge
            # approximation: total return divided by trade count.
            expected_edge = metrics.return_pct / max(1, metrics.trade_count)

        accept = (
            metrics.trade_count >= 5
            and metrics.profit_factor >= 1.2
            and metrics.return_pct > 0.0
        )
        return WalkForwardFittedParams(
            confidence_floor=float(confidence_floor),
            expected_edge=float(expected_edge),
            train_profit_factor=float(metrics.profit_factor),
            train_sharpe=float(metrics.sharpe_like),
            train_trade_count=int(metrics.trade_count),
            accept_strategy=bool(accept),
        )


def fitted_signal_filter(fitted: WalkForwardFittedParams):
    """Build a frozen signal-acceptance callable from train-fit params."""
    floor = fitted.confidence_floor

    def _accept(signal) -> bool:
        if signal is None:
            return False
        return getattr(signal, "confidence", 0.0) >= floor

    return _accept


def _zero_metrics(starting_capital: float) -> PerformanceMetrics:
    return PerformanceMetrics(
        starting_capital=starting_capital,
        ending_equity=starting_capital,
        total_pnl=0.0,
        return_pct=0.0,
        max_drawdown=0.0,
        trade_count=0,
        win_rate=0.0,
        profit_factor=0.0,
        sharpe_like=0.0,
    )
