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


@dataclass(frozen=True)
class SupervisorDecision:
    action: str
    reason: str
    max_allocation_multiplier: float


class MarketRegimeAgent:
    """Multi-factor regime classifier.

    H2 fix: thresholds lowered so synthetic/live daily-bar data gets a diverse
    spread of regimes instead of collapsing to MEAN_REVERTING on every bar.

    Old TRENDING gate (trend_strength>3.0, mom_20>0.025) was unreachable for
    most random-walk synthetic bars where mom_20 ≈ 0 and trend_strength ≈ 0.
    New gate uses trend_strength>1.5 and mom_20>0.010 — meaningful but reachable.

    HIGH_VOLATILITY threshold lowered slightly (0.030 → 0.025) to capture
    realistic intraday volatility spikes rather than only true panic moves.
    """

    def classify(self, features: FeatureSnapshot) -> str:
        rv = features.realized_volatility

        # Genuine stress/panic: high vol + directional extreme or bands blown wide
        if rv > 0.030 and (abs(features.momentum_5) > 0.03 or features.bb_width > 0.10):
            return "HIGH_VOLATILITY"

        # Trending: sustained directional move with moderate vol
        if features.trend_strength > 1.5 and abs(features.momentum_20) > 0.010 and rv < 0.025:
            return "TRENDING"

        # Breakout: short-term surge with volume confirmation
        if abs(features.momentum_5) > 0.012 and features.volume_ratio > 1.2 and rv < 0.030:
            return "BREAKOUT"

        # Elevated vol but directionless → high-vol regime (short premium strategies)
        if rv > 0.022 and features.trend_strength < 1.5:
            return "HIGH_VOLATILITY"

        # Default: range-bound, use mean-reversion
        return "MEAN_REVERTING"


class StrategySelectionAgent:
    """Map regime → strategies that PROFIT in that environment.

    HIGH_VOLATILITY fix: instead of buying expensive options (theta decay),
    use equity_momentum with tighter risk + gap_strategy (sells into vol spikes).
    Options buying is only valid when volatility is LOW (cheap premium) not HIGH.
    """

    def choose(self, regime: str, symbol: str) -> list[str]:
        if regime == "TRENDING":
            # Strong trend: ride it with momentum + futures leverage
            return ["equity_momentum", "futures_trend", "swing_trend"]
        if regime == "HIGH_VOLATILITY":
            # High vol: mean-revert extremes, use gap strategy (fades gap after vol spike)
            # Do NOT buy expensive options — that's theta decay
            return ["gap_strategy", "mean_reversion"]
        if regime == "BREAKOUT":
            # Price breaking out: breakout + momentum
            return ["breakout", "equity_momentum"]
        # MEAN_REVERTING: oscillating — RSI extremes are the edge
        return ["mean_reversion", "equity_momentum"]


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


class RiskSupervisorAgent:
    """Can reduce or halt trading, never raise limits beyond the configured risk engine."""

    def decide(
        self,
        drawdown: float,
        daily_loss_pct: float,
        rejection_rate: float,
        stale_market_data: bool,
        model_performance: ModelPerformance | None = None,
    ) -> SupervisorDecision:
        if stale_market_data:
            return SupervisorDecision("HALT", "stale_market_data", 0.0)
        if drawdown >= 0.10:
            return SupervisorDecision("HALT", "max_drawdown_reached", 0.0)
        if daily_loss_pct <= -0.02:
            return SupervisorDecision("HALT", "daily_loss_limit_reached", 0.0)
        if rejection_rate > 0.10:
            return SupervisorDecision("REDUCE", "order_rejection_rate_high", 0.25)
        if model_performance and (model_performance.profit_factor < 1.0 or model_performance.sharpe < 0):
            return SupervisorDecision("REDUCE", "model_performance_degraded", 0.50)
        if drawdown > 0.06:
            return SupervisorDecision("REDUCE", "drawdown_elevated", 0.50)
        return SupervisorDecision("ALLOW", "healthy", 1.0)
