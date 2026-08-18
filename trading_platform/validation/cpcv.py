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
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from numpy.typing import NDArray

from trading_platform.validation.gates import deflated_sharpe_ratio

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
                (train_indices < embargo_lower) | (train_indices >= embargo_upper)
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

    def get_combinatorial_paths(self, n_test_groups: int = 2) -> List[Tuple[int, ...]]:
        """
        Get CPCV combinatorial paths per López de Prado, AFML Ch. 12: every
        combination of `n_test_groups` base folds used TOGETHER as one combined
        test set. Returns C(n_folds, n_test_groups) paths — e.g. C(10,2)=45 —
        not the full 2**n_folds-1 power set (that's not what a "CPCV path" is).
        """
        from itertools import combinations

        return list(combinations(range(self.n_folds), n_test_groups))

    def build_path(self, path: Tuple[int, ...], n_samples: int) -> Fold:
        """
        Assemble one combinatorial train/test split for `path` — a tuple of base-fold
        indices whose test windows are combined into ONE test set. Train indices are
        every OTHER base fold's range, purged + embargoed around EACH path fold's own
        test boundary (generalizes generate_folds()'s single-test-block purge/embargo
        math to N combined test blocks).
        """
        fold_size = n_samples // self.n_folds
        path_set = set(path)

        def fold_range(k: int) -> Tuple[int, int]:
            start = k * fold_size
            end = min((k + 1) * fold_size, n_samples)
            return start, end

        test_indices = (
            np.concatenate([np.arange(*fold_range(i)) for i in sorted(path_set)])
            if path_set else np.array([], dtype=np.int64)
        )

        train_indices_list: List[int] = []
        for j in range(self.n_folds):
            if j not in path_set:
                start, end = fold_range(j)
                train_indices_list.extend(range(start, end))
        train_indices = np.array(train_indices_list, dtype=np.int64)

        for i in sorted(path_set):
            test_start, test_end = fold_range(i)
            purge_lower = max(0, test_start - self.purge_depth)
            purge_upper = min(n_samples, test_end + self.purge_depth)
            train_indices = train_indices[
                (train_indices < purge_lower) | (train_indices >= purge_upper)
            ]
            embargo_lower = test_end
            embargo_upper = min(n_samples, test_end + self.embargo_size)
            train_indices = train_indices[
                (train_indices < embargo_lower) | (train_indices >= embargo_upper)
            ]

        return Fold(
            fold_id=-1,  # combinatorial path, not a single base fold
            train_indices=train_indices,
            test_indices=test_indices,
            purge_depth=self.purge_depth,
            embargo_size=self.embargo_size,
        )


@dataclass
class PathVariantResult:
    """One (combinatorial path, param-grid variant) trial outcome — the unit
    real CPCV/PBO operates on. Produced by CPCVValidator.run_validation()."""
    path: Tuple[int, ...]
    params: Dict[str, Any]
    is_metric: float
    oos_metric: float
    oos_auc: float
    returns: NDArray[np.float64]
    n_trades: int = 0
    max_drawdown: float = 0.0


@dataclass
class PathPBOResult:
    pbo: float
    n_paths: int
    n_variants: int
    logits: List[float] = field(default_factory=list)
    insufficient_trials: bool = False


