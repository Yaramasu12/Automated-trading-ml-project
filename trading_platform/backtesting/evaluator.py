from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass
from datetime import date

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
            )
            for index, item in enumerate(ranked)
        ]
        return StrategyEvaluationResult(start=start, days=days, underlyings=underlyings, leaderboard=ranked)

    def _score(self, metrics: PerformanceMetrics) -> float:
        profit_quality = min(3.0, metrics.profit_factor) * 0.15
        risk_adjusted = metrics.sharpe_like * 0.10
        return metrics.return_pct + profit_quality + risk_adjusted - (metrics.max_drawdown * 1.5)


# ---------------------------------------------------------------------------
# Walk-forward validation
# ---------------------------------------------------------------------------


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
    """Rolling train/test walk-forward validation for a single strategy.

    Splits `total_days` into non-overlapping windows of size `train_days + test_days`,
    runs a backtest in each window, and reports per-window and aggregate metrics.
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
            test_cfg = BacktestConfig(
                starting_capital=starting_capital,
                start=test_start,
                days=test_days,
                underlyings=underlyings,
                max_drawdown=max_drawdown,
                strategy_names=(strategy_name,),
            )

            train_result = self.backtest_engine.run(train_cfg)
            test_result = self.backtest_engine.run(test_cfg)

            windows.append(
                WalkForwardWindow(
                    window_index=window_index,
                    strategy_name=strategy_name,
                    train_start=train_start,
                    train_end=train_start + _dt.timedelta(days=train_days - 1),
                    test_start=test_start,
                    test_end=test_end,
                    train_metrics=train_result.metrics,
                    test_metrics=test_result.metrics,
                )
            )

            # Advance cursor by one test window (anchored expansion)
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
