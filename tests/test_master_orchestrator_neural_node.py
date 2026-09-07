"""Tests for MasterOrchestrator._node_neural_forecast()'s uncertainty capping.

Found 2026-09-07 (TradingQA's own investigation into a month of near-zero
profit): confirmed live that the orchestrator had run 1874+ cycles and
produced ZERO trade candidates, because neural_service.predict() always
returns overall_uncertainty=1.0 when bundle.forecasts is empty (see
neural/serving.py's own aggregate-uncertainty step) — and forecasts IS
always empty in this deployment, since no validated return_forecaster
model artifact exists (CLAUDE.md's own honesty-discipline note: AUC~=0.50,
correctly refused deployment). The existing "cap uncertainty when no bars
available" safeguard did not cover this much more common "bars exist, no
model" case, so every single cycle's uncertainty exceeded
NEURAL_UNCERTAINTY_VETO (0.82) and halted before any candidate could be
generated. These tests guard the fix directly, without needing a real
neural model or a live orchestrator run.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from trading_platform.neural.schemas import NeuralPredictionBundle
from trading_platform.orchestrator.master_orchestrator import (
    NEURAL_UNCERTAINTY_VETO,
    MasterOrchestrator,
)
from trading_platform.orchestrator.state import OrchestratorState


def _orchestrator(neural_service) -> MasterOrchestrator:
    runtime = SimpleNamespace(
        neural_service=neural_service,
        feature_store=SimpleNamespace(get_bars=lambda sym, limit=60: [{"close": 100.0}] * limit),
    )
    return MasterOrchestrator(runtime)


def _state() -> OrchestratorState:
    return OrchestratorState(trace_id="t1", underlying="RELIANCE", symbol_universe=["RELIANCE"])


def test_empty_forecasts_with_real_bars_does_not_veto():
    # The exact bug: bars exist (feature_store returns real bars above), but
    # no model ever produced a forecast — overall_uncertainty=1.0 is a
    # missing-analysis placeholder, not a genuine high-confidence "don't
    # trade" conclusion, and must not halt the cycle.
    bundle = NeuralPredictionBundle(trace_id="t1", forecasts=[], overall_uncertainty=1.0)
    orch = _orchestrator(SimpleNamespace(predict=lambda **kw: bundle))

    result = orch._node_neural_forecast(_state())

    assert result.halt is False
    assert result.updates["neural_uncertainty"] <= 0.70
    assert result.updates["neural_uncertainty"] < NEURAL_UNCERTAINTY_VETO


def test_real_forecast_with_genuinely_high_uncertainty_still_vetoes():
    # A REAL model producing a genuinely high uncertainty must still halt —
    # this fix narrows the safety net, it does not remove it.
    forecast = SimpleNamespace(direction_probability=0.5, expected_return=0.0, model_uncertainty=0.95)
    bundle = NeuralPredictionBundle(trace_id="t1", forecasts=[forecast], overall_uncertainty=0.95)
    orch = _orchestrator(SimpleNamespace(predict=lambda **kw: bundle))

    result = orch._node_neural_forecast(_state())

    assert result.halt is True
    assert result.updates["neural_uncertainty"] == 0.95


def test_real_forecast_with_low_uncertainty_passes_through_unchanged():
    forecast = SimpleNamespace(direction_probability=0.6, expected_return=0.01, model_uncertainty=0.3)
    bundle = NeuralPredictionBundle(trace_id="t1", forecasts=[forecast], overall_uncertainty=0.3)
    orch = _orchestrator(SimpleNamespace(predict=lambda **kw: bundle))

    result = orch._node_neural_forecast(_state())

    assert result.halt is False
    assert result.updates["neural_uncertainty"] == 0.3


def test_no_bars_available_still_capped_as_before():
    # Regression guard for the pre-existing "no bars" branch — must keep
    # working exactly as it did before this fix.
    orch = _orchestrator(SimpleNamespace(predict=lambda **kw: None))
    orch._runtime.feature_store = SimpleNamespace(get_bars=lambda sym, limit=60: [])

    bundle = NeuralPredictionBundle(trace_id="t1", forecasts=[], overall_uncertainty=1.0)
    orch._runtime.neural_service = SimpleNamespace(predict=lambda **kw: bundle)

    result = orch._node_neural_forecast(_state())

    assert result.halt is False
    assert result.updates["neural_uncertainty"] <= 0.70


def test_neural_service_absent_uses_neutral_default():
    orch = _orchestrator(None)
    result = orch._node_neural_forecast(_state())
    assert result.halt is False
    assert result.updates["neural_uncertainty"] == 0.5