class ProbabilityOfOverfittingProcessor:
    """
    Probability of Backtest Overfitting (PBO) per Bailey, Borwein, López de
    Prado & Zhu (2015), "The Probability of Backtest Overfitting" — the real
    CSCV algorithm, applied here to CPCVValidator's (path x variant) grid.

    PBO > 0.4 → backtest is overfit, reject.
    """

    @staticmethod
    def calculate_from_path_variants(results: List[PathVariantResult]) -> PathPBOResult:
        """
        For each combinatorial path: find the IS-best variant (max is_metric),
        then find that variant's rank among all variants' oos_metric FOR THAT
        SAME PATH. w_c = relative rank in (0,1); lambda_c = ln(w_c/(1-w_c)).
        PBO = fraction of paths where lambda_c <= 0 (the IS-winner performs at
        or below the OOS median — i.e. overfit on that path).

        Requires >=2 distinct param-grid variants (else there's nothing to
        rank) — a single-model run (param_grid=[{}]) reports
        insufficient_trials=True rather than a fabricated PBO value.
        """
        by_path: Dict[Tuple[int, ...], List[PathVariantResult]] = {}
        variant_keys = set()
        for r in results:
            by_path.setdefault(r.path, []).append(r)
            variant_keys.add(tuple(sorted(r.params.items())))

        n_variants = len(variant_keys)
        if n_variants < 2 or not by_path:
            return PathPBOResult(pbo=0.0, n_paths=0, n_variants=n_variants, insufficient_trials=True)

        logits: List[float] = []
        for variants in by_path.values():
            if len(variants) < 2:
                continue
            is_best = max(variants, key=lambda v: v.is_metric)
            oos_sorted = sorted(variants, key=lambda v: v.oos_metric)
            rank = oos_sorted.index(is_best) + 1  # 1-based, 1 = worst OOS performer
            n = len(variants)
            w = min(max(rank / (n + 1), 1e-6), 1 - 1e-6)  # avoid exact 0/1 logit blowup
            logits.append(float(np.log(w / (1 - w))))

        if not logits:
            return PathPBOResult(pbo=0.0, n_paths=0, n_variants=n_variants, insufficient_trials=True)

        pbo = sum(1 for lam in logits if lam <= 0) / len(logits)
        return PathPBOResult(pbo=pbo, n_paths=len(logits), n_variants=n_variants, logits=logits)


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

    def evaluate_gate(
        self, name: str, value: float, threshold: float, message: str, direction: str = "gte",
    ) -> Gate:
        """Evaluate and record a gate.

        direction="gte" (default): PASS if value >= threshold (higher-is-better,
        e.g. AUC/DSR/Sharpe/trade-count). direction="lte": PASS if value <=
        threshold (lower-is-better, e.g. PBO/max_drawdown) — passing "gte" for
        these previously made PBO=0.5 vs threshold=0.4 PASS when it must FAIL.
        """
        passed = value <= threshold if direction == "lte" else value >= threshold
        result = GateResult.PASS if passed else GateResult.FAIL
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
        variant_fn: Callable[[Dict[str, Any], NDArray, NDArray, NDArray, NDArray], Dict[str, Any]],
        param_grid: Optional[List[Dict[str, Any]]] = None,
        n_test_groups: int = 2,
    ) -> CPCVResult:
        """
        Run full CPCV validation pipeline over real combinatorial paths.

        Args:
            X_train: Feature matrix
            y_train: Labels
            variant_fn: (params, X_tr, y_tr, X_te, y_te) -> {"is_metric", "oos_metric",
                "oos_auc" (optional), "returns": np.ndarray, "n_trades" (optional),
                "max_drawdown" (optional)}. Called once per (path, param combo) — trials
                are evaluated INSIDE each combinatorial fold, never on the full sample
                (§4.4b "Optuna tuning only inside folds").
            param_grid: trial configurations to compare. Defaults to [{}] (a single
                model/strategy, no sweep) — PBO then reports insufficient_trials=True
                since overfitting-probability needs >=2 variants to rank against
                each other.
            n_test_groups: base folds combined per CPCV path (C(n_folds, n_test_groups)
                total paths).

        Returns:
            CPCVResult with all gates evaluated
        """
        param_grid = param_grid or [{}]
        logger.info(f"CPCV: Starting validation for model {self.model_version}")

        n_samples = len(X_train)
        base_folds = self._cpcv.generate_folds(n_samples)
        if not base_folds:
            raise ValueError("No valid folds generated — increase min_train_size or reduce n_folds")

        paths = self._cpcv.get_combinatorial_paths(n_test_groups=n_test_groups)
        if not paths:
            raise ValueError("No combinatorial paths generated — check n_folds/n_test_groups")

        path_variant_results: List[PathVariantResult] = []
        fold_results: List[Dict[str, Any]] = []
        for path in paths:
            built = self._cpcv.build_path(path, n_samples)
            if (
                len(built.train_indices) < self._cpcv.min_train_size
                or len(built.test_indices) < self._cpcv.min_test_size
            ):
                continue
            X_tr, y_tr = X_train[built.train_indices], y_train[built.train_indices]
            X_te, y_te = X_train[built.test_indices], y_train[built.test_indices]
            for params in param_grid:
                outcome = variant_fn(params, X_tr, y_tr, X_te, y_te)
                returns = np.asarray(outcome.get("returns", np.array([])), dtype=np.float64)
                pvr = PathVariantResult(
                    path=path,
                    params=params,
                    is_metric=float(outcome.get("is_metric", 0.0)),
                    oos_metric=float(outcome.get("oos_metric", 0.0)),
                    oos_auc=float(outcome.get("oos_auc", outcome.get("oos_metric", 0.0))),
                    returns=returns,
                    n_trades=int(outcome.get("n_trades", 0)),
                    max_drawdown=float(outcome.get("max_drawdown", 0.0)),
                )
                path_variant_results.append(pvr)
                fold_results.append({
                    "path": path,
                    "params": params,
                    "train_size": len(built.train_indices),
                    "test_size": len(built.test_indices),
                    "auc": pvr.oos_auc,
                    "sharpe": pvr.oos_metric,
                    "max_drawdown": pvr.max_drawdown,
                    "n_trades": pvr.n_trades,
                })

        if not path_variant_results:
            raise ValueError(
                "No (path, variant) results produced — check min_train_size/"
                "min_test_size vs n_test_groups for this n_samples"
            )

        oos_aucs = [r.oos_auc for r in path_variant_results]
        oos_sharpes = [r.oos_metric for r in path_variant_results]
        oos_drawdowns = [r.max_drawdown for r in path_variant_results]
        out_of_sample_metrics = {
            "mean_oos_auc": float(np.mean(oos_aucs)),
            "std_oos_auc": float(np.std(oos_aucs)),
            "mean_oos_sharpe": float(np.mean(oos_sharpes)),
            "std_oos_sharpe": float(np.std(oos_sharpes)),
            "max_oos_drawdown": float(np.max(oos_drawdowns)) if oos_drawdowns else 0.0,
            "min_oos_sharpe": float(np.min(oos_sharpes)),
            "n_paths": len(paths),
            "n_variants": len(param_grid),
            "n_path_variant_results": len(path_variant_results),
        }

        # DSR — delegates to the single canonical implementation (gates.py); no
        # duplicate/self-permutation math here.
        best = max(path_variant_results, key=lambda r: r.oos_metric)
        trial_sharpes = [r.oos_metric for r in path_variant_results]
        returns_with_data = [r.returns for r in path_variant_results if len(r.returns) > 0]
        all_returns = np.concatenate(returns_with_data) if returns_with_data else best.returns
        dsr_result = deflated_sharpe_ratio(best.oos_metric, trial_sharpes, all_returns)
        dsr = dsr_result.dsr

        # PBO — real combinatorial-path CSCV over the (path x variant) grid.
        pbo_result = ProbabilityOfOverfittingProcessor.calculate_from_path_variants(path_variant_results)
        pbo = pbo_result.pbo

        self.gatekeeper.evaluate_gate(
            "min_oos_auc", out_of_sample_metrics["mean_oos_auc"], self.gatekeeper.min_auc,
            f"OOS AUC must be >= {self.gatekeeper.min_auc}",
        )
        self.gatekeeper.evaluate_gate(
            "dsr", dsr, self.gatekeeper.min_dsr, f"Deflated Sharpe >= {self.gatekeeper.min_dsr}",
        )
        self.gatekeeper.evaluate_gate(
            "pbo", pbo, self.gatekeeper.max_pbo, f"PBO <= {self.gatekeeper.max_pbo}", direction="lte",
        )
        self.gatekeeper.evaluate_gate(
            "min_walkforward_sharpe", out_of_sample_metrics["mean_oos_sharpe"],
            self.gatekeeper.min_walkforward_sharpe,
            f"Walk-forward Sharpe >= {self.gatekeeper.min_walkforward_sharpe}",
        )
        self.gatekeeper.evaluate_gate(
            "max_drawdown", out_of_sample_metrics["max_oos_drawdown"],
            self.gatekeeper.max_drawdown_threshold,
            f"Max DD <= {self.gatekeeper.max_drawdown_threshold}", direction="lte",
        )
        self.gatekeeper.evaluate_gate(
            "min_trades", float(sum(r.n_trades for r in path_variant_results)),
            float(self.gatekeeper.min_trades), f"Total trades >= {self.gatekeeper.min_trades}",
        )

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
            f"CPCV: Validation {'PASSED' if result.all_passed else 'FAILED'} for "
            f"model {self.model_version}"
        )
        logger.info(f"CPCV: OOS AUC={out_of_sample_metrics['mean_oos_auc']:.4f}, "
                    f"DSR={dsr:.4f}, PBO={pbo:.4f}")

        return result