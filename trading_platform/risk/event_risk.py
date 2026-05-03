from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


@dataclass
class EventRiskResult:
    blocked: bool
    reason: str
    nearest_event: str | None
    days_to_event: int | None
    recommended_action: str = "MONITOR"


class EventRiskGuard:
    """Blocks trading around high-impact economic events.

    Uses a hardcoded calendar (from news.calendar.EconomicCalendar).
    Blocks entry orders in the 1-day window before and after each event.
    Exit orders (SL/target/expiry) are never blocked.
    """

    def __init__(self, blocked_dates: set[date] | None = None, buffer_days: int = 1) -> None:
        self.blocked_dates: set[date] = blocked_dates or set()
        self.buffer_days = buffer_days
        self._temporary_blocks: list[tuple[datetime, str, str]] = []

    def load_from_calendar(self, calendar) -> None:
        self.blocked_dates = calendar.blocked_dates()

    def check(self, as_of: date | None = None) -> EventRiskResult:
        today = as_of or datetime.now(timezone.utc).date()
        now = datetime.now(timezone.utc)
        self._temporary_blocks = [
            block for block in self._temporary_blocks if block[0] > now
        ]
        for expires_at, reason, action in self._temporary_blocks:
            if action == "BLOCK_ENTRIES":
                return EventRiskResult(
                    blocked=True,
                    reason=reason,
                    nearest_event=expires_at.isoformat(),
                    days_to_event=0,
                    recommended_action=action,
                )

        for offset in range(-self.buffer_days, self.buffer_days + 1):
            check_date = today + timedelta(days=offset)
            if check_date in self.blocked_dates:
                days_to = (check_date - today).days
                label = check_date.isoformat()
                return EventRiskResult(
                    blocked=True,
                    reason=f"Trading blocked: high-impact event on {label} (offset {days_to:+d}d)",
                    nearest_event=label,
                    days_to_event=days_to,
                    recommended_action="BLOCK_ENTRIES",
                )

        nearest = self._nearest_event(today)
        return EventRiskResult(
            blocked=False,
            reason="No nearby high-impact events",
            nearest_event=nearest.isoformat() if nearest else None,
            days_to_event=(nearest - today).days if nearest else None,
            recommended_action=self._temporary_action(),
        )

    def _nearest_event(self, today: date) -> date | None:
        future = [d for d in self.blocked_dates if d >= today]
        return min(future) if future else None

    def is_blocked(self, as_of: date | None = None) -> bool:
        return self.check(as_of).blocked

    def register_temporary_event(
        self,
        reason: str,
        expires_at: datetime,
        recommended_action: str = "MANUAL_APPROVAL",
    ) -> None:
        """Register a live news/event-risk recommendation.

        Only BLOCK_ENTRIES hard-blocks scheduler entry intents. Other actions
        are exposed in event-risk status so the UI/operator can require manual
        approval or reduce size without blocking emergency exits.
        """
        self._temporary_blocks.append((expires_at, reason, recommended_action))

    def _temporary_action(self) -> str:
        now = datetime.now(timezone.utc)
        active = [action for expires_at, _, action in self._temporary_blocks if expires_at > now]
        if "MANUAL_APPROVAL" in active:
            return "MANUAL_APPROVAL"
        if "REDUCE_SIZE" in active:
            return "REDUCE_SIZE"
        return "MONITOR"
