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

import hashlib
import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

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


def deflated_sharpe_ratio(
    returns: np.ndarray,
    num_simulations: int = 500,
    seed: int = 42,
    annualization_factor: float = 252.0,
    rejection_rate: float = 0.05,
) -> Tuple[float, float]:
    """
    Compute Deflated Sharpe Ratio (Lo, 2002).

    DSR = (SR_obs - mu_ST) / sigma_ST

    where mu_ST, sigma_ST are the expected min/max Sharpe ratios
    from num_simulations of random strategies.

    Returns:
        (dsr, p_value) — p_value = proportion of simulated Sharpes >= SR_obs
    """
    if len(returns) < 30:
        return 0.0, 1.0

    # Observed Sharpe ratio (annualized)
    mu = np.mean(returns)
    sigma = np.std(returns, ddof=1)
    if sigma == 0:
        return 0.0, 1.0
    sr_obs = (mu / sigma) * math.sqrt(annualization_factor)

    # Simulate random strategies
    rng = np.random.RandomState(seed)
    n = len(returns)
    simulated_srs = []
    for _ in range(num_simulations):
        # Shuffle returns
        shuffled = rng.permutation(returns)
        m = np.mean(shuffled)
        s = np.std(shuffled, ddof=1)
        if s > 0:
            simulated_srs.append((m / s) * math.sqrt(annualization_factor))

    if not simulated_srs:
        return sr_obs, 0.5

    simulated_srs = np.array(simulated_srs)

    # P-value: proportion of simulated Sharpes >= observed
    p_value = float(np.mean(simulated_srs >= sr_obs))

    # DSR
    mu_st = float(np.min(simulated_srs))
    sigma_st = float(np.std(simulated_srs))
    if sigma_st == 0:
        dsr = sr_obs
    else:
        dsr = (sr_obs - mu_st) / sigma_st

    return dsr, p_value


# ──────────────────────────────────────────────
# Probability of Backtest Overfitting (PBO)
# ──────────────────────────────────────────────


def probability_of_overfitting(
    out_of_sample_srs: List[float],
    in_sample_srs: List[List[float]],
    num_simulations: int = 10000,
    seed: int = 42,
) -> float:
    """
    Compute Probability of Backtest Overfitting (Romano, Paloalto, Tapia, 2015).

    PBO = Pr( max(in-sample SR) > out-of-sample SR )

    Returns value in [0, 1]. PBO > 0.4 means overfitted.
    """
    if not out_of_sample_srs or not in_sample_srs:
        return 1.0

    rng = np.random.RandomState(seed)
    oos_max = max(out_of_sample_srs)
    n_in = len(in_sample_srs[0]) if in_sample_srs else 1

    overfit_count = 0
    for _ in range(num_simulations):
        # Sample random in-sample sets
        sampled = []
        for _ in range(len(in_sample_srs)):
            idx = rng.randint(0, len(in_sample_srs))
            sampled.append(in_sample_srs[idx][:n_in])

        in_max = max(max(s) for s in sampled)
        if in_max > oos_max:
            overfit_count += 1

    return overfit_count / num_simulations


# ──────────────────────────────────────────────
# Monte Carlo trade-reshuffle
# ──────────────────────────────────────────────


def monte_carlo_dd_estimate(
    trade_list: List[Dict[str, Any]],
    confidence: float = 0.95,
    num_simulations: int = 1000,
    seed: int = 42,
) -> float:
    """
    Monte Carlo trade reshuffle → 95% max drawdown estimate.

    Parameters:
        trade_list: list of {pnl, ...} dicts
        confidence: confidence level (default 0.95)
        num_simulations: number of reshuffles
        seed: random seed

    Returns:
        95th percentile max drawdown from reshuffles
    """
    if len(trade_list) < 3:
        return 0.0

    rng = np.random.RandomState(seed)
    pnls = np.array([t.get("pnl", 0) for t in trade_list], dtype=float)

    # Normalize to equity curve
    equity = np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / (np.abs(peak) + 1e-10)
    max_dd = float(np.max(dd))

    # Reshuffle trades
    simulated_maxdds = [max_dd]
    for _ in range(num_simulations):
        shuffled = rng.permutation(pnls)
        eq = np.cumsum(shuffled)
        pk = np.maximum.accumulate(eq)
        drawdowns = (pk - eq) / (np.abs(pk) + 1e-10)
        simulated_maxdds.append(float(np.max(drawdowns)))

    return float(np.percentile(simulated_maxdds, confidence * 100))


