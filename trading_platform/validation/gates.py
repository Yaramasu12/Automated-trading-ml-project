"""
trading_platform/validation/gates.py — Backtest promotion gates

Per §5: Every strategy/model must pass enforced gates before reaching the money path:
1. Walk-forward optimization
2. CPCV (combinatorial purged CV) with embargo
3. Deflated Sharpe Ratio (DSR)
4. Probability of Backtest Overfitting (PBO) < 0.4
5. Monte Carlo trade-reshuffle → 95% max-DD within risk limits
6. Full India cost model (gross vs net)
7. ≥30 paper days → live at min size → scale per allocator

All gates stored in DB. No model deploys without passing all.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Gate results
# ──────────────────────────────────────────────


class GateResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


@dataclass
class GateOutcome:
    """Single gate result."""
    gate_name: str
    result: GateResult
    metric: float
    threshold: float
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.result in (GateResult.PASS, GateResult.WARN)


@dataclass
class BacktestGateResults:
    """All gate results for one backtest."""
    backtest_id: str
    strategy_id: str
    walk_forward: Optional[GateOutcome] = None
    cpcv: Optional[GateOutcome] = None
    dsr: Optional[GateOutcome] = None
    pbo: Optional[GateOutcome] = None
    monte_carlo: Optional[GateOutcome] = None
    cost_model: Optional[GateOutcome] = None
    paper_days: Optional[GateOutcome] = None
    promotion_ladder: Optional[GateOutcome] = None

    @property
    def all_passed(self) -> bool:
        """All gates that were evaluated must pass."""
        gates = [
            self.walk_forward, self.cpcv, self.dsr,
            self.pbo, self.monte_carlo, self.cost_model,
            self.paper_days, self.promotion_ladder,
        ]
        return all(g.passed for g in gates if g is not None)

    @property
    def any_failed(self) -> bool:
        return any(g and g.result == GateResult.FAIL for g in [
            self.walk_forward, self.cpcv, self.dsr,
            self.pbo, self.monte_carlo, self.cost_model,
        ])

    def summary(self) -> Dict[str, Any]:
        return {
            "backtest_id": self.backtest_id,
            "strategy_id": self.strategy_id,
            "all_passed": self.all_passed,
            "any_failed": self.any_failed,
            "gates": {
                g.gate_name: {
                    "result": g.result.value,
                    "metric": g.metric,
                    "threshold": g.threshold,
                    "message": g.message,
                }
                for g in [
                    self.walk_forward, self.cpcv, self.dsr,
                    self.pbo, self.monte_carlo, self.cost_model,
                    self.paper_days, self.promotion_ladder,
                ]
                if g is not None
            }
        }


# ──────────────────────────────────────────────
# Deflated Sharpe Ratio (DSR)
# ──────────────────────────────────────────────


@dataclass
class DSRResult:
    dsr: float                  # Phi(z) in [0,1] — probability true Sharpe > 0 net of selection bias
    observed_sharpe: float
    expected_max_sharpe: float  # SR_0 — expected max Sharpe under N iid null trials
    sharpe_variance: float      # V[SR_n] across trials
    n_trials: int
    skew: float
    kurtosis: float
    n_obs: int
    # True when the inputs were too thin to deflate at all (T<30 or n_trials<2).
    # Distinguishes "we could not evaluate this" from "this scored badly" —
    # the gate reports the former as SKIP, not FAIL.
    insufficient_data: bool = False


def deflated_sharpe_ratio(
    sharpe_hat: float,
    trial_sharpes: Any,  # Sequence[float] — every trial's Sharpe, including the selected one
    returns: np.ndarray,
    *,
    annualization_factor: float = 252.0,
) -> DSRResult:
    """
    Deflated Sharpe Ratio per Bailey & Lopez de Prado (2014), "The Deflated
    Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting, and
    Non-Normality".

    DSR = Phi( (SR_hat - SR_0) * sqrt(T-1) / sqrt(1 - g3*SR_hat + (g4-1)/4*SR_hat^2) )

    SR_0 = sqrt(V[SR_n]) * ( (1-gamma_EM)*Phi^-1(1-1/N) + gamma_EM*Phi^-1(1-1/(N*e)) )
    is the expected maximum Sharpe of N iid N(0, V[SR_n]) trials (Euler-Mascheroni
    approximation of the expected max of N Gaussians) — this is what "deflates"
    a Sharpe that was the best of many trials, unlike naively testing SR_hat
    against zero.

    trial_sharpes supplies N and V[SR_n] (the trial-selection variance).
    returns is the OOS return series of the SELECTED strategy — supplies T
    (sample size) and skew/kurtosis (g3/g4, non-excess convention: normal
    data -> kurtosis=3) for the non-normality correction.

    T<30 or n_trials<2 -> DSR reported as 0.0 (insufficient data to deflate),
    not silently skipped — callers must not treat 0.0 as "passed".
    """
    returns = np.asarray(returns, dtype=np.float64)
    t_obs = len(returns)
    n_trials = len(trial_sharpes)

    if t_obs < 30 or n_trials < 2:
        return DSRResult(
            dsr=0.0, observed_sharpe=sharpe_hat, expected_max_sharpe=0.0,
            sharpe_variance=0.0, n_trials=n_trials, skew=0.0, kurtosis=3.0, n_obs=t_obs,
            insufficient_data=True,
        )

    from scipy import stats as _stats

    sr_var = float(np.var(np.asarray(trial_sharpes, dtype=np.float64), ddof=1))
    euler_mascheroni = 0.5772156649015329
    if sr_var <= 0 or n_trials < 2:
        sr0 = 0.0
    else:
        sr0 = math.sqrt(sr_var) * (
            (1 - euler_mascheroni) * _stats.norm.ppf(1 - 1.0 / n_trials)
            + euler_mascheroni * _stats.norm.ppf(1 - 1.0 / (n_trials * math.e))
        )

    skew = float(_stats.skew(returns))
    kurt = float(_stats.kurtosis(returns, fisher=False))  # non-excess: normal data -> 3.0
    denom_sq = 1 - skew * sharpe_hat + ((kurt - 1) / 4.0) * sharpe_hat ** 2
    denom = math.sqrt(max(1e-12, denom_sq))
    z = (sharpe_hat - sr0) * math.sqrt(t_obs - 1) / denom
    dsr = float(_stats.norm.cdf(z))

    return DSRResult(
        dsr=dsr, observed_sharpe=sharpe_hat, expected_max_sharpe=sr0,
        sharpe_variance=sr_var, n_trials=n_trials, skew=skew, kurtosis=kurt, n_obs=t_obs,
    )


# ──────────────────────────────────────────────
# Probability of Backtest Overfitting (PBO)
# ──────────────────────────────────────────────


@dataclass
class PBOResult:
    pbo: float               # fraction of CSCV splits where the IS-winner is OOS-median-or-worse
    n_splits: int
    n_variants: int
    logits: List[float] = field(default_factory=list)


def probability_of_backtest_overfitting(
    returns_matrix: np.ndarray,   # shape (T periods, N variants)
    n_groups: int = 8,            # S — must be even; C(S, S/2) splits generated
) -> PBOResult:
    """
    CSCV / PBO per Bailey, Borwein, Lopez de Prado & Zhu (2015), "The
    Probability of Backtest Overfitting".

    Splits the T periods into n_groups contiguous blocks. For every
    combination of n_groups/2 blocks used as the in-sample (IS) set (complement
    = out-of-sample, OOS): find the IS-best variant (by mean IS return), locate
    its OOS rank among all N variants' OOS performance, w_c = relative rank in
    (0,1), lambda_c = ln(w_c / (1-w_c)). PBO = fraction of splits with
    lambda_c <= 0 (the IS-winner performs at/below the OOS median on that split
    — evidence of overfitting rather than genuine skill).

    This is the strategy-parameter-sweep counterpart of
    validation.cpcv.ProbabilityOfOverfittingProcessor.calculate_from_path_variants
    — same CSCV algorithm, different combinatorial unit (time-blocks x
    strategy variants here, vs purge/embargo folds of one series there).
    """
    from itertools import combinations

    returns_matrix = np.asarray(returns_matrix, dtype=np.float64)
    if returns_matrix.ndim != 2:
        raise ValueError("returns_matrix must be 2D: (T periods, N variants)")
    t_obs, n_variants = returns_matrix.shape
    if n_variants < 2:
        return PBOResult(pbo=0.0, n_splits=0, n_variants=n_variants)
    if n_groups % 2 != 0:
        n_groups -= 1
    n_groups = max(2, min(n_groups, t_obs))

    block_size = t_obs // n_groups
    if block_size < 1:
        return PBOResult(pbo=0.0, n_splits=0, n_variants=n_variants)
    blocks = [
        returns_matrix[i * block_size: (i + 1) * block_size if i < n_groups - 1 else t_obs]
        for i in range(n_groups)
    ]

    half = n_groups // 2
    logits: List[float] = []
    for is_block_ids in combinations(range(n_groups), half):
        oos_block_ids = [b for b in range(n_groups) if b not in is_block_ids]
        is_returns = np.concatenate([blocks[b] for b in is_block_ids], axis=0)
        oos_returns = np.concatenate([blocks[b] for b in oos_block_ids], axis=0)

        is_perf = is_returns.mean(axis=0)   # (N,) — mean IS return per variant
        oos_perf = oos_returns.mean(axis=0)  # (N,) — mean OOS return per variant

        best_variant = int(np.argmax(is_perf))
        oos_rank = int(np.sum(oos_perf <= oos_perf[best_variant]))  # 1..N, 1=worst
        w = min(max(oos_rank / (n_variants + 1), 1e-6), 1 - 1e-6)
        logits.append(float(np.log(w / (1 - w))))

    if not logits:
        return PBOResult(pbo=0.0, n_splits=0, n_variants=n_variants)

    pbo = sum(1 for lam in logits if lam <= 0) / len(logits)
    return PBOResult(pbo=pbo, n_splits=len(logits), n_variants=n_variants, logits=logits)


# ──────────────────────────────────────────────
# Monte Carlo trade-reshuffle
# ──────────────────────────────────────────────


def monte_carlo_dd_estimate(
    trade_list: List[Dict[str, Any]],
    confidence: float = 0.95,
    num_simulations: int = 1000,
    seed: int = 42,
    starting_capital: float = 1_000_000.0,
) -> float:
    """
    Monte Carlo trade reshuffle → 95% max drawdown estimate, as a FRACTION of
    starting capital (0.0-1.0), comparable against GATE_MAX_DRAWDOWN.

    Parameters:
        trade_list: list of {pnl, ...} dicts
        confidence: confidence level (default 0.95)
        num_simulations: number of reshuffles
        seed: random seed
        starting_capital: equity base. Required — drawdown must be measured
            against real equity, not against cumulative PnL starting at ~0
            (dividing by a near-zero running peak produced absurd values like
            138.0 instead of a fraction).

    Returns:
        95th percentile max drawdown fraction from reshuffles
    """
    if len(trade_list) < 3:
        return 0.0

    rng = np.random.RandomState(seed)
    pnls = np.array([t.get("pnl", 0) for t in trade_list], dtype=float)
    base = float(starting_capital) if starting_capital and starting_capital > 0 else 1.0

    def _max_dd(sequence: np.ndarray) -> float:
        equity = base + np.cumsum(sequence)
        peak = np.maximum.accumulate(np.concatenate(([base], equity)))[1:]
        safe_peak = np.where(peak > 0, peak, np.nan)
        dd = (peak - equity) / safe_peak
        dd = np.nan_to_num(dd, nan=1.0, posinf=1.0, neginf=0.0)
        return float(np.clip(np.max(dd), 0.0, 1.0))

    max_dd = _max_dd(pnls)

    # Reshuffle trades
    simulated_maxdds = [max_dd]
    for _ in range(num_simulations):
        shuffled = rng.permutation(pnls)
        simulated_maxdds.append(_max_dd(shuffled))

    return float(np.percentile(simulated_maxdds, confidence * 100))


# Note: the fake CPCV/walk-forward placeholders that used to live here
# (mean(labels) as a stand-in "accuracy") were deleted — GateEvaluator now
# wraps REAL results from validation.cpcv.CPCVValidator and
# backtesting.evaluator.WalkForwardEvaluator instead (see evaluate_cpcv /
# evaluate_walk_forward below). The India cost model that used to live here
# (calculate_india_costs) had the brokerage formula backwards (max instead of
# min, wrong rate) — deleted in favor of delegating to the real
# backtesting.charges.ChargesModel via BacktestResult.total_charges (see
# evaluate_cost_model below). PromotionStage/PromotionRecord/PaperTradingRecord
# were a THIRD, unused promotion-ladder concept (parallel to but not the same
# as rl/policies.py's live one and validation/promotion.py's dead one) —
# deleted; promotion is enforced by api/strategy_promotion_service.py instead.


# ──────────────────────────────────────────────
# Gate evaluator
# ──────────────────────────────────────────────


class GateEvaluator:
    """
    Evaluate all promotion gates for a backtest/strategy.

    Wraps REAL results — the real WalkForwardEvaluator
    (backtesting.evaluator.WalkForwardResult), the real (fixed) CPCVValidator
    (validation.cpcv.CPCVResult), and BacktestResult.total_charges (which
    already comes from the real backtesting.charges.ChargesModel at fill
    time). No metric is recomputed here from a fake proxy.
    """

    def __init__(self, settings: Optional[Any] = None) -> None:
        """settings: trading_platform.config.Settings. When provided, gate
        thresholds default to its fields (DEFLECTED_SHARPE_MIN, PBO_MAX,
        MC_SHUFFLE_RUNS, PROMOTION_PAPER_DAYS, MIN_WALKFORWARD_SHARPE,
        GATE_MAX_DRAWDOWN, MIN_NET_COST_RATIO, CSCV_N_GROUPS) instead of the
        hardcoded literals below; explicit method arguments always win."""
        self.results = BacktestGateResults(backtest_id="", strategy_id="")
        self._settings = settings

    def _cfg(self, name: str, default: float) -> float:
        return getattr(self._settings, name, default) if self._settings is not None else default

    def evaluate_walk_forward(
        self,
        wf_result: Any,  # backtesting.evaluator.WalkForwardResult
        min_sharpe: Optional[float] = None,
    ) -> None:
        """Wraps the REAL WalkForwardEvaluator output — no recomputation."""
        threshold = min_sharpe if min_sharpe is not None else self._cfg("MIN_WALKFORWARD_SHARPE", 0.3)
        mean_sharpe = float(getattr(wf_result, "mean_test_sharpe", 0.0))
        degraded = bool(getattr(wf_result, "degradation_detected", False))
        passed = mean_sharpe >= threshold and not degraded
        self.results.walk_forward = GateOutcome(
            gate_name="walk_forward",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=mean_sharpe,
            threshold=threshold,
            message=f"mean OOS Sharpe={mean_sharpe:.4f} vs threshold {threshold:.4f}"
                    + (" (degradation detected)" if degraded else ""),
            details=wf_result.to_dict() if hasattr(wf_result, "to_dict") else {},
        )

    def evaluate_cpcv(
        self,
        cpcv_result: Any,  # validation.cpcv.CPCVResult
        min_auc: Optional[float] = None,
        min_sharpe: Optional[float] = None,
    ) -> None:
        """Wraps the REAL (fixed) CPCVValidator.run_validation() output."""
        auc_threshold = min_auc if min_auc is not None else 0.52
        sharpe_threshold = min_sharpe if min_sharpe is not None else self._cfg("MIN_WALKFORWARD_SHARPE", 0.3)
        metrics = getattr(cpcv_result, "out_of_sample_metrics", {}) or {}
        mean_auc = float(metrics.get("mean_oos_auc", 0.0))
        mean_sharpe = float(metrics.get("mean_oos_sharpe", 0.0))
        passed = mean_auc >= auc_threshold and mean_sharpe >= sharpe_threshold
        self.results.cpcv = GateOutcome(
            gate_name="cpcv",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=mean_auc,
            threshold=auc_threshold,
            message=f"CPCV mean OOS AUC={mean_auc:.4f} (>= {auc_threshold}), "
                    f"mean OOS Sharpe={mean_sharpe:.4f} (>= {sharpe_threshold})",
            details=metrics,
        )

    def evaluate_dsr(
        self,
        sharpe_hat: float,
        trial_sharpes: Any,
        returns: np.ndarray,
        min_dsr: Optional[float] = None,
    ) -> None:
        """Evaluate Deflated Sharpe Ratio gate (real Bailey/Lopez de Prado formula)."""
        threshold = min_dsr if min_dsr is not None else self._cfg("DEFLECTED_SHARPE_MIN", 0.5)
        dsr_result = deflated_sharpe_ratio(sharpe_hat, trial_sharpes, returns)
        if dsr_result.insufficient_data:
            # Not enough data to deflate — report honestly as SKIP rather than
            # FAIL, so "couldn't evaluate" is never mistaken for "overfit".
            self.results.dsr = GateOutcome(
                gate_name="deflated_sharpe",
                result=GateResult.SKIP,
                metric=0.0,
                threshold=threshold,
                message=f"SKIPPED: insufficient data to deflate "
                        f"(n_obs={dsr_result.n_obs}, need >=30; n_trials={dsr_result.n_trials}, need >=2)",
                details={"n_obs": dsr_result.n_obs, "n_trials": dsr_result.n_trials},
            )
            return
        passed = dsr_result.dsr >= threshold
        self.results.dsr = GateOutcome(
            gate_name="deflated_sharpe",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=dsr_result.dsr,
            threshold=threshold,
            message=f"DSR={dsr_result.dsr:.4f} (threshold >= {threshold}), "
                    f"n_trials={dsr_result.n_trials}, SR_hat={sharpe_hat:.4f}, SR_0={dsr_result.expected_max_sharpe:.4f}",
            details={
                "observed_sharpe": dsr_result.observed_sharpe,
                "expected_max_sharpe": dsr_result.expected_max_sharpe,
                "sharpe_variance": dsr_result.sharpe_variance,
                "n_trials": dsr_result.n_trials,
                "skew": dsr_result.skew,
                "kurtosis": dsr_result.kurtosis,
                "n_obs": dsr_result.n_obs,
            },
        )

    def evaluate_pbo(
        self,
        returns_matrix: np.ndarray,
        max_pbo: Optional[float] = None,
        n_groups: Optional[int] = None,
    ) -> None:
        """Evaluate PBO gate (real CSCV over a T x N returns matrix)."""
        threshold = max_pbo if max_pbo is not None else self._cfg("PBO_MAX", 0.4)
        groups = int(n_groups if n_groups is not None else self._cfg("CSCV_N_GROUPS", 8))
        pbo_result = probability_of_backtest_overfitting(returns_matrix, n_groups=groups)
        passed = pbo_result.pbo <= threshold
        self.results.pbo = GateOutcome(
            gate_name="pbo",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=pbo_result.pbo,
            threshold=threshold,
            message=f"PBO={pbo_result.pbo:.4f} (threshold <= {threshold}), "
                    f"n_splits={pbo_result.n_splits}, n_variants={pbo_result.n_variants}",
            details={"n_splits": pbo_result.n_splits, "n_variants": pbo_result.n_variants},
        )

    def evaluate_monte_carlo(
        self,
        trade_list: List[Dict[str, Any]],
        max_dd_limit: Optional[float] = None,
        starting_capital: float = 1_000_000.0,
    ) -> None:
        """Evaluate Monte Carlo DD gate (drawdown as a fraction of starting capital)."""
        threshold = max_dd_limit if max_dd_limit is not None else self._cfg("GATE_MAX_DRAWDOWN", 0.15)
        mc_dd = monte_carlo_dd_estimate(trade_list, starting_capital=starting_capital)
        passed = mc_dd <= threshold
        self.results.monte_carlo = GateOutcome(
            gate_name="monte_carlo_dd",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=mc_dd,
            threshold=threshold,
            message=f"95% max DD={mc_dd:.4f} (limit <= {threshold})",
            details={"mc_95_dd": mc_dd},
        )

    def evaluate_cost_model(
        self,
        total_pnl: float,
        total_charges: float,
        min_net_ratio: Optional[float] = None,
    ) -> None:
        """Evaluate cost model gate: net must be >= min_net_ratio of gross.

        total_pnl is already net (charges deducted); total_charges comes from
        the REAL backtesting.charges.ChargesModel via Trade.charges — no cost
        recomputation here (the deleted calculate_india_costs() had the
        brokerage formula backwards: max() instead of min(), 0.01% instead of
        the real 0.03%)."""
        threshold = min_net_ratio if min_net_ratio is not None else self._cfg("MIN_NET_COST_RATIO", 0.6)
        gross_pnl = total_pnl + total_charges
        if gross_pnl <= 0:
            self.results.cost_model = GateOutcome(
                gate_name="cost_model",
                result=GateResult.WARN,
                metric=0.0,
                threshold=threshold,
                message="Gross P&L <= 0, cannot evaluate cost drag",
            )
            return
        net_ratio = total_pnl / gross_pnl
        passed = net_ratio >= threshold
        self.results.cost_model = GateOutcome(
            gate_name="cost_model",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=net_ratio,
            threshold=threshold,
            message=f"Net/Gross ratio={net_ratio:.4f} (threshold >= {threshold})",
            details={"gross_pnl": gross_pnl, "total_charges": total_charges, "net_pnl": total_pnl},
        )

    def evaluate_paper_days(
        self,
        paper_days: int,
        min_days: Optional[int] = None,
    ) -> None:
        """Evaluate paper trading days gate."""
        threshold = min_days if min_days is not None else int(self._cfg("PROMOTION_PAPER_DAYS", 30))
        passed = paper_days >= threshold
        self.results.paper_days = GateOutcome(
            gate_name="paper_days",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=float(paper_days),
            threshold=float(threshold),
            message=f"Paper days={paper_days} (threshold >= {threshold})",
        )

    def evaluate_promotion_ladder(self, gates_all_passed: bool) -> None:
        """Pass-through summary gate — real promotion enforcement lives in
        api/strategy_promotion_service.py (StrategyPromotionService), which
        reads BacktestGateResults.all_passed via persistence's
        latest_gate_summary(). This slot just records whether everything
        evaluated so far passed."""
        self.results.promotion_ladder = GateOutcome(
            gate_name="promotion_ladder",
            result=GateResult.PASS if gates_all_passed else GateResult.FAIL,
            metric=1.0 if gates_all_passed else 0.0,
            threshold=1.0,
            message="backtest gates passed" if gates_all_passed else "backtest gates failed",
        )

    def finalize(self, backtest_id: str, strategy_id: str) -> BacktestGateResults:
        """Finalize and return gate results."""
        self.results.backtest_id = backtest_id
        self.results.strategy_id = strategy_id
        return self.results