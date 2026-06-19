"""Tests for the honest AI-capabilities report (review finding #4)."""
from __future__ import annotations

from types import SimpleNamespace

from trading_platform.api.ai_capabilities import ai_capabilities


def _runtime(*, gateway="stub", llm_runtime="stub", quantum="classical",
             gbm_available=False, neural=True, policies=None):
    neural_svc = None
    if neural:
        neural_svc = SimpleNamespace(_gbm_forecaster=SimpleNamespace(is_available=lambda: gbm_available))

    class Reg:
        def list_all(self): return policies if policies is not None else []
        def get(self, pid):
            kind = next((p.get("kind") for p in (policies or []) if p.get("policy_id") == pid), "real")
            return SimpleNamespace() if kind == "real" else type("MockPolicy", (), {})()

    return SimpleNamespace(
        settings=SimpleNamespace(local_llm_gateway=gateway, local_llm_runtime=llm_runtime, quantum_backend=quantum),
        neural_service=neural_svc, policy_registry=Reg(),
    )


def test_degraded_stack_is_reported_honestly():
    cap = ai_capabilities(_runtime())
    assert cap["degraded"] is True
    assert cap["layers"]["llm_council"]["status"] == "stub"
    assert cap["layers"]["quantum"]["status"] == "classical_fallback"
    assert cap["layers"]["neural_forecast"]["status"] == "heuristic_baseline"
    # the note must make clear these do NOT block trades
    assert "ADVISORY" in cap["note"] and "do NOT" in cap["note"]


def test_validated_neural_and_real_quantum_not_degraded_for_those_layers():
    cap = ai_capabilities(_runtime(quantum="qiskit", gbm_available=True))
    assert cap["layers"]["quantum"]["status"] == "real"
    assert cap["layers"]["neural_forecast"]["status"] == "validated_model"
    assert "quantum" not in cap["degraded_layers"]
    assert "neural_forecast" not in cap["degraded_layers"]


def test_neural_disabled_when_service_absent():
    cap = ai_capabilities(_runtime(neural=False))
    assert cap["layers"]["neural_forecast"]["status"] == "disabled"


def test_rl_mock_only_flagged_degraded():
    cap = ai_capabilities(_runtime(policies=[{"policy_id": "m1", "kind": "mock"}]))
    assert cap["layers"]["rl_marl"]["status"] == "mock_only"
    assert "rl_marl" in cap["degraded_layers"]