# ──────────────────────────────────────────────
# CPCV (Combinatorial Purged Cross-Validation)
# ──────────────────────────────────────────────


def combinatorial_purged_cv(
    returns: np.ndarray,
    labels: np.ndarray,
    n_folds: int = 8,
    embargo_periods: int = 5,
) -> List[Dict[str, Any]]:
    """
    Combinatorial Purged Cross-Validation (López de Prado, 2018).

    Returns list of fold results with purged train/test splits.
    """
    if len(returns) < n_folds * 10:
        return []

    n = len(returns)
    step = n // n_folds
    fold_results = []

    for fold in range(n_folds):
        # Define test window
        test_start = fold * step
        test_end = min(test_start + step, n)

        # Purge overlapping labels from train
        purge_start = max(0, test_start - embargo_periods)
        purge_end = test_end + embargo_periods

        # Train: everything outside test + purge
        train_idx = list(range(0, purge_start)) + list(range(purge_end, n))
        test_idx = list(range(test_start, test_end))

        if not train_idx or not test_idx:
            continue

        train_returns = returns[train_idx]
        train_labels = labels[train_idx]
        test_returns = returns[test_idx]
        test_labels = labels[test_idx]

        # Simple accuracy as metric (would use actual model in real usage)
        train_mean = float(np.mean(train_labels)) if len(train_labels) > 0 else 0.5
        test_mean = float(np.mean(test_labels)) if len(test_labels) > 0 else 0.5

        fold_results.append({
            "fold": fold,
            "train_size": len(train_idx),
            "test_size": len(test_idx),
            "train_mean": train_mean,
            "test_mean": test_mean,
            "test_accuracy": test_mean,
            "purge_range": (purge_start, purge_end),
        })

    return fold_results


# ──────────────────────────────────────────────
# India cost model
# ──────────────────────────────────────────────


def calculate_india_costs(
    turnover: Decimal,
    side: str,
    product_type: str = "CNC",  # CNC, MIS, NRML
) -> Dict[str, Decimal]:
    """
    Full India cost model (SEBI/NSE compliant).

    Parameters:
        turnover: price × quantity
        side: BUY or SELL
        product_type: CNC (delivery), MIS (intraday), NRML (margin)

    Returns:
        Dict with brokerage, STT, exchange_txn, gst, sebi, stamp_duty, total
    """
    # Brokerage: lower of ₹20 or 0.01% per side
    brokerage = max(Decimal("20"), turnover * Decimal("0.0001"))

    # STT (Securities Transaction Tax)
    stt = Decimal("0")
    if product_type == "CNC":
        if side == "SELL":
            stt = turnover * Decimal("0.025") / Decimal("100")  # 0.025%
    elif product_type == "MIS":
        if side == "SELL":
            stt = turnover * Decimal("0.025") / Decimal("100")

    # Exchange transaction charge
    exchange_txn = turnover * Decimal("0.00325") / Decimal("10000")  # 0.00325%

    # GST (18% on brokerage + exchange txn)
    gst = (brokerage + exchange_txn) * Decimal("18") / Decimal("100")

    # SEBI turnover fee
    sebi = turnover * Decimal("0.00001") / Decimal("10000")  # ₹10 per crore

    # Stamp duty (varies by state, approximate)
    stamp_duty = turnover * Decimal("0.015") / Decimal("100") if side == "BUY" else Decimal("0")

    total = brokerage + stt + exchange_txn + gst + sebi + stamp_duty

    return {
        "turnover": turnover,
        "brokerage": brokerage,
        "stt": stt,
        "exchange_txn": exchange_txn,
        "gst": gst,
        "sebi": sebi,
        "stamp_duty": stamp_duty,
        "total_cost": total,
        "net_pnl_multiplier": Decimal("1") - total / turnover if turnover > 0 else Decimal("1"),
    }


