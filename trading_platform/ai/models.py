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
    in_sample: bool = True


@dataclass(frozen=True)
class WalkForwardForecastEvaluation:
    """Honest out-of-sample volatility evaluation.

    The forecast is fit on `train_closes` only; coverage is then measured on
    `test_closes` (the held-out window). Reported alongside the in-sample
    coverage for comparison so callers can see the fitted-on-itself number is
    NOT the same as a true forecast hit-rate.
    """

    train_size: int
    test_size: int
    in_sample_coverage: float
    out_of_sample_coverage: float
    out_of_sample_misses: int
    healthy: bool
    model_name: str


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

    def evaluate_interval(
        self,
        closes: list[float],
        forecast: VolatilityForecast,
        *,
        in_sample: bool = True,
    ) -> ForecastEvaluation:
        """Evaluate interval coverage.

        `in_sample=True` (default, kept for backward compatibility) reports
        coverage on the same closes the forecast was derived from — this is
        an inflated number and should be treated as a sanity check, not a
        forecast hit-rate. For an honest forecast hit-rate use
        `walk_forward_evaluate` or pass `in_sample=False` and a held-out
        window of closes.
        """
        returns = self._returns(closes)
        if not returns:
            return ForecastEvaluation(coverage=0.0, misses=0, sample_size=0, healthy=False, in_sample=in_sample)
        misses = sum(1 for value in returns if value < forecast.lower_95 or value > forecast.upper_95)
        coverage = 1 - (misses / len(returns))
        return ForecastEvaluation(
            coverage=coverage,
            misses=misses,
            sample_size=len(returns),
            healthy=coverage >= 0.90,
            in_sample=in_sample,
        )

    def walk_forward_evaluate(
        self,
        closes: list[float],
        *,
        model_name: str = "ewma_volatility",
        train_fraction: float = 0.7,
    ) -> WalkForwardForecastEvaluation:
        """Fit on the leading `train_fraction` of closes, evaluate on the rest.

        Produces a genuine out-of-sample coverage number for the 95% interval.
        The historical `evaluate_interval` is preserved (and now flagged as
        in-sample) because tests and callers depend on the same shape, but new
        callers should prefer this method for forecast quality reports.
        """
        if len(closes) < 12:
            raise ValueError("At least 12 closes required for walk-forward evaluation")
        train_fraction = min(0.9, max(0.3, train_fraction))
        split = max(6, int(len(closes) * train_fraction))
        if split >= len(closes) - 1:
            split = len(closes) - 2
        train_closes = closes[: split + 1]
        test_closes = closes[split:]
        train_forecast = self.forecast(train_closes, model_name=model_name)
        in_sample = self.evaluate_interval(train_closes, train_forecast, in_sample=True)
        oos = self.evaluate_interval(test_closes, train_forecast, in_sample=False)
        return WalkForwardForecastEvaluation(
            train_size=len(train_closes),
            test_size=len(test_closes),
            in_sample_coverage=in_sample.coverage,
            out_of_sample_coverage=oos.coverage,
            out_of_sample_misses=oos.misses,
            healthy=oos.coverage >= 0.85,
            model_name=train_forecast.model_name,
        )

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

    def walk_forward_evaluate(
        self,
        closes: list[float],
        *,
        train_fraction: float = 0.7,
    ) -> WalkForwardForecastEvaluation:
        """Fit GARCH(1,1) on the leading window and evaluate 95% interval coverage on the held-out window."""
        if len(closes) < 20:
            raise ValueError("At least 20 closes required for GARCH walk-forward evaluation")
        train_fraction = min(0.9, max(0.3, train_fraction))
        split = max(11, int(len(closes) * train_fraction))
        if split >= len(closes) - 1:
            split = len(closes) - 2
        train_closes = closes[: split + 1]
        test_closes = closes[split:]
        train_forecast = self.forecast(train_closes)
        # Reuse VolatilityForecaster's interval check (same lower_95/upper_95 shape).
        helper = VolatilityForecaster()
        in_sample = helper.evaluate_interval(train_closes, train_forecast, in_sample=True)
        oos = helper.evaluate_interval(test_closes, train_forecast, in_sample=False)
        return WalkForwardForecastEvaluation(
            train_size=len(train_closes),
            test_size=len(test_closes),
            in_sample_coverage=in_sample.coverage,
            out_of_sample_coverage=oos.coverage,
            out_of_sample_misses=oos.misses,
            healthy=oos.coverage >= 0.85,
            model_name=train_forecast.model_name,
        )

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
        # earnings / fundamentals
        "beat", "outperform", "record", "profit", "revenue", "dividend", "surplus",
        "margin", "earnings", "growth", "expansion", "recovery", "rebound",
        # sentiment
        "bullish", "optimistic", "confident", "positive", "strong", "robust",
        # price action
        "surge", "rally", "jump", "soar", "climb", "gain", "rise", "breakout",
        # ratings / news
        "upgrade", "buy", "overweight", "accumulate", "recommend", "target",
        # macro
        "gdp", "stimulus", "reform", "investment", "inflow", "opportunity",
    }
    negative_words = {
        # earnings / fundamentals
        "miss", "loss", "deficit", "writedown", "impairment", "default", "debt",
        "liabilities", "downgrade", "warning", "revision", "shortfall",
        # sentiment
        "bearish", "pessimistic", "concern", "fear", "risk", "uncertain", "weak",
        # price action
        "fall", "drop", "decline", "plunge", "crash", "sell", "slump", "slide",
        # regulatory / news
        "fraud", "probe", "investigation", "penalty", "ban", "fine", "lawsuit",
        # macro
        "recession", "inflation", "hike", "outflow", "sanctions", "volatile",
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
    """Market regime classifier with an *honest* train/test split.

    Behaviour:

    * `predict` / `predict_proba` always default to the deterministic
      rule-based classifier (`_rule_based`) until `train()` has been called
      with EXTERNALLY-SOURCED labels (e.g. forward-return-derived labels).
    * `train(records, source=...)` REFUSES to fit when the label source is
      `"rule_based"` or unspecified, because fitting sklearn to its own
      teacher's labels only memorises the rule and provides no validation
      signal. Callers must supply `source="external"` (or another non-rule
      tag) to acknowledge that labels were not generated by `_rule_based`.
    * Even with external labels we hold out a stratified test split and
      report `holdout_accuracy`. A model that does not beat the rule baseline
      on the holdout is rejected (`accepted=False`) and `predict` keeps
      using the rule. This stops the codebase from advertising "ML regime
      classifier" performance numbers that were never validated.
    """

    REGIMES = ("HIGH_VOLATILITY", "TRENDING", "BREAKOUT", "MEAN_REVERTING")
    RULE_LABEL_SOURCE = "rule_based"
    _MIN_HOLDOUT_GAIN = 0.0

    def __init__(self) -> None:
        self._model = None
        self._trained = False
        self._last_train_metrics: dict | None = None
        self._label_source: str | None = None

    @staticmethod
    def _vec(f: FeatureSnapshot) -> list[float]:
        return [f.momentum_5, f.momentum_20, f.realized_volatility, f.volume_ratio, f.trend_strength]

    def train(
        self,
        feature_records: list[dict],
        *,
        label_source: str = "rule_based",
        test_fraction: float = 0.25,
        random_state: int = 42,
    ) -> bool:
        """Fit on externally-labelled records with a held-out evaluation.

        Returns True only when:
          * sklearn is available,
          * `label_source` is NOT `"rule_based"`,
          * at least 12 records spanning >=2 regimes are supplied,
          * the trained model's holdout accuracy is >= the rule baseline's
            holdout accuracy (otherwise the model is dropped).
        """
        if label_source == self.RULE_LABEL_SOURCE:
            self._last_train_metrics = {
                "rejected": True,
                "reason": "label_source=rule_based — refusing to fit on self-generated labels",
            }
            self._label_source = label_source
            return False
        self._label_source = label_source
        valid = [r for r in feature_records if r.get("regime") in self.REGIMES]
        if len(valid) < 8 or len({r["regime"] for r in valid}) < 2:
            self._last_train_metrics = {
                "rejected": True,
                "reason": f"insufficient_records ({len(valid)} valid; need >=8 spanning >=2 regimes)",
            }
            return False
        try:
            from sklearn.ensemble import GradientBoostingClassifier
            from sklearn.model_selection import train_test_split
        except Exception as exc:
            self._last_train_metrics = {
                "rejected": True,
                "reason": "sklearn_unavailable",
                "error": exc.__class__.__name__,
            }
            return False

        X = [
            [r["momentum_5"], r["momentum_20"], r["realized_volatility"], r["volume_ratio"], r["trend_strength"]]
            for r in valid
        ]
        y = [r["regime"] for r in valid]
        try:
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=test_fraction, random_state=random_state, stratify=y
            )
        except ValueError:
            # stratify can fail with sparse classes -> fall back to non-stratified split
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=test_fraction, random_state=random_state
            )

        model = GradientBoostingClassifier(n_estimators=50, max_depth=3, random_state=random_state)
        model.fit(X_train, y_train)
        model_acc = sum(1 for pred, actual in zip(model.predict(X_test), y_test) if pred == actual) / max(1, len(y_test))

        rule_acc = self._rule_baseline_accuracy(X_test, y_test)
        accepted = model_acc >= rule_acc + self._MIN_HOLDOUT_GAIN

        self._last_train_metrics = {
            "rejected": not accepted,
            "label_source": label_source,
            "holdout_accuracy": float(model_acc),
            "rule_holdout_accuracy": float(rule_acc),
            "train_samples": len(X_train),
            "test_samples": len(X_test),
            "accepted": accepted,
        }
        if not accepted:
            self._model = None
            self._trained = False
            return False
        self._model = model
        self._trained = True
        return True

    @classmethod
    def _rule_baseline_accuracy(cls, X_test: list[list[float]], y_test: list[str]) -> float:
        correct = 0
        for vec, label in zip(X_test, y_test):
            snap = FeatureSnapshot(
                symbol="_holdout_",
                close=0.0,
                momentum_5=vec[0],
                momentum_20=vec[1],
                realized_volatility=vec[2],
                volume_ratio=vec[3],
                trend_strength=vec[4],
            )
            if cls._rule_based(snap) == label:
                correct += 1
        return correct / max(1, len(y_test))

    def predict(self, features: FeatureSnapshot) -> str:
        if self._trained and self._model is not None:
            try:
                raw = self._model.predict([self._vec(features)])[0]
            except Exception:
                # sklearn can raise for malformed inputs; defensively fall back.
                return self._rule_based(features)
            label = str(raw)
            if label not in self.REGIMES:
                # Unknown / unexpected label: fall back to the deterministic
                # rule rather than propagate an out-of-vocabulary regime to
                # downstream gates. This protects the strategy router and
                # risk engine from being fed labels they do not recognise.
                return self._rule_based(features)
            return label
        return self._rule_based(features)

    def predict_proba(self, features: FeatureSnapshot) -> dict[str, float]:
        """Return probability dict per regime. Falls back to hard 1.0 if model not available."""
        if self._trained and self._model is not None:
            try:
                probs = self._model.predict_proba([self._vec(features)])[0]
                classes = list(self._model.classes_)
            except Exception:
                regime = self._rule_based(features)
                return {r: (1.0 if r == regime else 0.0) for r in self.REGIMES}
            # Filter to only known regimes and renormalise; drop unknowns.
            valid = {
                str(cls): float(prob)
                for cls, prob in zip(classes, probs)
                if str(cls) in self.REGIMES
            }
            total = sum(valid.values())
            if total <= 0.0:
                regime = self._rule_based(features)
                return {r: (1.0 if r == regime else 0.0) for r in self.REGIMES}
            normalised = {cls: prob / total for cls, prob in valid.items()}
            # Ensure every REGIME key is present, defaulting unseen to 0.0.
            return {regime: normalised.get(regime, 0.0) for regime in self.REGIMES}
        regime = self._rule_based(features)
        return {r: (1.0 if r == regime else 0.0) for r in self.REGIMES}

    @property
    def is_trained(self) -> bool:
        return self._trained

    @property
    def last_train_metrics(self) -> dict | None:
        return self._last_train_metrics

    @property
    def label_source(self) -> str | None:
        return self._label_source

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
        # Track observation count so the first few updates use a debiased
        # initialisation rather than seeding `prev = score` (which fully
        # retained the very first window's score with no smoothing).
        self._counts: dict[str, dict[str, int]] = {}

    def update(self, regime: str, strategy_name: str, score: float) -> None:
        """Update the EMA score for (regime, strategy_name).

        Implementation detail (fix for N5): the previous version seeded
        `prev = score` on the first observation, so the stored value was
        fully `score` — i.e. the EMA wasn't smoothing anything for the first
        window. We now seed `prev = 0` and apply the EMA blend uniformly,
        and track `_counts` so callers can tell the difference between a
        single observation and a converged EMA.
        """
        regime_map = self._scores.setdefault(regime, {})
        count_map = self._counts.setdefault(regime, {})
        prev = regime_map.get(strategy_name, 0.0)
        regime_map[strategy_name] = self._EMA_ALPHA * score + (1.0 - self._EMA_ALPHA) * prev
        count_map[strategy_name] = count_map.get(strategy_name, 0) + 1

    def observation_count(self, regime: str, strategy_name: str) -> int:
        """Return how many updates have been applied for (regime, strategy_name)."""
        return self._counts.get(regime, {}).get(strategy_name, 0)

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
