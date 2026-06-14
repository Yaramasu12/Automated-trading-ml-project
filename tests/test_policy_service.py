"""Tests for the extracted PolicyService (Phase 2 — the most entangled cluster).

Covers the public surface (list/promote/rollback), the static helpers, and the
live-canary readiness chain that the final execution gate consults.
"""
from __future__ import annotations

from trading_platform.api.policy_service import PolicyService, _POLICY_STATUS_ORDER


_REQ = {
    "min_paper_trading_days": 5, "min_paper_fills": 20, "min_paper_labels": 20,
    "min_slippage_records": 20, "min_learning_updates": 20, "min_complete_replays": 1,
    "min_profit_factor": 1.10, "min_sharpe": 0.50, "max_drawdown_pct": 0.10,
    "max_avg_slippage_surprise": 0.0015,
}


class _Registry:
    _records: dict = {}
    def list_all(self): return []
    def get_record(self, pid): return None
    def promote(self, pid, status): return False


class _Journal:
    def events(self): return []
    def events_for_trace(self, t): return []


def _svc() -> PolicyService:
    return PolicyService(
        policy_registry=_Registry(),
        policy_promotion_requirements=_REQ,
        paper_learning_journal=_Journal(),
        trace_replay=lambda t: None,
        float_or_none=lambda v: float(v) if v is not None else None,
    )


def test_status_ladder_constant_moved():
    assert _POLICY_STATUS_ORDER[0] == "research"
    assert _POLICY_STATUS_ORDER[-1] == "live_approved"


def test_next_policy_status():
    s = _svc()
    assert s._next_policy_status("paper") == "live_canary"
    assert s._next_policy_status("live_approved") is None
    assert s._next_policy_status("disabled") is None


def test_promotion_check_shape():
    c = _svc()._promotion_check("gate", False, ">=1", 0, "too_low")
    assert c["name"] == "gate" and c["passed"] is False and c["reason"] == "too_low"


def test_list_policies_empty():
    assert _svc().list_policies() == {"policies": []}


def test_live_canary_readiness_not_ready_without_evidence():
    r = _svc().live_canary_readiness_payload()
    assert r["status"] == "NOT_READY"
    assert r["can_consider_live_canary"] is False
    assert isinstance(r["blocking_reasons"], list) and r["blocking_reasons"]


def test_promote_and_rollback_unknown_policy_safe():
    s = _svc()
    assert s.policy_promotion_gate("nope", "live_canary")["approved"] is False
    assert s.promote_policy({"policy_id": "nope", "status": "live_canary"})["ok"] is False
    assert "ok" in s.rollback_policy({"policy_id": "nope"})
