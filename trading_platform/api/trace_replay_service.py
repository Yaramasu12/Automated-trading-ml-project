"""TraceReplayService — decision-lifecycle reconstruction, extracted from runtime.

Phase 2 of the runtime decomposition. This read-only service rebuilds a single
decision's lifecycle from the trace store, OMS events, paper-learning journal,
fills and outcome labels. It was previously ~270 lines of methods on the
TradingRuntime god object; it has no write side effects and depends only on a
handful of injected stores, so it lifts out cleanly.

The pure helpers are module-level functions (one of them, `decode_oms_event`, is
also used elsewhere in the runtime, so it lives here as the single source).
"""
from __future__ import annotations

import json
from typing import Any, Callable


# ── Pure helpers (were @staticmethods on TradingRuntime) ───────────────────────

def decode_oms_event(event: dict) -> dict:
    decoded = dict(event)
    metadata = decoded.get("metadata")
    if isinstance(metadata, str) and metadata:
        try:
            decoded["metadata"] = json.loads(metadata)
        except json.JSONDecodeError:
            decoded["metadata"] = {"raw": metadata}
    elif metadata is None:
        decoded["metadata"] = {}
    return decoded


def trace_order_ids(trace: dict) -> list[str]:
    ids = list(trace.get("order_intent_ids") or [])
    for event in trace.get("events", []):
        data = event.get("data") or {}
        order_id = data.get("order_id")
        if order_id:
            ids.append(str(order_id))
    return list(dict.fromkeys(ids))


def timeline_from_oms(event: dict) -> dict:
    metadata = event.get("metadata") or {}
    return {
        "ts": event.get("occurred_at"),
        "source": "oms",
        "event_type": event.get("event_type"),
        "component": "OMS",
        "order_id": event.get("order_id"),
        "symbol": event.get("symbol"),
        "reason": event.get("rejection_reason") or metadata.get("reason"),
        "data": event,
    }


def first_event_value(events: list[dict], key: str) -> object:
    for event in events:
        if event.get(key) is not None:
            return event[key]
    return None


def order_status_from_events(events: list[dict]) -> str:
    event_types = [event.get("event_type") for event in events]
    if "broker_filled" in event_types:
        return "FILLED"
    if "broker_rejected" in event_types:
        return "BROKER_REJECTED"
    if "risk_rejected" in event_types:
        return "RISK_REJECTED"
    if "capital_check_failed" in event_types:
        return "CAPITAL_REJECTED"
    if "compliance_rejected" in event_types:
        return "COMPLIANCE_REJECTED"
    if "broker_submitted" in event_types:
        return "SUBMITTED"
    if "intent_queued" in event_types:
        return "QUEUED"
    return "TRACE_ONLY"


def trace_replay_summary(
    trace: dict,
    orders: list[dict],
    labels: list[dict],
    trades: list[dict],
    timeline: list[dict],
    journal_events: list[dict] | None = None,
) -> dict:
    journal_events = journal_events or []
    event_types = {str(item.get("event_type")) for item in timeline}
    order_statuses = [order["status"] for order in orders]
    has_rejection = any(status.endswith("REJECTED") for status in order_statuses) or "final_gate_rejected" in event_types
    has_fill = (
        any(status == "FILLED" for status in order_statuses)
        or "broker_filled" in event_types
        or "fill_recorded" in event_types
    )
    journal_event_counts: dict[str, int] = {}
    for event in journal_events:
        event_type = str(event.get("event_type") or "")
        journal_event_counts[event_type] = journal_event_counts.get(event_type, 0) + 1
    status = "DECISION_ONLY"
    if has_rejection:
        status = "REJECTED"
    elif labels:
        status = "LABELED"
    elif has_fill:
        status = "FILLED"
    elif orders:
        status = "ORDER_IN_PROGRESS"

    required = ["decision_cycle_started", "baseline_scan_completed"] if trace.get("metadata", {}).get("baseline_summary") else []
    if orders:
        required.append("order_intent_created")
        required.append("final_gate_rejected" if has_rejection else "final_gate_approved")
    if has_fill:
        required.append("broker_filled")
    if labels:
        required.extend(["outcome_label_created", "post_trade_learning_updated"])
    missing = [item for item in required if item not in event_types]
    return {
        "status": status,
        "execution_mode": trace.get("execution_mode"),
        "symbols": trace.get("symbol_universe", []),
        "order_count": len(orders),
        "timeline_event_count": len(timeline),
        "trace_event_count": len(trace.get("events", [])),
        "oms_event_count": sum(len(order["events"]) for order in orders),
        "journal_event_count": len(journal_events),
        "journal_event_counts": journal_event_counts,
        "trade_count": len(trades),
        "fill_count": int(journal_event_counts.get("fill_recorded", len(trades))),
        "slippage_count": int(journal_event_counts.get("slippage_recorded", 0)),
        "label_count": len(labels),
        "learning_update_count": int(journal_event_counts.get("post_trade_learning_updated", 0)),
        "rejection_count": sum(1 for status in order_statuses if status.endswith("REJECTED")),
        "broker_submitted": "broker_submitted" in event_types,
        "broker_filled": "broker_filled" in event_types,
        "lifecycle_complete": not missing and status in {"DECISION_ONLY", "REJECTED", "LABELED"},
        "missing_stages": missing,
    }


