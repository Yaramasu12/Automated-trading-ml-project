"""RegimeMetaService — regime classification + meta-model ranking, extracted from runtime.

Phase 2 of the runtime decomposition. Regime inference (rule/ML classifier),
news-adjusted current regime, and the meta-model strategy ranking/update. Deps
injected under their EXACT runtime attribute names; bodies are a verbatim move.
None of these deps are rebuilt by _rebuild_market_engines, so by-reference
injection is safe.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any


class RegimeMetaService:
    def __init__(
        self,
        *,
        regime_classifier: Any,
        synthetic_data: Any,
        news_intelligence: Any,
        meta_model: Any,
        strategy_factory: Any,
    ) -> None:
        self.regime_classifier = regime_classifier
        self.synthetic_data = synthetic_data
        self.news_intelligence = news_intelligence
        self.meta_model = meta_model
        self.strategy_factory = strategy_factory

    def regime_classify(self, payload: dict) -> dict:
        symbol = str(payload.get("symbol", "NIFTY")).upper()
        start_raw = str(payload.get("start", "2026-01-01"))
        days = int(payload.get("days", 30))

        if payload.get("features"):
            from trading_platform.ai.features import FeatureSnapshot
            f = payload["features"]
            features = FeatureSnapshot(
                symbol=symbol,
                close=float(f.get("close", 0)),
                momentum_5=float(f.get("momentum_5", 0)),
                momentum_20=float(f.get("momentum_20", 0)),
                realized_volatility=float(f.get("realized_volatility", 0)),
                volume_ratio=float(f.get("volume_ratio", 1)),
                trend_strength=float(f.get("trend_strength", 0)),
            )
        else:
            bars = self.synthetic_data.generate_daily_bars(symbol, date.fromisoformat(start_raw), days)
            from trading_platform.ai.features import FeatureEngine
            features = FeatureEngine().compute(bars)

        # We deliberately do NOT auto-train on FeatureStore records here:
        # those records are labelled by the rule-based regime agent, and
        # fitting sklearn to its own teacher's labels has no validation
        # value (see RegimeClassifier.train docstring). The classifier
        # therefore stays in deterministic rule mode unless an external
        # caller invokes `regime_classifier.train(records, label_source=...)`
        # with non-rule labels.
        regime = self.regime_classifier.predict(features)
        proba = self.regime_classifier.predict_proba(features)
        return {
            "symbol": symbol,
            "regime": regime,
            "probabilities": proba,
            "classifier_trained": self.regime_classifier.is_trained,
            "label_source": self.regime_classifier.label_source,
            "training_metrics": self.regime_classifier.last_train_metrics,
            "features": asdict(features),
        }

    def meta_model_rank(self, payload: dict) -> dict:
        regime = str(payload.get("regime", "MEAN_REVERTING"))
        strategy_names = [str(n) for n in payload.get("strategy_names", self.strategy_factory.names())]
        ranked = self.meta_model.rank(regime, strategy_names)
        return {
            "regime": regime,
            "ranked_strategies": [
                {"strategy_name": item.strategy_name, "score": item.score, "rank": item.rank}
                for item in ranked
            ],
            "summary": self.meta_model.summary(),
        }

    def meta_model_update(self, payload: dict) -> dict:
        regime = str(payload["regime"])
        strategy_name = str(payload["strategy_name"])
        score = float(payload["score"])
        self.meta_model.update(regime, strategy_name, score)
        return {"updated": True, "regime": regime, "strategy_name": strategy_name, "score": score}

    def current_regime(self, symbol: str = "NIFTY") -> dict:
        features_payload = self.regime_classify({"symbol": symbol, "days": 30})
        news_features = self.news_intelligence.feature_snapshot()
        action = news_features["recommended_action"]
        adjusted_regime = features_payload["regime"]
        if action in {"BLOCK_ENTRIES", "MANUAL_APPROVAL"}:
            adjusted_regime = "EVENT_RISK"
        elif action == "REDUCE_SIZE" and adjusted_regime != "HIGH_VOLATILITY":
            adjusted_regime = f"{adjusted_regime}_EVENT_RISK"
        return {
            **features_payload,
            "adjusted_regime": adjusted_regime,
            "news_features": news_features,
        }

