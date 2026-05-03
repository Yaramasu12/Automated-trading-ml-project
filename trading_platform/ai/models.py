from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean, pstdev

from trading_platform.ai.agents import ModelPerformance, ModelSelectionAgent
from trading_platform.ai.features import FeatureSnapshot


@dataclass(frozen=True)
class VolatilityForecast:
    model_name: str
    annualized_volatility: float
    daily_volatility: float
    lower_95: float
    upper_95: float
    sample_size: int


@dataclass(frozen=True)
class ForecastEvaluation:
    coverage: float
    misses: int
    sample_size: int
    healthy: bool


@dataclass(frozen=True)
class SentimentResult:
    model_name: str
    score: float
    label: str
    confidence: float
    matched_positive: int
    matched_negative: int


@dataclass(frozen=True)
class ModelSelectionResult:
    selected_model: str
    reason: str
    candidates: list[ModelPerformance]


@dataclass(frozen=True)
class StrategyRegimeScore:
    strategy_name: str
    regime: str
    score: float
    rank: int


# ---------------------------------------------------------------------------
# Volatility forecasters
# ---------------------------------------------------------------------------


class VolatilityForecaster:
    """Local baseline volatility service used before heavier GARCH/LSTM models are trusted."""

    def forecast(self, closes: list[float], model_name: str = "ewma_volatility") -> VolatilityForecast:
        returns = self._returns(closes)
        if len(returns) < 5:
            raise ValueError("At least 6 closes are required for volatility forecast")
        if model_name == "garch_baseline":
            daily_vol = self._garch_like(returns)
        else:
            daily_vol = self._ewma(returns)
            model_name = "ewma_volatility"
        annualized = daily_vol * math.sqrt(252)
        interval_width = 1.96 * daily_vol
        expected_next_return = mean(returns[-5:])
        return VolatilityForecast(
            model_name=model_name,
            annualized_volatility=annualized,
            daily_volatility=daily_vol,
            lower_95=expected_next_return - interval_width,
            upper_95=expected_next_return + interval_width,
            sample_size=len(returns),
        )

    def evaluate_interval(self, closes: list[float], forecast: VolatilityForecast) -> ForecastEvaluation:
        returns = self._returns(closes)
        if not returns:
            return ForecastEvaluation(coverage=0.0, misses=0, sample_size=0, healthy=False)
        misses = sum(1 for value in returns if value < forecast.lower_95 or value > forecast.upper_95)
        coverage = 1 - (misses / len(returns))
        return ForecastEvaluation(coverage=coverage, misses=misses, sample_size=len(returns), healthy=coverage >= 0.90)

    def _returns(self, closes: list[float]) -> list[float]:
        return [
            (closes[index] - closes[index - 1]) / closes[index - 1]
            for index in range(1, len(closes))
            if closes[index - 1] > 0
        ]

    def _ewma(self, returns: list[float], lambda_: float = 0.94) -> float:
        variance = returns[0] ** 2
        for value in returns[1:]:
            variance = lambda_ * variance + (1 - lambda_) * value * value
        return math.sqrt(max(variance, 0.000001))

    def _garch_like(self, returns: list[float]) -> float:
        long_run = pstdev(returns) if len(returns) > 1 else abs(returns[-1])
        ewma = self._ewma(returns, lambda_=0.90)
        shock = abs(returns[-1])
        return max(0.0001, 0.10 * long_run + 0.80 * ewma + 0.10 * shock)


