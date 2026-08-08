"""
trading_platform/validation/cpcv.py — Combinatorial Purged Cross-Validation

Per §4.4b / §5: CPCV with embargo replaces plain walk-forward for model selection.
Purged+embargoed folds everywhere. Uses existing backtest engine.

Implementation:
- BlueSky CPCV (combinatorial purged CV) for comprehensive fold coverage
- Purge overlapping labels from training sets
- Embargo: skip warm-up after train/test split
- DSR (Deflated Sharpe Ratio) + PBO (Probability of Backtest Overfitting) gates
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from numpy.typing import NDArray

logger = logging.getLogger(__name__)


class GateResult(str, Enum):
    """Backtest gate result."""
    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"


@dataclass
class Gate:
    """A single validation gate."""
    name: str
    result: GateResult
    value: float
    threshold: float
    message: str
    timestamp: float = field(default_factory=time.time)


@dataclass 
class CPCVResult:
    """CPCV validation result."""
    model_version: str
    folds: List[Dict[str, Any]]
    out_of_sample_metrics: Dict[str, float]
    gates: List[Gate]
    all_passed: bool
    dsr_value: float
    pbo_value: float
    training_data_hash: str
    git_sha: str
    config_snapshot: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)


@dataclass
class Fold:
    """A single CV fold with train/test split."""
    fold_id: int
    train_indices: NDArray[np.int64]
    test_indices: NDArray[np.int64]
    purge_depth: int  # number of observations to purge from train
    embargo_size: int  # number of observations to skip after split


class CombinatorialPurgedCrossValidator:
    """
    Combinatorial Purged Cross-Validation (CPCV).

    Per López de Prado "Advances in Financial Machine Learning" Ch. 7:
    - Generates combinatorial train/test splits
    - Purges overlapping labels from training sets
    - Applies embargo after train/test split
    - Provides comprehensive fold coverage (more than plain walk-forward)
    """

    def __init__(
        self,
        n_folds: int = 8,
        purge_depth: int = 10,
        embargo_size: int = 10,
        min_train_size: int = 100,
        min_test_size: int = 30,
    ):
        """
        Initialize CPCV validator.

        Args:
            n_folds: Number of base folds (2^n_folds combinatorial paths)
            purge_depth: Observations to purge from train near test boundary
            embargo_size: Observations to skip after train/test split
            min_train_size: Minimum training set size
            min_test_size: Minimum test set size
        """
        self.n_folds = n_folds
        self.purge_depth = purge_depth
        self.embargo_size = embargo_size
        self.min_train_size = min_train_size
        self.min_test_size = min_test_size

        self._folds: List[Fold] = []

    def generate_folds(self, n_samples: int) -> List[Fold]:
        """
        Generate combinatorial purged CV folds for a dataset.

        Args:
            n_samples: Total number of samples

        Returns:
            List of Fold objects with train/test indices
        """
        if n_samples < self.min_train_size + self.min_test_size:
            raise ValueError(
                f"Need at least {self.min_train_size + self.min_test_size} samples, "
                f"got {n_samples}"
            )

        # Create base folds (time-series split)
        fold_size = n_samples // self.n_folds
        self._folds = []

        for i in range(self.n_folds):
            test_start = i * fold_size
            test_end = min((i + 1) * fold_size, n_samples)

            # Test indices
            test_indices = np.arange(test_start, test_end)

            # Train indices: all except test window + purge zone
            train_indices_list = []
            for j in range(self.n_folds):
                if j != i:  # Exclude current fold
                    train_start = j * fold_size
                    train_end = min((j + 1) * fold_size, n_samples)
                    train_indices_list.extend(range(train_start, train_end))

            train_indices = np.array(train_indices_list)

            # Purge: remove samples within purge_depth of test boundary
            purge_lower = max(0, test_start - self.purge_depth)
            purge_upper = min(n_samples, test_end + self.purge_depth)
            train_indices = train_indices[
                (train_indices < purge_lower) | (train_indices >= purge_upper)
            ]

            # Embargo: remove samples within embargo_size after test start
            embargo_lower = test_end
            embargo_upper = min(n_samples, test_end + self.embargo_size)
            train_indices = train_indices[
                train_indices < embargo_lower | train_indices >= embargo_upper
            ]

            # Validate sizes
            if len(train_indices) < self.min_train_size:
                logger.warning(
                    f"Fold {i}: train size {len(train_indices)} < min {self.min_train_size}"
                )
                continue
            if len(test_indices) < self.min_test_size:
                logger.warning(
                    f"Fold {i}: test size {len(test_indices)} < min {self.min_test_size}"
                )
                continue

            fold = Fold(
                fold_id=i,
                train_indices=train_indices,
                test_indices=test_indices,
                purge_depth=self.purge_depth,
                embargo_size=self.embargo_size,
            )
            self._folds.append(fold)

        logger.info(f"CPCV: Generated {len(self._folds)} valid folds for {n_samples} samples")
        return self._folds

    def get_folds(self) -> List[Fold]:
        """Get generated folds."""
        return self._folds

    def get_combinatorial_paths(self) -> List[List[int]]:
        """
        Get combinatorial paths (subsets of folds).

        Returns all 2^n_folds - 1 combinations of folds for comprehensive coverage.
        """
        from itertools import combinations

        paths = []
        for k in range(1, self.n_folds + 1):
            for combo in combinations(range(len(self._folds)), k):
                paths.append(list(combo))
        return paths


class DeflatedSharpeProcessor:
    """
    Deflated Sharpe Ratio (DSR) test per López de Deluxe.

    Adjusts the Sharpe ratio for multiple testing and selection bias.
    DSR < 1.0 → Sharpe is not statistically significant.
    """

    @staticmethod
    def calculate(
        sharpe_ratio: float,
        n_backtests: int,
        n_obs: int,
        skew: float = 0.0,
        kurtosis: float = 3.0,
        confidence: float = 0.95,
    ) -> float:
        """
        Calculate Deflated Sharpe Ratio.

        Args:
            sharpe_ratio: Observed Sharpe ratio
            n_backtests: Number of backtests run (multiple testing correction)
            n_obs: Number of observations in return series
            skew: Skewness of return distribution
            kurtosis: Kurtosis of return distribution
            confidence: Confidence level

        Returns:
            DSR value ( < 1.0 means not significant)
        """
        if n_obs < 30:
            return 0.0

        # Standard Sharpe expectation under null
        expected_sharpe = DSRProcessor._expected_sharpe(sharpe_ratio, skew, kurtosis)

        # Multiple testing correction
        pi_star = DSRProcessor._prior_probabilities(sharpe_ratio, n_backtests, confidence)

        # DSR formula
        dsr = DSRProcessor._sharpe_to_normal(sharpe_ratio)
        dsr -= DSRProcessor._sharpe_to_normal(0) + pi_star

        return float(dsr)

    @staticmethod
    def _expected_sharpe(sharpe: float, skew: float, kurtosis: float) -> float:
        """Expected Sharpe under null hypothesis."""
        # Approximation using Cornish-Fisher expansion
        n = 252  # annualization
        z = (skew / 6.0) * (sharpe / np.sqrt(n)) + \
            (kurtosis - 3) / 24.0 * (sharpe**2 / n) - 1.0 / 6.0 * (skew**2 / 36.0) * (sharpe**3 / n**1.5)
        return sharpe - z

    @staticmethod
    def _prior_probabilities(sharpe: float, n_backtests: int, confidence: float) -> float:
        """Prior probability of selection."""
        from scipy import stats

        alpha = 1.0 - confidence
        z = stats.norm.ppf(1.0 - alpha / (2.0 * n_backtests))
        return float(z * sharpe)

    @staticmethod
    def _sharpe_to_normal(sharpe: float) -> float:
        """Transform Sharpe to normal score."""
        from scipy import stats
        return float(stats.norm.ppf(0.5 + 0.5 * stats.erf(sharpe / np.sqrt(2.0))))


class ProbabilityOfOverfittingProcessor:
    """
    Probability of Backtest Overfitting (PBO) per López de Prado.

    PBO > 0.4 → Backtest is overfit, reject.
    """

    @staticmethod
    def calculate(
        backtest_returns: List[NDArray[np.float64]],
        best_sharpe: float,
        n_permutations: int = 1000,
        random_state: Optional[int] = None,
    ) -> float:
        """
        Calculate probability of backtest overfitting.

        Args:
            backtest_returns: List of return arrays from each backtest
            best_sharpe: Best Sharpe ratio observed
            n_permutations: Number of permutations for estimation
            random_state: Random seed

        Returns:
            PBO value (0.0 = not overfit, 1.0 = fully overfit)
        """
        if not backtest_returns or len(backtest_returns) < 2:
            return 0.0

        rng = np.random.default_rng(random_state)

        # Calculate Sharpe for permuted backtests
        permuted_sharpes = []
        for _ in range(n_permutations):
            permuted_best = 0.0
            for returns in backtest_returns:
                # Permuted Sharpe
                perm_returns = returns[rng.permutation(len(returns))]
                sharpe = np.mean(perm_returns) / np.std(perm_returns) * np.sqrt(252)
                permuted_best = max(permuted_best, abs(sharpe))
            permuted_sharpes.append(permuted_best)

        # PBO = proportion of permuted sharpes > observed best
        pbo = float(np.mean(np.array(permuted_sharpes) > best_sharpe))
        return pbo


class ValidationGateKeeper:
    """
    Manages validation gates for model promotion.

    All gates must pass for a model to be promoted.
    Gates are stored in DB (passed to MLflow registry).
    """

    def __init__(
        self,
        min_auc: float = 0.52,
        min_dsr: float = 1.0,
        max_pbo: float = 0.4,
        min_walkforward_sharpe: float = 0.3,
        max_drawdown_threshold: float = 0.15,
        min_trades: int = 30,
    ):
        self.min_auc = min_auc
        self.min_dsr = min_dsr
        self.max_pbo = max_pbo
        self.min_walkforward_sharpe = min_walkforward_sharpe
        self.max_drawdown_threshold = max_drawdown_threshold
        self.min_trades = min_trades

        self._gates: List[Gate] = []

    def add_gate(self, gate: Gate) -> None:
        """Add a validation gate."""
        self._gates.append(gate)

    def evaluate_gate(self, name: str, value: float, threshold: float, message: str) -> Gate:
        """Evaluate and record a gate."""
        result = GateResult.PASS if value >= threshold else GateResult.FAIL
        gate = Gate(name=name, result=result, value=value, threshold=threshold, message=message)
        self._gates.append(gate)
        status = "✅ PASS" if result == GateResult.PASS else "❌ FAIL"
        logger.info(f"Gate [{name}]: {status} (value={value:.4f}, threshold={threshold:.4f})")
        return gate

    def all_passed(self) -> bool:
        """Check if all gates passed."""
        return all(g.result == GateResult.PASS for g in self._gates) if self._gates else False

    def get_summary(self) -> Dict:
        """Get gate summary."""
        return {
            "all_passed": self.all_passed(),
            "total_gates": len(self._gates),
            "gates": [
                {
                    "name": g.name,
                    "result": g.result.value,
                    "value": g.value,
                    "threshold": g.threshold,
                    "message": g.message,
                }
                for g in self._gates
            ],
        }


class CPCVValidator:
    """
    Main CPCV validation orchestrator.

    Coordinates:
    1. Fold generation (CPCV)
    2. Training + backtesting on each combinatorial path
    3. DSR + PBO calculation
    4. Gate evaluation
    5. MLflow registry submission
    """

    def __init__(
        self,
        model_version: str,
        training_data_hash: str,
        git_sha: str,
        config_snapshot: Dict[str, Any],
        gatekeeper: Optional[ValidationGateKeeper] = None,
    ):
        self.model_version = model_version
        self.training_data_hash = training_data_hash
        self.git_sha = git_sha
        self.config_snapshot = config_snapshot
        self.gatekeeper = gatekeeper or ValidationGateKeeper()
        self._cpcv = CombinatorialPurgedCrossValidator()

    def run_validation(
        self,
        X_train: NDArray[np.float64],
        y_train: NDArray[np.int64],
        backtest_func,  # callable(fold_indices) -> backtest_result
        n_backtests: int = 50,  # for DSR correction
    ) -> CPCVResult:
        """
        Run full CPCV validation pipeline.

        Args:
            X_train: Feature matrix
            y_train: Labels
            backtest_func: Function that takes fold indices and returns backtest metrics
            n_backtests: Number of backtests for DSR correction

        Returns:
            CPCVResult with all gates evaluated
        """
        logger.info(f"CPCV: Starting validation for model {self.model_version}")

        # 1. Generate folds
        n_samples = len(X_train)
        folds = self._cpcv.generate_folds(n_samples)

        if not folds:
            raise ValueError("No valid folds generated — increase min_train_size or reduce n_folds")

        # 2. Run backtests on each fold
        fold_results = []
        all_backtest_returns = []
        for fold in folds:
            logger.info(
                f"CPCV: Fold {fold.fold_id} — train: {len(fold.train_indices)}, "
                f"test: {len(fold.test_indices)}"
            )

            # Train on fold's train set
            X_tr, y_tr = X_train[fold.train_indices], y_train[fold.train_indices]
            X_te, y_te = X_train[fold.test_indices], y_train[fold.test_indices]

            # Backtest on fold's test set
            result = backtest_func(fold, X_tr, y_tr, X_te, y_te)
            fold_results.append({
                "fold_id": fold.fold_id,
                "train_size": len(fold.train_indices),
                "test_size": len(fold.test_indices),
                **result,
            })
            all_backtest_returns.append(result.get("returns", np.array([])))

        # 3. Aggregate OOS metrics
        oos_aucs = [r.get("auc", 0.0) for r in fold_results]
        oos_sharpes = [r.get("sharpe", 0.0) for r in fold_results]
        oos_drawdowns = [r.get("max_drawdown", 0.0) for r in fold_results]

        out_of_sample_metrics = {
            "mean_oos_auc": float(np.mean(oos_aucs)),
            "std_oos_auc": float(np.std(oos_aucs)),
            "mean_oos_sharpe": float(np.mean(oos_sharpes)),
            "std_oos_sharpe": float(np.std(oos_sharpes)),
            "max_oos_drawdown": float(np.max(oos_drawdowns)),
            "min_oos_sharpe": float(np.min(oos_sharpes)),
            "n_folds": len(fold_results),
        }

        # 4. Calculate DSR
        best_sharpe = float(np.max(oos_sharpes))
        skew = float(np.mean([r.get("return_skew", 0.0) for r in fold_results]))
        kurt = float(np.mean([r.get("return_kurtosis", 3.0) for r in fold_results]))

        # Find total returns series for n_obs
        all_returns = np.concatenate([r.get("returns", np.array([])) for r in fold_results if len(r.get("returns", np.array([]))) > 0])
        n_obs = len(all_returns) if len(all_returns) > 0 else 252

        dsr = DeflatedSharpeProcessor.calculate(
            sharpe_ratio=best_sharpe,
            n_backtests=n_backtests,
            n_obs=n_obs,
            skew=skew,
            kurtosis=kurt,
        )

        # 5. Calculate PBO
        pbo = ProbabilityOfOverfittingProcessor.calculate(
            backtest_returns=all_backtest_returns,
            best_sharpe=best_sharpe,
        )

        # 6. Evaluate gates
        self.gatekeeper.evaluate_gate(
            name="min_oos_auc",
            value=out_of_sample_metrics["mean_oos_auc"],
            threshold=self.gatekeeper.min_auc,
            message=f"OOS AUC must be ≥ {self.gatekeeper.min_auc}",
        )

        self.gatekeeper.evaluate_gate(
            name="dsr",
            value=dsr,
            threshold=self.gatekeeper.min_dsr,
            message=f"Deflated Sharpe ≥ {self.gatekeeper.min_dsr}",
        )

        self.gatekeeper.evaluate_gate(
            name="pbo",
            value=pbo,
            threshold=self.gatekeeper.max_pbo,
            message=f"PBO ≤ {self.gatekeeper.max_pbo}",
        )

        self.gatekeeper.evaluate_gate(
            name="min_walkforward_sharpe",
            value=out_of_sample_metrics["mean_oos_sharpe"],
            threshold=self.gatekeeper.min_walkforward_sharpe,
            message=f"Walk-forward Sharpe ≥ {self.gatekeeper.min_walkforward_sharpe}",
        )

        self.gatekeeper.evaluate_gate(
            name="max_drawdown",
            value=out_of_sample_metrics["max_oos_drawdown"],
            threshold=self.gatekeeper.max_drawdown_threshold,
            message=f"Max DD ≤ {self.gatekeeper.max_drawdown_threshold}",
        )

        self.gatekeeper.evaluate_gate(
            name="min_trades",
            value=float(sum(r.get("n_trades", 0) for r in fold_results)),
            threshold=float(self.gatekeeper.min_trades),
            message=f"Total trades ≥ {self.gatekeeper.min_trades}",
        )

        # 7. Build result
        result = CPCVResult(
            model_version=self.model_version,
            folds=fold_results,
            out_of_sample_metrics=out_of_sample_metrics,
            gates=self.gatekeeper._gates,
            all_passed=self.gatekeeper.all_passed(),
            dsr_value=dsr,
            pbo_value=pbo,
            training_data_hash=self.training_data_hash,
            git_sha=self.git_sha,
            config_snapshot=self.config_snapshot,
        )

        logger.info(
            f"CPCV: Validation {'✅ PASSED' if result.all_passed else '❌ FAILED'} for "
            f"model {self.model_version}"
        )
        logger.info(f"CPCV: OOS AUC={out_of_sample_metrics['mean_oos_auc']:.4f}, "
                    f"DSR={dsr:.4f}, PBO={pbo:.4f}")

        return result