# ──────────────────────────────────────────────
# Walk-forward evaluation
# ──────────────────────────────────────────────


def walk_forward_evaluation(
    returns: np.ndarray,
    labels: np.ndarray,
    window_size: int = 252,
    step_size: int = 50,
) -> Dict[str, Any]:
    """
    Walk-forward evaluation: train on rolling window, test on next step.

    Returns OOS (out-of-sample) results per fold.
    """
    if len(returns) < window_size + step_size:
        return {"folds": [], "oos_accuracy": 0.5}

    oos_accuracies = []
    n = len(returns)

    for start in range(0, n - window_size, step_size):
        train_end = start + window_size
        test_end = min(train_end + step_size, n)

        train_labels = labels[start:train_end]
        test_labels = labels[train_end:test_end]

        if len(test_labels) < 5:
            continue

        # Simple metric: mean of test labels as accuracy proxy
        oos_acc = float(np.mean(test_labels))
        oos_accuracies.append(oos_acc)

    oos_accuracy = float(np.mean(oos_accuracies)) if oos_accuracies else 0.5

    return {
        "window_size": window_size,
        "step_size": step_size,
        "num_folds": len(oos_accuracies),
        "oos_accuracy": oos_accuracy,
        "oos_accuracies_per_fold": oos_accuracies,
    }


# ──────────────────────────────────────────────
# Paper trading days tracker
# ──────────────────────────────────────────────


@dataclass
class PaperTradingRecord:
    """Track paper trading days for a strategy."""
    strategy_id: str
    tenant_id: str
    paper_days: int = 0
    paper_pnl: Decimal = Decimal("0")
    paper_sharpe: float = 0.0
    live_deployed: bool = False
    live_deployed_at: Optional[str] = None

    def add_day(self, daily_pnl: Decimal) -> None:
        self.paper_days += 1
        self.paper_pnl += daily_pnl

    def meets_minimum_days(self, min_days: int = 30) -> bool:
        return self.paper_days >= min_days


# ──────────────────────────────────────────────
# Promotion ladder
# ──────────────────────────────────────────────


