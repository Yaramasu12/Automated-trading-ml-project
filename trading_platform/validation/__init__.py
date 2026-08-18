from trading_platform.validation.monte_carlo import MonteCarloSimulator
from trading_platform.validation.purged_walk_forward import PurgedWalkForwardValidator
from trading_platform.validation.tournament import StrategyTournament
from trading_platform.validation.promotion import PolicyPromoter, PromotionStatus
from trading_platform.validation.postmortem import PostmortemFactory

# REDESIGN §5 validation gates (real CPCV / Deflated Sharpe / PBO).
from trading_platform.validation.cpcv import (
    CombinatorialPurgedCrossValidator,
    CPCVValidator,
    CPCVResult,
    PathVariantResult,
    ProbabilityOfOverfittingProcessor,
)
from trading_platform.validation.gates import (
    BacktestGateResults,
    DSRResult,
    GateEvaluator,
    GateOutcome,
    GateResult,
    PBOResult,
    deflated_sharpe_ratio,
    monte_carlo_dd_estimate,
    probability_of_backtest_overfitting,
)

__all__ = [
    "MonteCarloSimulator",
    "PurgedWalkForwardValidator",
    "StrategyTournament",
    "PolicyPromoter",
    "PromotionStatus",
    "PostmortemFactory",
    # REDESIGN §5
    "CombinatorialPurgedCrossValidator",
    "CPCVValidator",
    "CPCVResult",
    "PathVariantResult",
    "ProbabilityOfOverfittingProcessor",
    "BacktestGateResults",
    "DSRResult",
    "GateEvaluator",
    "GateOutcome",
    "GateResult",
    "PBOResult",
    "deflated_sharpe_ratio",
    "monte_carlo_dd_estimate",
    "probability_of_backtest_overfitting",
]