class GARCHForecaster:
    """Proper GARCH(1,1) with grid-search MLE parameter estimation (pure Python)."""

    _ALPHA_GRID = (0.05, 0.08, 0.10, 0.12, 0.15, 0.20)
    _BETA_GRID = (0.70, 0.75, 0.80, 0.85, 0.88, 0.90)

    def forecast(self, closes: list[float]) -> VolatilityForecast:
        returns = self._returns(closes)
        if len(returns) < 10:
            raise ValueError("At least 11 closes required for GARCH(1,1) forecast")
        daily_vol, (omega, alpha, beta) = self._fit(returns)
        annualized = daily_vol * math.sqrt(252)
        interval_width = 1.96 * daily_vol
        expected_return = mean(returns[-5:]) if len(returns) >= 5 else 0.0
        return VolatilityForecast(
            model_name="garch_1_1",
            annualized_volatility=annualized,
            daily_volatility=daily_vol,
            lower_95=expected_return - interval_width,
            upper_95=expected_return + interval_width,
            sample_size=len(returns),
        )

    def fitted_params(self, closes: list[float]) -> dict:
        """Return fitted omega, alpha, beta, and persistence."""
        returns = self._returns(closes)
        if len(returns) < 10:
            raise ValueError("At least 11 closes required")
        _, (omega, alpha, beta) = self._fit(returns)
        return {"omega": omega, "alpha": alpha, "beta": beta, "persistence": alpha + beta}

    def _fit(self, returns: list[float]) -> tuple[float, tuple[float, float, float]]:
        sigma2_bar = sum(r * r for r in returns) / len(returns)
        best_ll = float("-inf")
        best_params = (0.10, 0.85)
        for alpha in self._ALPHA_GRID:
            for beta in self._BETA_GRID:
                if alpha + beta >= 0.9999:
                    continue
                omega = sigma2_bar * (1.0 - alpha - beta)
                if omega <= 0:
                    continue
                h = sigma2_bar
                ll = 0.0
                for r in returns:
                    h = omega + alpha * r * r + beta * h
                    h = max(h, 1e-12)
                    ll -= 0.5 * (math.log(h) + r * r / h)
                if ll > best_ll:
                    best_ll = ll
                    best_params = (alpha, beta)
        alpha, beta = best_params
        omega = sigma2_bar * (1.0 - alpha - beta)
        # Filter through full returns series to get current conditional variance
        h = sigma2_bar
        for r in returns:
            h = omega + alpha * r * r + beta * h
            h = max(h, 1e-12)
        return math.sqrt(h), (omega, alpha, beta)

    def _returns(self, closes: list[float]) -> list[float]:
        return [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ]


# ---------------------------------------------------------------------------
# Sentiment
# ---------------------------------------------------------------------------


class SentimentAnalyzer:
    """Lexicon baseline that keeps the FinBERT interface shape without network/model downloads."""

    positive_words = {
        "beat",
        "bullish",
        "growth",
        "profit",
        "record",
        "upgrade",
        "strong",
        "surge",
        "gain",
        "optimistic",
    }
    negative_words = {
        "bearish",
        "downgrade",
        "fall",
        "fraud",
        "loss",
        "miss",
        "probe",
        "weak",
        "decline",
        "volatile",
    }

    def analyze(self, text: str) -> SentimentResult:
        words = [word.strip(".,:;!?()[]{}\"'").lower() for word in text.split()]
        positive = sum(1 for word in words if word in self.positive_words)
        negative = sum(1 for word in words if word in self.negative_words)
        total = max(1, positive + negative)
        score = (positive - negative) / total
        if score > 0.15:
            label = "POSITIVE"
        elif score < -0.15:
            label = "NEGATIVE"
        else:
            label = "NEUTRAL"
        confidence = min(0.95, 0.50 + abs(score) * 0.45 + min(total, 5) * 0.03)
        return SentimentResult(
            model_name="lexicon_finance_sentiment_baseline",
            score=score,
            label=label,
            confidence=confidence,
            matched_positive=positive,
            matched_negative=negative,
        )


# ---------------------------------------------------------------------------
# Regime classifier
# ---------------------------------------------------------------------------


class RegimeClassifier:
    """ML-based market regime classifier.

    Uses sklearn GradientBoostingClassifier trained on FeatureSnapshots labeled by the
    rule-based MarketRegimeAgent. Falls back to rule-based logic when sklearn is absent
    or when fewer than 10 labeled samples are available.
    """

    REGIMES = ("HIGH_VOLATILITY", "TRENDING", "BREAKOUT", "MEAN_REVERTING")

    def __init__(self) -> None:
        self._model = None
        self._trained = False

    @staticmethod
    def _vec(f: FeatureSnapshot) -> list[float]:
        return [f.momentum_5, f.momentum_20, f.realized_volatility, f.volume_ratio, f.trend_strength]

    def train(self, feature_records: list[dict]) -> bool:
        """Fit classifier on records from FeatureStore. Returns True if training succeeded."""
        valid = [r for r in feature_records if r.get("regime") in self.REGIMES]
        if len(valid) < 10 or len({r["regime"] for r in valid}) < 2:
            return False
        try:
            from sklearn.ensemble import GradientBoostingClassifier
        except ImportError:
            return False
        X = [
            [r["momentum_5"], r["momentum_20"], r["realized_volatility"], r["volume_ratio"], r["trend_strength"]]
            for r in valid
        ]
        y = [r["regime"] for r in valid]
        self._model = GradientBoostingClassifier(n_estimators=50, max_depth=3, random_state=42)
        self._model.fit(X, y)
        self._trained = True
        return True

    def predict(self, features: FeatureSnapshot) -> str:
        if self._trained and self._model is not None:
            return str(self._model.predict([self._vec(features)])[0])
        return self._rule_based(features)

    def predict_proba(self, features: FeatureSnapshot) -> dict[str, float]:
        """Return probability dict per regime. Falls back to hard 1.0 if model not available."""
        if self._trained and self._model is not None:
            probs = self._model.predict_proba([self._vec(features)])[0]
            classes = list(self._model.classes_)
            return {cls: float(prob) for cls, prob in zip(classes, probs)}
        regime = self._rule_based(features)
        return {r: (1.0 if r == regime else 0.0) for r in self.REGIMES}

    @property
    def is_trained(self) -> bool:
        return self._trained

    @staticmethod
    def _rule_based(f: FeatureSnapshot) -> str:
        if f.realized_volatility > 0.025:
            return "HIGH_VOLATILITY"
        if f.trend_strength > 4 and abs(f.momentum_20) > 0.03:
            return "TRENDING"
        if abs(f.momentum_5) > 0.02 and f.volume_ratio > 1.2:
            return "BREAKOUT"
        return "MEAN_REVERTING"


