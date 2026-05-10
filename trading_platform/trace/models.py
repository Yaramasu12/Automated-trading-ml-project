from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class TraceEvent:
    """A single timestamped event within a decision trace."""
    event_type: str
    component: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "component": self.component,
            "data": self.data,
            "ts": self.ts.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> TraceEvent:
        return cls(
            event_type=d["event_type"],
            component=d["component"],
            data=d.get("data", {}),
            ts=datetime.fromisoformat(d["ts"]),
        )


@dataclass
class DecisionTrace:
    """Full trace of one scan/order decision path."""
    trace_id: str
    created_at: datetime
    execution_mode: str
    symbol_universe: list[str] = field(default_factory=list)
    feature_snapshot_ids: dict[str, str] = field(default_factory=dict)
    agent_outputs: list[dict] = field(default_factory=list)
    neural_model_versions: dict[str, str] = field(default_factory=dict)
    quantum_result_id: str | None = None
    risk_decisions: list[dict] = field(default_factory=list)
    order_intent_ids: list[str] = field(default_factory=list)
    broker_result_id: str | None = None
    events: list[TraceEvent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_event(self, event_type: str, component: str, data: dict | None = None) -> None:
        self.events.append(TraceEvent(event_type=event_type, component=component, data=data or {}))

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "created_at": self.created_at.isoformat(),
            "execution_mode": self.execution_mode,
            "symbol_universe": self.symbol_universe,
            "feature_snapshot_ids": self.feature_snapshot_ids,
            "agent_outputs": self.agent_outputs,
            "neural_model_versions": self.neural_model_versions,
            "quantum_result_id": self.quantum_result_id,
            "risk_decisions": self.risk_decisions,
            "order_intent_ids": self.order_intent_ids,
            "broker_result_id": self.broker_result_id,
            "events": [e.to_dict() for e in self.events],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DecisionTrace:
        t = cls(
            trace_id=d["trace_id"],
            created_at=datetime.fromisoformat(d["created_at"]),
            execution_mode=d["execution_mode"],
            symbol_universe=d.get("symbol_universe", []),
            feature_snapshot_ids=d.get("feature_snapshot_ids", {}),
            agent_outputs=d.get("agent_outputs", []),
            neural_model_versions=d.get("neural_model_versions", {}),
            quantum_result_id=d.get("quantum_result_id"),
            risk_decisions=d.get("risk_decisions", []),
            order_intent_ids=d.get("order_intent_ids", []),
            broker_result_id=d.get("broker_result_id"),
            metadata=d.get("metadata", {}),
        )
        t.events = [TraceEvent.from_dict(e) for e in d.get("events", [])]
        return t

    @staticmethod
    def _is_secret_key(key: str) -> bool:
        lower = key.lower()
        return any(s in lower for s in ("token", "secret", "password", "api_key", "credential", "pin", "totp"))
