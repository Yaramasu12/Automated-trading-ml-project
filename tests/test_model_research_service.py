"""Tests for the extracted ModelResearchService (Phase 2 runtime decomposition).

Behaviour-preservation checks for the statistical-advisory endpoints moved out of
the TradingRuntime god object.
"""
from __future__ import annotations

from dataclasses import dataclass

from trading_platform.api.model_research_service import ModelResearchService


@dataclass
class _Box:
    value: float = 1.0


class _Bar:
    def __init__(self, c): self.close = c


class _Synthetic:
    def generate_daily_bars(self, symbol, start, days):
        return [_Bar(100.0 + i) for i in range(days)]


class _Vol:
    def forecast(self, closes, model_name="ewma_volatility"): return _Box(0.2)
    def evaluate_interval(self, closes, forecast, in_sample=True): return _Box(0.9)
    def walk_forward_evaluate(self, closes, model_name="ewma_volatility"): return _Box(0.85)


class _Garch:
    def forecast(self, closes): return _Box(0.25)
    def fitted_params(self, closes): return {"omega": 0.1}
    def walk_forward_evaluate(self, closes): return _Box(0.8)


class _Sentiment:
    def analyze(self, text): return _Box(0.5)


class _Registry:
    def candidates(self): return [{"family": "vol"}, {"family": "vol"}, {"family": "trend"}]
    def select(self, performances):
        return type("R", (), {"selected_model": "m1", "reason": "best", "candidates": [_Box(1.0)]})()


class _RetrainAgent:
    def should_retrain(self, performance, target_gap_pct=0.0): return (True, "drift_high")


def _svc():
    return ModelResearchService(
        retraining_agent=_RetrainAgent(),
        model_registry=_Registry(),
        volatility_forecaster=_Vol(),
        sentiment_analyzer=_Sentiment(),
        garch_forecaster=_Garch(),
        synthetic_data=_Synthetic(),
    )


def test_model_catalog_groups_by_family():
    out = _svc().model_catalog()
    assert out["count"] == 3
    assert out["by_family"] == {"vol": 2, "trend": 1}


def test_retraining_decision_passes_through_agent():
    out = _svc().retraining_decision({"model_name": "x"})
    assert out["should_retrain"] is True
    assert out["reason"] == "drift_high"
    assert "evaluated_at" in out


def test_sentiment_requires_text():
    import pytest
    with pytest.raises(ValueError):
        _svc().sentiment({"text": "  "})
    assert "sentiment" in _svc().sentiment({"text": "good"})


def test_volatility_and_garch_use_payload_closes():
    vf = _svc().volatility_forecast({"closes": [1, 2, 3, 4, 5] * 6})
    assert "forecast" in vf and "walk_forward_evaluation" in vf
    gf = _svc().garch_forecast({"closes": [1, 2, 3, 4, 5] * 6})
    assert gf["garch_params"] == {"omega": 0.1}


def test_closes_fallback_to_synthetic():
    # No "closes" → uses synthetic_data provider
    closes = _svc()._closes_from_payload({"symbol": "NIFTY", "days": 5})
    assert len(closes) == 5


def test_model_selection_serializes_candidates():
    out = _svc().model_selection({"performances": [{"model_name": "a", "sharpe": 1.0}]})
    assert out["selected_model"] == "m1"
    assert isinstance(out["candidates"], list)