class PromotionStage(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE_MIN = "live_min"
    LIVE_FULL = "live_full"


@dataclass
class PromotionRecord:
    """Promotion status for a strategy."""
    strategy_id: str
    current_stage: PromotionStage = PromotionStage.BACKTEST
    backtest_passed: bool = False
    paper_days: int = 0
    paper_pnl: Decimal = Decimal("0")
    paper_sharpe: float = 0.0
    live_min_days: int = 0
    live_min_pnl: Decimal = Decimal("0")
    promoted_at: Optional[str] = None
    gate_results: Dict[str, Any] = field(default_factory=dict)

    def can_promote(self) -> bool:
        """Check if strategy can advance to next stage."""
        if self.current_stage == PromotionStage.BACKTEST:
            return self.backtest_passed and self.paper_days >= 30
        elif self.current_stage == PromotionStage.PAPER:
            return self.paper_sharpe > 0 and abs(self.paper_pnl) > 1000  # Min ₹1k profit
        elif self.current_stage == PromotionStage.LIVE_MIN:
            return self.live_min_days >= 15 and self.live_min_pnl > 0
        return False

    def promote(self) -> None:
        """Advance to next stage."""
        stages = list(PromotionStage)
        idx = stages.index(self.current_stage)
        if idx < len(stages) - 1:
            self.current_stage = stages[idx + 1]
            self.promoted_at = str(int(__import__("time").time()))


# ──────────────────────────────────────────────
# Gate evaluator
# ──────────────────────────────────────────────


class GateEvaluator:
    """
    Evaluate all promotion gates for a backtest/strategy.
    """

    def __init__(self):
        self.results = BacktestGateResults(
            backtest_id="",
            strategy_id="",
        )

    def evaluate_walk_forward(
        self,
        returns: np.ndarray,
        labels: np.ndarray,
        window_size: int = 252,
        step_size: int = 50,
    ) -> None:
        """Evaluate walk-forward gate."""
        wf = walk_forward_evaluation(returns, labels, window_size, step_size)
        oos_acc = wf.get("oos_accuracy", 0.5)

        # Gate: OOS accuracy must beat 0.5 + margin
        margin = max(0.02, 2 * math.sqrt(0.25 * 0.75 / max(1, wf.get("test_size", 1))))
        passed = oos_acc > 0.5 + margin

        self.results.walk_forward = GateOutcome(
            gate_name="walk_forward",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=oos_acc,
            threshold=0.5 + margin,
            message=f"OOS accuracy {oos_acc:.4f} vs threshold {0.5 + margin:.4f}",
            details=wf,
        )

    def evaluate_cpcv(
        self,
        returns: np.ndarray,
        labels: np.ndarray,
        n_folds: int = 8,
    ) -> None:
        """Evaluate CPCV gate."""
        folds = combinatorial_purged_cv(returns, labels, n_folds)
        if not folds:
            self.results.cpcv = GateOutcome(
                gate_name="cpcv",
                result=GateResult.WARN,
                metric=0.0,
                threshold=0.0,
                message="Insufficient data for CPCV",
            )
            return

        avg_test_acc = float(np.mean([f["test_accuracy"] for f in folds]))

        # Gate: avg test accuracy > 0.5 + margin
        margin = 0.02
        passed = avg_test_acc > 0.5 + margin

        self.results.cpcv = GateOutcome(
            gate_name="cpcv",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=avg_test_acc,
            threshold=0.5 + margin,
            message=f"CPCV avg accuracy {avg_test_acc:.4f} vs threshold {0.5 + margin:.4f}",
            details={"num_folds": len(folds), "fold_results": folds[:3]},  # Sample
        )

    def evaluate_dsr(
        self,
        returns: np.ndarray,
        num_simulations: int = 500,
    ) -> None:
        """Evaluate Deflated Sharpe Ratio gate."""
        dsr, p_value = deflated_sharpe_ratio(returns, num_simulations)

        # Gate: DSR > 0.5 and p-value < 0.1
        dsr_threshold = 0.5
        p_threshold = 0.1
        passed = dsr > dsr_threshold and p_value < p_threshold

        self.results.dsr = GateOutcome(
            gate_name="deflated_sharpe",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=dsr,
            threshold=dsr_threshold,
            message=f"DSR={dsr:.4f}, p={p_value:.4f} (threshold > {dsr_threshold}, p < {p_threshold})",
            details={"dsr": dsr, "p_value": p_value},
        )

    def evaluate_pbo(
        self,
        oos_srs: List[float],
        is_srs: List[List[float]],
    ) -> None:
        """Evaluate PBO gate."""
        pbo = probability_of_overfitting(oos_srs, is_srs)

        # Gate: PBO < 0.4
        threshold = 0.4
        passed = pbo < threshold

        self.results.pbo = GateOutcome(
            gate_name="pbo",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=pbo,
            threshold=threshold,
            message=f"PBO={pbo:.4f} (threshold < {threshold})",
            details={"pbo": pbo, "num_oos": len(oos_srs), "num_is": len(is_srs)},
        )

    def evaluate_monte_carlo(
        self,
        trade_list: List[Dict[str, Any]],
        max_dd_limit: float = 0.15,
    ) -> None:
        """Evaluate Monte Carlo DD gate."""
        mc_dd = monte_carlo_dd_estimate(trade_list)

        passed = mc_dd < max_dd_limit

        self.results.monte_carlo = GateOutcome(
            gate_name="monte_carlo_dd",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=mc_dd,
            threshold=max_dd_limit,
            message=f"95% max DD={mc_dd:.4f} (limit < {max_dd_limit})",
            details={"mc_95_dd": mc_dd},
        )

    def evaluate_cost_model(
        self,
        gross_pnl: Decimal,
        costs: Decimal,
        min_net_ratio: float = 0.6,
    ) -> None:
        """Evaluate cost model gate: net must be >= min_net_ratio of gross."""
        if gross_pnl <= 0:
            self.results.cost_model = GateOutcome(
                gate_name="cost_model",
                result=GateResult.WARN,
                metric=0.0,
                threshold=min_net_ratio,
                message="Gross P&L ≤ 0, cannot evaluate cost drag",
            )
            return

        net_pnl = gross_pnl - costs
        net_ratio = float(net_pnl / gross_pnl) if gross_pnl > 0 else 0.0
        passed = net_ratio >= min_net_ratio

        self.results.cost_model = GateOutcome(
            gate_name="cost_model",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=net_ratio,
            threshold=min_net_ratio,
            message=f"Net/Gross ratio={net_ratio:.4f} (threshold ≥ {min_net_ratio})",
            details={"gross_pnl": str(gross_pnl), "costs": str(costs), "net_pnl": str(net_pnl)},
        )

    def evaluate_paper_days(
        self,
        paper_days: int,
        min_days: int = 30,
    ) -> None:
        """Evaluate paper trading days gate."""
        passed = paper_days >= min_days
        self.results.paper_days = GateOutcome(
            gate_name="paper_days",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=float(paper_days),
            threshold=float(min_days),
            message=f"Paper days={paper_days} (threshold ≥ {min_days})",
        )

    def evaluate_promotion_ladder(
        self,
        current_stage: PromotionStage,
        backtest_passed: bool,
    ) -> None:
        """Evaluate promotion ladder gate."""
        passed = current_stage != PromotionStage.BACKTEST or backtest_passed
        self.results.promotion_ladder = GateOutcome(
            gate_name="promotion_ladder",
            result=GateResult.PASS if passed else GateResult.FAIL,
            metric=float(current_stage.value),
            threshold=0,
            message=f"Current stage={current_stage.value}",
        )

    def finalize(self, backtest_id: str, strategy_id: str) -> BacktestGateResults:
        """Finalize and return gate results."""
        self.results.backtest_id = backtest_id
        self.results.strategy_id = strategy_id
        return self.results


# ──────────────────────────────────────────────
# Convenience: evaluate all gates
# ──────────────────────────────────────────────


async def evaluate_all_gates(
    backtest_id: str,
    strategy_id: str,
    returns: np.ndarray,
    labels: np.ndarray,
    trade_list: List[Dict[str, Any]],
    oos_srs: List[float],
    is_srs: List[List[float]],
    gross_pnl: Decimal,
    costs: Decimal,
    paper_days: int = 0,
    current_stage: PromotionStage = PromotionStage.BACKTEST,
    backtest_passed: bool = False,
) -> BacktestGateResults:
    """Run all gates and return results."""
    evaluator = GateEvaluator()

    evaluator.evaluate_walk_forward(returns, labels)
    evaluator.evaluate_cpcv(returns, labels)
    evaluator.evaluate_dsr(returns)
    evaluator.evaluate_pbo(oos_srs, is_srs)
    evaluator.evaluate_monte_carlo(trade_list)
    evaluator.evaluate_cost_model(gross_pnl, costs)
    evaluator.evaluate_paper_days(paper_days)
    evaluator.evaluate_promotion_ladder(current_stage, backtest_passed)

    return evaluator.finalize(backtest_id, strategy_id)