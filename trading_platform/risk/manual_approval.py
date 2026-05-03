from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from trading_platform.domain.enums import ApprovalState
from trading_platform.domain.models import OrderIntent

logger = logging.getLogger(__name__)

_DEFAULT_EXPIRY_SECONDS = 300


@dataclass
class ApprovalRequest:
    request_id: str = field(default_factory=lambda: uuid4().hex)
    intent: OrderIntent | None = None
    state: ApprovalState = ApprovalState.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc) + timedelta(seconds=_DEFAULT_EXPIRY_SECONDS))
    reviewed_at: datetime | None = None
    reviewer_note: str = ""

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "reviewer_note": self.reviewer_note,
            "symbol": self.intent.instrument.symbol if self.intent else None,
            "side": self.intent.signal.side.value if self.intent else None,
            "quantity": self.intent.quantity if self.intent else None,
            "strategy_name": self.intent.signal.strategy_name if self.intent else None,
        }


class ManualApprovalGate:
    """Holds high-risk OrderIntents in a pending queue until a human approves or rejects.

    Configured notional threshold: intents above the threshold require approval.
    Expired pending requests are auto-rejected on next sweep.
    """

    def __init__(self, approval_threshold_notional: float = 500_000.0) -> None:
        self.approval_threshold_notional = approval_threshold_notional
        self._pending: dict[str, ApprovalRequest] = {}

    def requires_approval(self, intent: OrderIntent) -> bool:
        return intent.notional_value >= self.approval_threshold_notional

    def submit(self, intent: OrderIntent, expiry_seconds: int = _DEFAULT_EXPIRY_SECONDS) -> ApprovalRequest:
        req = ApprovalRequest(
            intent=intent,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expiry_seconds),
        )
        self._pending[req.request_id] = req
        logger.info("ApprovalGate: request %s submitted for %s notional=%.0f",
                    req.request_id, intent.instrument.symbol, intent.notional_value)
        return req

    def approve(self, request_id: str, note: str = "") -> ApprovalRequest:
        req = self._pending.get(request_id)
        if req is None:
            raise KeyError(f"Approval request {request_id} not found")
        self._expire_stale()
        if req.is_expired:
            req.state = ApprovalState.EXPIRED
            raise ValueError(f"Request {request_id} has expired")
        req.state = ApprovalState.APPROVED
        req.reviewed_at = datetime.now(timezone.utc)
        req.reviewer_note = note
        self._pending.pop(request_id, None)
        logger.info("ApprovalGate: request %s APPROVED — %s", request_id, note)
        return req

    def reject(self, request_id: str, note: str = "") -> ApprovalRequest:
        req = self._pending.get(request_id)
        if req is None:
            raise KeyError(f"Approval request {request_id} not found")
        req.state = ApprovalState.REJECTED
        req.reviewed_at = datetime.now(timezone.utc)
        req.reviewer_note = note
        self._pending.pop(request_id, None)
        logger.info("ApprovalGate: request %s REJECTED — %s", request_id, note)
        return req

    def _expire_stale(self) -> list[str]:
        expired = [rid for rid, req in self._pending.items() if req.is_expired]
        for rid in expired:
            self._pending[rid].state = ApprovalState.EXPIRED
            self._pending.pop(rid)
            logger.warning("ApprovalGate: request %s auto-expired", rid)
        return expired

    def pending_requests(self) -> list[dict]:
        self._expire_stale()
        return [req.to_dict() for req in self._pending.values()]

    @property
    def pending_count(self) -> int:
        self._expire_stale()
        return len(self._pending)
