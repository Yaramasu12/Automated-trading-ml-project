from __future__ import annotations

from dataclasses import dataclass

from trading_platform.ai.features import FeatureSnapshot


@dataclass(frozen=True)
class ModelPerformance:
    model_name: str
    profit_factor: float
    sharpe: float
    drawdown: float
    iv_interval_coverage: float
    sentiment_precision: float
    sample_size: int
    feature_drift_score: float = 0.0


class MarketRegimeAgent:
    def classify(self, features: FeatureSnapshot) -> str:
        if features.realized_volatility > 0.025:
            return "HIGH_VOLATILITY"
        if features.trend_strength > 4 and abs(features.momentum_20) > 0.03:
            return "TRENDING"
        if abs(features.momentum_5) > 0.02 and features.volume_ratio > 1.2:
            return "BREAKOUT"
        return "MEAN_REVERTING"


class StrategySelectionAgent:
    def choose(self, regime: str, symbol: str) -> list[str]:
        if regime == "TRENDING":
            return ["futures_trend", "equity_momentum"]
        if regime == "HIGH_VOLATILITY":
            return ["defined_risk_option_spread", "volatility_breakout_options"]
        if regime == "BREAKOUT":
            return ["volatility_breakout_options", "futures_trend"]
        return ["equity_momentum", "defined_risk_option_spread"]


class ModelSelectionAgent:
    def choose(self, performances: list[ModelPerformance]) -> str:
        if not performances:
            return "baseline_rules"
        eligible = [
            performance
            for performance in performances
            if performance.sample_size >= 20
            and performance.drawdown <= 0.10
            and performance.profit_factor >= 1.1
        ]
        if not eligible:
            return "baseline_rules"
        return max(eligible, key=lambda item: item.sharpe + item.profit_factor).model_name


class CapitalAllocationAgent:
    def allocation_pct(self, performance: ModelPerformance, target_gap_pct: float) -> float:
        base = 0.02
        if performance.drawdown > 0.06:
            return base
        quality_bonus = min(0.03, max(0.0, (performance.profit_factor - 1.0) * 0.02))
        target_bonus = 0.0
        if target_gap_pct > 0.10 and performance.sharpe > 1.0 and performance.profit_factor > 1.3:
            target_bonus = 0.01
        return min(0.06, base + quality_bonus + target_bonus)


class RetrainingAgent:
    """Retrain on evidence of drift/degradation, not just because the annual target is behind."""

    def should_retrain(self, performance: ModelPerformance, target_gap_pct: float = 0.0) -> tuple[bool, str]:
        if performance.sample_size < 20:
            return False, "insufficient_sample"
        if performance.feature_drift_score > 0.25:
            return True, "feature_drift"
        if performance.iv_interval_coverage < 0.90:
            return True, "iv_coverage_below_threshold"
        if performance.sentiment_precision < 0.85:
            return True, "sentiment_precision_below_threshold"
        if performance.profit_factor < 1.0 and performance.sharpe < 0:
            return True, "risk_adjusted_performance_degraded"
        return False, "healthy"

