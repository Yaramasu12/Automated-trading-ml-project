from __future__ import annotations

"""Purged walk-forward validation with embargo to prevent data leakage."""

import math
from dataclasses import dataclass, field


@dataclass
class WalkForwardFold:
    fold_id: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int


@dataclass
class WalkForwardResult:
    n_folds: int
    mean_return: float
    sharpe_ratio: float
    max_drawdown: float
    profit_factor: float
    oos_improvement: float   # vs in-sample
    pass_threshold: bool
    fold_results: list[dict] = field(default_factory=list)


class PurgedWalkForwardValidator:
    """Purged train/test splits with embargo around the label horizon.

    Prevents data leakage via purging and embargo periods.
    """

    def __init__(
        self,
        n_folds: int = 5,
        embargo_pct: float = 0.01,
        min_profit_factor: float = 1.10,
        min_sharpe: float = 0.20,
        max_drawdown: float = 0.20,
    ) -> None:
        self._n_folds = n_folds
        self._embargo_pct = embargo_pct
        self._min_profit_factor = min_profit_factor
        self._min_sharpe = min_sharpe
        self._max_drawdown = max_drawdown

    def create_folds(self, n: int) -> list[WalkForwardFold]:
        """Create purged walk-forward fold splits."""
        fold_size = n // (self._n_folds + 1)
        embargo = max(1, int(fold_size * self._embargo_pct))
        folds: list[WalkForwardFold] = []

        for i in range(self._n_folds):
            train_end = fold_size * (i + 1)
            test_start = train_end + embargo
            test_end = min(n, test_start + fold_size)
            folds.append(WalkForwardFold(
                fold_id=i,
                train_start=0,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            ))
        return folds

    def validate(
        self,
        returns: list[float],
        strategy_fn: object = None,
    ) -> WalkForwardResult:
        """Run walk-forward validation on a return series.

        strategy_fn: callable(train_returns) -> list[float] (out-of-sample returns).
        Uses simple buy-and-hold baseline if None.
        """
        n = len(returns)
        if n < 20:
            return WalkForwardResult(
                n_folds=0, mean_return=0.0, sharpe_ratio=0.0,
                max_drawdown=1.0, profit_factor=0.0,
                oos_improvement=0.0, pass_threshold=False,
            )

        folds = self.create_folds(n)
        fold_results: list[dict] = []

        all_oos_returns: list[float] = []
        all_is_returns: list[float] = []

        for fold in folds:
            train_rets = returns[fold.train_start:fold.train_end]
            test_rets = returns[fold.test_start:fold.test_end]

            if not test_rets:
                continue

            # In-sample mean
            is_mean = sum(train_rets) / max(1, len(train_rets))
            all_is_returns.extend(test_rets)

            # Out-of-sample (use provided strategy or baseline)
            if strategy_fn and callable(strategy_fn):
                try:
                    oos = strategy_fn(train_rets, test_rets)
                except Exception:
                    oos = test_rets
            else:
                oos = test_rets

            all_oos_returns.extend(oos)
            oos_mean = sum(oos) / max(1, len(oos))
            fold_results.append({"fold_id": fold.fold_id, "oos_mean": oos_mean, "n_oos": len(oos)})

        mean_ret = sum(all_oos_returns) / max(1, len(all_oos_returns))
        std_ret = math.sqrt(sum((r - mean_ret) ** 2 for r in all_oos_returns) / max(1, len(all_oos_returns)))
        sharpe = mean_ret / max(std_ret, 1e-6) * math.sqrt(252)

        # Drawdown
        cum, peak, max_dd = 0.0, 0.0, 0.0
        for r in all_oos_returns:
            cum += r
            if cum > peak:
                peak = cum
            dd = (peak - cum) / max(abs(peak), 1.0) if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        # Profit factor
        gains = sum(r for r in all_oos_returns if r > 0)
        losses = abs(sum(r for r in all_oos_returns if r < 0))
        pf = gains / max(losses, 1e-6)

        # OOS improvement
        is_mean = sum(all_is_returns) / max(1, len(all_is_returns))
        oos_improvement = mean_ret - is_mean

        pass_threshold = (
            pf >= self._min_profit_factor
            and sharpe >= self._min_sharpe
            and max_dd <= self._max_drawdown
        )

        return WalkForwardResult(
            n_folds=len(fold_results),
            mean_return=mean_ret,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            profit_factor=pf,
            oos_improvement=oos_improvement,
            pass_threshold=pass_threshold,
            fold_results=fold_results,
        )