# ---------------------------------------------------------------------------
# Meta-model: strategy ranking by regime
# ---------------------------------------------------------------------------


class MetaModel:
    """Ranks strategies using regime-conditioned exponential moving averages of back-test scores.

    Call `update()` after each evaluation window, then `rank()` to get an ordered list
    of strategy names for the current regime.
    """

    _EMA_ALPHA = 0.30  # higher = faster adaptation to recent windows

    def __init__(self) -> None:
        self._scores: dict[str, dict[str, float]] = {}  # regime -> strategy -> ema_score

    def update(self, regime: str, strategy_name: str, score: float) -> None:
        """Update the EMA score for (regime, strategy_name)."""
        regime_map = self._scores.setdefault(regime, {})
        prev = regime_map.get(strategy_name, score)
        regime_map[strategy_name] = self._EMA_ALPHA * score + (1.0 - self._EMA_ALPHA) * prev

    def rank(self, regime: str, strategy_names: list[str]) -> list[StrategyRegimeScore]:
        """Return all strategies sorted by regime-specific score (best first)."""
        regime_map = self._scores.get(regime, {})
        scored = sorted(
            ((name, regime_map.get(name, 0.0)) for name in strategy_names),
            key=lambda x: x[1],
            reverse=True,
        )
        return [
            StrategyRegimeScore(strategy_name=name, regime=regime, score=score, rank=idx + 1)
            for idx, (name, score) in enumerate(scored)
        ]

    def top_strategies(self, regime: str, strategy_names: list[str], top_n: int = 3) -> list[str]:
        return [item.strategy_name for item in self.rank(regime, strategy_names)[:top_n]]

    def summary(self) -> dict:
        return {
            regime: dict(sorted(scores.items(), key=lambda x: x[1], reverse=True))
            for regime, scores in self._scores.items()
        }


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------


class ModelRegistry:
    def __init__(self):
        self.selection_agent = ModelSelectionAgent()

    def candidates(self) -> list[dict]:
        return [
            {
                "name": "ewma_volatility",
                "family": "volatility",
                "status": "active_baseline",
                "promotion_rule": "Used until GARCH/sequence models beat coverage and risk metrics.",
            },
            {
                "name": "garch_1_1",
                "family": "volatility",
                "status": "candidate",
                "promotion_rule": "Promote when walk-forward IV interval coverage is stable above 90%.",
            },
            {
                "name": "garch_baseline",
                "family": "volatility",
                "status": "candidate",
                "promotion_rule": "Promote when walk-forward IV interval coverage is stable above 90%.",
            },
            {
                "name": "xgboost_regime",
                "family": "regime",
                "status": "candidate",
                "promotion_rule": "Promote only when regime classification beats rule baseline on held-out data.",
            },
            {
                "name": "lstm_sequence",
                "family": "sequence",
                "status": "planned",
                "promotion_rule": "Promote only when it beats tabular and volatility baselines.",
            },
            {
                "name": "finbert_sentiment",
                "family": "sentiment",
                "status": "planned",
                "promotion_rule": "Promote only when precision stays above 90% on labeled news.",
            },
            {
                "name": "rl_options_selector",
                "family": "reinforcement_learning",
                "status": "blocked_until_simulator_validation",
                "promotion_rule": "Never route live until simulator, paper, and shadow-live validation pass.",
            },
        ]

    def select(self, performances: list[ModelPerformance]) -> ModelSelectionResult:
        selected = self.selection_agent.choose(performances)
        if selected == "baseline_rules":
            reason = "no_candidate_beat_baseline_quality_gates"
        else:
            reason = "candidate_met_sample_profit_drawdown_quality_gates"
        return ModelSelectionResult(selected_model=selected, reason=reason, candidates=performances)
