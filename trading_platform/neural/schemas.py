from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class NeuralFeatureBatch:
    """Normalized, model-ready feature tensor for a symbol."""
    symbol: str
    feature_ids: list[str]
    values: list[float]
    schema_version: str = "1.0"
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "feature_ids": self.feature_ids,
            "n_features": len(self.values),
            "schema_version": self.schema_version,
            "ts": self.ts.isoformat(),
        }


@dataclass
class ForecastPrediction:
    """Return and direction forecast for a symbol."""
    symbol: str
    direction_probability: float      # P(up) in (0, 1)
    expected_return: float            # estimated % return
    return_quantile_10: float = 0.0
    return_quantile_90: float = 0.0
    model_id: str = "baseline_ma"
    confidence: float = 0.5
    model_uncertainty: float = 0.5

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction_probability": self.direction_probability,
            "expected_return": self.expected_return,
            "return_quantile_10": self.return_quantile_10,
            "return_quantile_90": self.return_quantile_90,
            "model_id": self.model_id,
            "confidence": self.confidence,
            "model_uncertainty": self.model_uncertainty,
        }


@dataclass
class VolatilityPrediction:
    """Volatility and tail-risk forecast."""
    symbol: str
    predicted_volatility: float       # annualized
    garch_volatility: float           # GARCH-derived baseline
    tail_risk_score: float            # 0-1; higher = more extreme tail risk
    model_id: str = "garch_baseline"
    confidence: float = 0.5

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "predicted_volatility": self.predicted_volatility,
            "garch_volatility": self.garch_volatility,
            "tail_risk_score": self.tail_risk_score,
            "model_id": self.model_id,
            "confidence": self.confidence,
        }


@dataclass
class CorrelationRiskPrediction:
    """Cross-asset correlation risk estimate."""
    symbols: list[str]
    max_pairwise_correlation: float
    contagion_risk_score: float       # 0-1
    model_id: str = "rolling_corr"

    def to_dict(self) -> dict:
        return {
            "symbols": self.symbols,
            "max_pairwise_correlation": self.max_pairwise_correlation,
            "contagion_risk_score": self.contagion_risk_score,
            "model_id": self.model_id,
        }


@dataclass
class TailRiskPrediction:
    """Extreme move and drawdown prediction."""
    symbol: str
    extreme_move_probability: float   # P(|return| > 3σ)
    expected_max_drawdown: float
    model_id: str = "quantile_baseline"
    confidence: float = 0.5

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "extreme_move_probability": self.extreme_move_probability,
            "expected_max_drawdown": self.expected_max_drawdown,
            "model_id": self.model_id,
            "confidence": self.confidence,
        }


@dataclass
class NeuralPredictionBundle:
    """Full typed prediction output from the neural lab."""
    trace_id: str
    forecasts: list[ForecastPrediction] = field(default_factory=list)
    volatility: list[VolatilityPrediction] = field(default_factory=list)
    correlation_risk: CorrelationRiskPrediction | None = None
    tail_risks: list[TailRiskPrediction] = field(default_factory=list)
    model_versions: dict[str, str] = field(default_factory=dict)
    overall_uncertainty: float = 0.5
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "forecasts": [f.to_dict() for f in self.forecasts],
            "volatility": [v.to_dict() for v in self.volatility],
            "correlation_risk": self.correlation_risk.to_dict() if self.correlation_risk else None,
            "tail_risks": [t.to_dict() for t in self.tail_risks],
            "model_versions": self.model_versions,
            "overall_uncertainty": self.overall_uncertainty,
            "ts": self.ts.isoformat(),
        }
