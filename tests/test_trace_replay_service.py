"""Tests for the extracted TraceReplayService (Phase 2 runtime decomposition).

These lock in behaviour-preservation: the reconstruction logic moved out of the
TradingRuntime god object must produce the same lifecycle structure.
"""
from __future__ import annotations

from trading_platform.api.trace_replay_service import (
    TraceReplayService,
    decode_oms_event,
    order_status_from_events,
    trace_order_ids,
)


# ── Pure helpers ────────────────────────────────────────────────────────────────

def test_order_status_priority():
    assert order_status_from_events([{"event_type": "intent_queued"}, {"event_type": "broker_filled"}]) == "FILLED"
    assert order_status_from_events([{"event_type": "risk_rejected"}]) == "RISK_REJECTED"
    assert order_status_from_events([{"event_type": "intent_queued"}]) == "QUEUED"
    assert order_status_from_events([]) == "TRACE_ONLY"


def test_trace_order_ids_dedup_and_order():
    trace = {"order_intent_ids": ["a"], "events": [{"data": {"order_id": "b"}}, {"data": {"order_id": "a"}}]}
    assert trace_order_ids(trace) == ["a", "b"]


def test_decode_oms_event_parses_json_metadata():
    assert decode_oms_event({"metadata": '{"x": 1}'})["metadata"] == {"x": 1}
    assert decode_oms_event({"metadata": None})["metadata"] == {}
    assert decode_oms_event({"metadata": "not json"})["metadata"] == {"raw": "not json"}


# ── Service reconstruction with fakes ─────────────────────────────────────────────

class _Trace:
    def __init__(self, d): self._d = d
    def to_dict(self): return self._d


class _FakeStore:
    def __init__(self, trace): self._trace = trace
    def get(self, _tid): return self._trace
    def iter_recent(self, n): return [{"trace_id": "t1"}][:n]


class _FakeOMS:
    def events_for_order(self, order_id):
        return [{"event_type": "broker_filled", "order_id": order_id, "symbol": "NIFTY",
                 "broker_order_id": "B1", "occurred_at": "2026-01-01T00:00:00", "metadata": "{}"}]


class _Trade:
    order_id = "o1"


class _FakePortfolio:
    trades = [_Trade()]


class _FakeJournal:
    def events_for_trace(self, _tid): return []


class _FakeOutcome:
    def recent_labels(self, _n): return []


def _service(trace_dict):
    return TraceReplayService(
        trace_store=_FakeStore(_Trace(trace_dict)),
        oms=_FakeOMS(),
        portfolio=_FakePortfolio(),
        journal_getter=lambda: _FakeJournal(),
        outcome_factory=_FakeOutcome(),
        serialize_trade=lambda t: {"order_id": t.order_id},
    )


def test_get_trace_and_list():
    svc = _service({"events": []})
    assert svc.get_trace("t1") == {"events": []}
    assert svc.list_traces(50)["count"] == 1


def test_trace_replay_reconstructs_filled_order():
    trace = {"order_intent_ids": ["o1"], "events": [], "execution_mode": "PAPER", "symbol_universe": ["NIFTY"]}
    replay = _service(trace).trace_replay("t1")
    assert replay["trace_id"] == "t1"
    assert replay["orders"][0]["status"] == "FILLED"
    assert replay["summary"]["broker_filled"] is True
    # the trade whose order_id matches the broker_order_id (B1) or order id is serialized
    assert {"order_id": "o1"} in replay["trades"]


def test_trace_replay_missing_returns_none():
    svc = TraceReplayService(
        trace_store=type("S", (), {"get": staticmethod(lambda _t: None)})(),
        oms=_FakeOMS(), portfolio=_FakePortfolio(), journal_getter=lambda: _FakeJournal(),
        outcome_factory=_FakeOutcome(), serialize_trade=lambda t: {},
    )
    assert svc.trace_replay("missing") is None
