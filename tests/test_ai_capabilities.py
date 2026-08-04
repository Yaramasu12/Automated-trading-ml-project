"""Tests for the honest AI-capabilities report (review finding #4)."""
from __future__ import annotations

from types import SimpleNamespace

from trading_platform.api.ai_capabilities import ai_capabilities


def _runtime(*, gateway="stub", llm_runtime="stub", quantum="classical",
             gbm_available=False, neural=True, policies=None,
             enable_news_feed=False, active_news_events=0):
    neural_svc = None
    if neural:
        neural_svc = SimpleNamespace(_gbm_forecaster=SimpleNamespace(is_available=lambda: gbm_available))

    class Reg:
        def list_all(self): return policies if policies is not None else []
        def get(self, pid):
            kind = next((p.get("kind") for p in (policies or []) if p.get("policy_id") == pid), "real")
            return SimpleNamespace() if kind == "real" else type("MockPolicy", (), {})()

    news_intelligence = SimpleNamespace(
        feature_snapshot=lambda: {"active_event_count": active_news_events}
    )

    return SimpleNamespace(
        settings=SimpleNamespace(local_llm_gateway=gateway, local_llm_runtime=llm_runtime, quantum_backend=quantum,
                                  enable_news_feed=enable_news_feed),
        neural_service=neural_svc, policy_registry=Reg(), news_intelligence=news_intelligence,
    )


def test_degraded_stack_is_reported_honestly():
    cap = ai_capabilities(_runtime())
    assert cap["degraded"] is True
    assert cap["layers"]["llm_council"]["status"] == "stub"
    assert cap["layers"]["neural_forecast"]["status"] == "heuristic_baseline"
    # Quantum was removed entirely (it was classical theatre) — it must no longer
    # appear as an AI layer at all.
    assert "quantum" not in cap["layers"]
    # the note must make clear these do NOT block trades
    assert "ADVISORY" in cap["note"] and "do NOT" in cap["note"]


def test_validated_neural_not_degraded():
    cap = ai_capabilities(_runtime(gbm_available=True))
    assert cap["layers"]["neural_forecast"]["status"] == "validated_model"
    assert "neural_forecast" not in cap["degraded_layers"]


def test_neural_disabled_when_service_absent():
    cap = ai_capabilities(_runtime(neural=False))
    assert cap["layers"]["neural_forecast"]["status"] == "disabled"


def test_rl_mock_only_flagged_degraded():
    cap = ai_capabilities(_runtime(policies=[{"policy_id": "m1", "kind": "mock"}]))
    assert cap["layers"]["rl_marl"]["status"] == "mock_only"
    assert "rl_marl" in cap["degraded_layers"]


def test_lm_studio_runtime_reported_as_real():
    cap = ai_capabilities(_runtime(gateway="lm_studio", llm_runtime="lm_studio"))
    assert cap["layers"]["llm_council"]["status"] == "real"
    assert "llm_council" not in cap["degraded_layers"]


def test_news_sentiment_disabled_when_feed_off():
    cap = ai_capabilities(_runtime(enable_news_feed=False))
    assert cap["layers"]["news_sentiment"]["status"] == "disabled"
    assert "news_sentiment" in cap["degraded_layers"]


def test_news_sentiment_no_data_source_when_enabled_but_empty():
    cap = ai_capabilities(_runtime(enable_news_feed=True, active_news_events=0))
    assert cap["layers"]["news_sentiment"]["status"] == "no_data_source"
    assert "news_sentiment" in cap["degraded_layers"]


def test_news_sentiment_real_once_events_ingested():
    cap = ai_capabilities(_runtime(enable_news_feed=True, active_news_events=3))
    assert cap["layers"]["news_sentiment"]["status"] == "real"
    assert "news_sentiment" not in cap["degraded_layers"]