class TraceReplayService:
    """Read-only reconstruction of decision lifecycles from the trace + OMS + journal."""

    def __init__(
        self,
        *,
        trace_store: Any,
        oms: Any,
        portfolio: Any,
        journal_getter: Callable[[], Any],
        outcome_factory: Any,
        serialize_trade: Callable[[Any], dict],
    ) -> None:
        self._trace_store = trace_store
        self._oms = oms
        self._portfolio = portfolio
        # Read paper_learning_journal dynamically — the runtime can reassign it
        # after construction (test isolation / restore_state).
        self._journal_getter = journal_getter
        self._outcome_factory = outcome_factory
        self._serialize_trade = serialize_trade

    @property
    def _paper_learning_journal(self):
        return self._journal_getter()

    def get_trace(self, trace_id: str) -> dict | None:
        trace = self._trace_store.get(trace_id)
        if trace is None:
            return None
        return trace.to_dict()

    def list_traces(self, max_traces: int = 50) -> dict:
        traces = list(self._trace_store.iter_recent(max_traces))
        return {"count": len(traces), "traces": traces}

    def trace_replay(self, trace_id: str) -> dict | None:
        """Reconstruct one decision lifecycle from trace, OMS, fills, and labels."""
        trace = self._trace_store.get(trace_id)
        if trace is None:
            return None
        trace_dict = trace.to_dict()
        order_ids = trace_order_ids(trace_dict)
        orders = []
        oms_timeline = []
        broker_order_ids: set[str] = set()
        for order_id in order_ids:
            events = [decode_oms_event(event) for event in self._oms.events_for_order(order_id)]
            for event in events:
                if event.get("broker_order_id"):
                    broker_order_ids.add(str(event["broker_order_id"]))
                oms_timeline.append(timeline_from_oms(event))
            orders.append({
                "order_id": order_id,
                "status": order_status_from_events(events),
                "symbol": first_event_value(events, "symbol"),
                "strategy_name": first_event_value(events, "strategy_name"),
                "events": events,
            })

        journal_events = self._paper_learning_journal.events_for_trace(trace_id)
        journal_labels = [
            dict(event.get("payload") or {})
            for event in journal_events
            if event.get("event_type") == "outcome_label_created"
        ]
        label_candidates = [
            label
            for label in self._outcome_factory.recent_labels(5000)
            if label.get("trace_id") == trace_id
        ]
        label_candidates.extend(journal_labels)
        labels_by_key: dict[tuple, dict] = {}
        for label in label_candidates:
            key = (
                label.get("trace_id"),
                label.get("symbol"),
                label.get("ts"),
                label.get("entry_price"),
                label.get("exit_price"),
            )
            labels_by_key[key] = label
        labels = list(labels_by_key.values())
        fills = [
            dict(event.get("payload") or {})
            for event in journal_events
            if event.get("event_type") == "fill_recorded"
        ]
        slippage = [
            dict(event.get("payload") or {})
            for event in journal_events
            if event.get("event_type") == "slippage_recorded"
        ]
        trades = [
            self._serialize_trade(trade)
            for trade in self._portfolio.trades
            if trade.order_id in set(order_ids) or trade.order_id in broker_order_ids
        ]

        timeline = [
            {
                "ts": event.get("ts"),
                "source": "trace",
                "event_type": event.get("event_type"),
                "component": event.get("component"),
                "order_id": (event.get("data") or {}).get("order_id"),
                "symbol": (event.get("data") or {}).get("symbol"),
                "reason": (event.get("data") or {}).get("reason"),
                "data": event.get("data") or {},
            }
            for event in trace_dict.get("events", [])
        ]
        timeline.extend(oms_timeline)
        for event in journal_events:
            payload = event.get("payload") or {}
            timeline.append({
                "ts": event.get("occurred_at"),
                "source": "paper_journal",
                "event_type": event.get("event_type"),
                "component": "PaperLearningJournal",
                "order_id": event.get("order_id"),
                "symbol": event.get("symbol"),
                "reason": payload.get("barrier_outcome") or payload.get("reason"),
                "data": payload,
            })
        for label in labels:
            timeline.append({
                "ts": label.get("ts"),
                "source": "label",
                "event_type": "outcome_label_created",
                "component": "OutcomeFactory",
                "order_id": None,
                "symbol": label.get("symbol"),
                "reason": label.get("barrier_outcome"),
                "data": label,
            })
        timeline.sort(key=lambda item: str(item.get("ts") or ""))

        summary = trace_replay_summary(
            trace_dict,
            orders,
            labels,
            trades,
            timeline,
            journal_events,
        )
        return {
            "trace_id": trace_id,
            "summary": summary,
            "trace": trace_dict,
            "timeline": timeline,
            "orders": orders,
            "labels": labels,
            "fills": fills,
            "slippage": slippage,
            "trades": trades,
            "risk_decisions": trace_dict.get("risk_decisions", []),
            "paper_journal": {
                "event_count": len(journal_events),
                "events": journal_events,
            },
            "raw": {
                "trace_events": trace_dict.get("events", []),
                "oms_event_count": sum(len(order["events"]) for order in orders),
                "paper_journal_event_count": len(journal_events),
            },
        }
