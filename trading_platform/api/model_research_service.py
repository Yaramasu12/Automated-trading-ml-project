"""ModelResearchService — statistical model advisory endpoints, extracted from runtime.

Phase 2 of the runtime decomposition. These read-only endpoints power the AI/Models
views: volatility & GARCH forecasts, sentiment, model selection/catalog, and the
retraining recommendation. They depend only on the research model objects, so they
lift cleanly out of the TradingRuntime god object.

Logic is a verbatim move; only `self.<dep>` became injected `self._<dep>`.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any

from trading_platform.ai.agents import ModelPerformance


class ModelResearchService:
    def __init__(
        self,
        *,
        retraining_agent: Any,
        model_registry: Any,
        volatility_forecaster: Any,
        sentiment_analyzer: Any,
        garch_forecaster: Any,
        synthetic_data: Any,
    ) -> None:
        self._retraining_agent = retraining_agent
        self._model_registry = model_registry
        self._volatility_forecaster = volatility_forecaster
        self._sentiment_analyzer = sentiment_analyzer
        self._garch_forecaster = garch_forecaster
        self._synthetic_data = synthetic_data

    def retraining_decision(self, payload: dict) -> dict:
        performance = ModelPerformance(
            model_name=payload.get("model_name", "candidate"),
            profit_factor=float(payload.get("profit_factor", 1.0)),
            sharpe=float(payload.get("sharpe", 0.0)),
            drawdown=float(payload.get("drawdown", 0.0)),
            iv_interval_coverage=float(payload.get("iv_interval_coverage", 0.95)),
            sentiment_precision=float(payload.get("sentiment_precision", 0.90)),
            sample_size=int(payload.get("sample_size", 0)),
            feature_drift_score=float(payload.get("feature_drift_score", 0.0)),
        )
        should_retrain, reason = self._retraining_agent.should_retrain(
            performance,
            target_gap_pct=float(payload.get("target_gap_pct", 0.0)),
        )
        return {
            "should_retrain": should_retrain,
            "reason": reason,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

    def model_catalog(self) -> dict:
        candidates = self._model_registry.candidates()
        by_family: dict[str, int] = {}
        for candidate in candidates:
            by_family[candidate["family"]] = by_family.get(candidate["family"], 0) + 1
        return {
            "count": len(candidates),
            "by_family": by_family,
            "models": candidates,
        }

    def volatility_forecast(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        closes = self._closes_from_payload(payload)
        model_name = str(payload.get("model_name", "ewma_volatility"))
        forecast = self._volatility_forecaster.forecast(closes, model_name=model_name)
        in_sample_eval = self._volatility_forecaster.evaluate_interval(
            closes[-min(len(closes), 20):], forecast, in_sample=True
        )
        result = {
            "forecast": asdict(forecast),
            "interval_evaluation": asdict(in_sample_eval),
            "policy": {
                "promotion_gate": "Candidate volatility models must keep OUT-OF-SAMPLE interval coverage above 85% before live use.",
                "current_use": "baseline risk sizing and IV sanity checks",
                "interval_evaluation_note": (
                    "interval_evaluation.in_sample=True; this number is computed on the same "
                    "closes the forecast was derived from. Use walk_forward_evaluation for an "
                    "honest forecast hit-rate."
                ),
            },
        }
        try:
            wf = self._volatility_forecaster.walk_forward_evaluate(closes, model_name=model_name)
            result["walk_forward_evaluation"] = asdict(wf)
        except ValueError as exc:
            result["walk_forward_evaluation"] = {"error": str(exc)}
        return result

    def sentiment(self, payload: dict) -> dict:
        text = str(payload.get("text", ""))
        if not text.strip():
            raise ValueError("text is required")
        result = self._sentiment_analyzer.analyze(text)
        return {
            "sentiment": asdict(result),
            "policy": {
                "live_gate": "Sentiment models can influence ranking only after precision exceeds 90% on labeled data.",
            },
        }

    def model_selection(self, payload: dict) -> dict:
        performances = [
            ModelPerformance(
                model_name=item.get("model_name", item.get("name", "candidate")),
                profit_factor=float(item.get("profit_factor", 1.0)),
                sharpe=float(item.get("sharpe", 0.0)),
                drawdown=float(item.get("drawdown", 0.0)),
                iv_interval_coverage=float(item.get("iv_interval_coverage", 0.95)),
                sentiment_precision=float(item.get("sentiment_precision", 0.90)),
                sample_size=int(item.get("sample_size", 0)),
                feature_drift_score=float(item.get("feature_drift_score", 0.0)),
            )
            for item in payload.get("performances", [])
        ]
        result = self._model_registry.select(performances)
        return {
            "selected_model": result.selected_model,
            "reason": result.reason,
            "candidates": [asdict(candidate) for candidate in result.candidates],
        }

    def _closes_from_payload(self, payload: dict) -> list[float]:
        if payload.get("closes"):
            return [float(value) for value in payload["closes"]]
        symbol = str(payload.get("symbol", "NIFTY")).upper()
        start_raw = str(payload.get("start", "2026-01-01"))
        days = int(payload.get("days", 30))
        bars = self._synthetic_data.generate_daily_bars(symbol, date.fromisoformat(start_raw), days)
        return [bar.close for bar in bars]

    def garch_forecast(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        closes = self._closes_from_payload(payload)
        forecast = self._garch_forecaster.forecast(closes)
        in_sample_eval = self._volatility_forecaster.evaluate_interval(
            closes[-min(len(closes), 20):], forecast, in_sample=True
        )
        params = self._garch_forecaster.fitted_params(closes)
        result = {
            "forecast": asdict(forecast),
            "garch_params": params,
            "interval_evaluation": asdict(in_sample_eval),
            "interval_evaluation_note": (
                "in_sample=True — fitted-on-itself coverage. Use walk_forward_evaluation for a "
                "genuine out-of-sample forecast hit-rate."
            ),
        }
        try:
            wf = self._garch_forecaster.walk_forward_evaluate(closes)
            result["walk_forward_evaluation"] = asdict(wf)
        except ValueError as exc:
            result["walk_forward_evaluation"] = {"error": str(exc)}
        return result
