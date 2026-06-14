"""Tests for the extracted RegimeMetaService (Phase 2 runtime decomposition)."""
from __future__ import annotations

from datetime import datetime, timedelta

from trading_platform.api.regime_meta_service import RegimeMetaService
from trading_platform.domain.models import MarketBar


class _RegimeClf:
    is_trained = False
    label_source = "rule_based"
    last_train_metrics = None
    def predict(self, f): return "MEAN_REVERTING"
    def predict_proba(self, f): return {"MEAN_REVERTING": 1.0}


class _Item:
    def __init__(self, n, s, r): self.strategy_name, self.score, self.rank = n, s, r


class _MetaModel:
    last_update = None
    def rank(self, regime, names): return [_Item("a", 0.9, 1)]
    def summary(self): return {"count": 1}
    def update(self, regime, name, score): self.last_update = (regime, name, score)


class _StrategyFactory:
    def names(self): return ["a", "b"]


class _News:
    def __init__(self, action="MONITOR"): self._action = action
    def feature_snapshot(self): return {"recommended_action": self._action}


class _Synthetic:
    def generate_daily_bars(self, symbol, start, days):
        base = datetime(2026, 1, 1)
        return [MarketBar(timestamp=base + timedelta(days=i), symbol=symbol,
                          open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=100000)
                for i in range(days)]


def _svc(news_action="MONITOR", meta=None):
    return RegimeMetaService(
        regime_classifier=_RegimeClf(), synthetic_data=_Synthetic(),
        news_intelligence=_News(news_action), meta_model=meta or _MetaModel(),
        strategy_factory=_StrategyFactory(),
    )


def test_regime_classify_with_features():
    out = _svc().regime_classify({"features": {
        "close": 100, "momentum_5": 0.1, "momentum_20": 0.2,
        "realized_volatility": 0.01, "volume_ratio": 1.2, "trend_strength": 3}})
    assert out["regime"] == "MEAN_REVERTING"
    assert out["classifier_trained"] is False
    assert "features" in out


def test_meta_model_rank_and_update():
    mm = _MetaModel()
    svc = _svc(meta=mm)
    rk = svc.meta_model_rank({"regime": "TRENDING"})
    assert rk["ranked_strategies"][0]["strategy_name"] == "a"
    up = svc.meta_model_update({"regime": "X", "strategy_name": "a", "score": 0.5})
    assert up["updated"] is True
    assert mm.last_update == ("X", "a", 0.5)


def test_current_regime_event_risk_adjustment():
    # BLOCK_ENTRIES news must override the base regime to EVENT_RISK.
    cr = _svc(news_action="BLOCK_ENTRIES").current_regime("NIFTY")
    assert cr["adjusted_regime"] == "EVENT_RISK"
    assert cr["regime"] == "MEAN_REVERTING"
    assert "news_features" in cr


def test_current_regime_no_event_keeps_base():
    cr = _svc(news_action="MONITOR").current_regime("NIFTY")
    assert cr["adjusted_regime"] == "MEAN_REVERTING"
