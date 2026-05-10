from __future__ import annotations

"""Automated postmortem factory — triggered on large losses or unexpected rejections."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class PostmortemItem:
    postmortem_id: str
    trace_id: str
    failure_mode: str
    loss_amount: float
    predicted_return: float
    realized_return: float
    prediction_error: float
    research_issue: str
    model_decay_flagged: bool = False
    summary: str = ""
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "postmortem_id": self.postmortem_id,
            "trace_id": self.trace_id,
            "failure_mode": self.failure_mode,
            "loss_amount": self.loss_amount,
            "predicted_return": self.predicted_return,
            "realized_return": self.realized_return,
            "prediction_error": self.prediction_error,
            "research_issue": self.research_issue,
            "model_decay_flagged": self.model_decay_flagged,
            "summary": self.summary,
            "ts": self.ts.isoformat(),
        }


class PostmortemFactory:
    """Generates automated postmortems for large losses."""

    def __init__(self, loss_threshold: float = 0.02) -> None:
        self._threshold = loss_threshold
        self._items: list[PostmortemItem] = []
        self._id_counter = 0

    def evaluate(
        self,
        trace_id: str,
        predicted_return: float,
        realized_return: float,
        portfolio_equity: float,
    ) -> PostmortemItem | None:
        loss_pct = -realized_return
        if loss_pct < self._threshold:
            return None

        self._id_counter += 1
        pm_id = f"pm-{self._id_counter:04d}-{trace_id[:8]}"
        pred_error = abs(predicted_return - realized_return)

        # Classify failure mode
        if pred_error > 0.05:
            failure_mode = "model_miss"
            research_issue = "Investigate feature drift or regime mismatch"
        elif loss_pct > 0.05:
            failure_mode = "tail_risk_realized"
            research_issue = "Review tail risk limits and position sizing"
        else:
            failure_mode = "execution_slippage"
            research_issue = "Review order slicing and market impact"

        model_decay = pred_error > 0.03

        item = PostmortemItem(
            postmortem_id=pm_id,
            trace_id=trace_id,
            failure_mode=failure_mode,
            loss_amount=abs(realized_return * portfolio_equity),
            predicted_return=predicted_return,
            realized_return=realized_return,
            prediction_error=pred_error,
            research_issue=research_issue,
            model_decay_flagged=model_decay,
            summary=f"{failure_mode}: predicted={predicted_return:.4f}, realized={realized_return:.4f}",
        )
        self._items.append(item)
        return item

    def recent(self, n: int = 20) -> list[dict]:
        return [item.to_dict() for item in self._items[-n:]]

    def count(self) -> int:
        return len(self._items)
