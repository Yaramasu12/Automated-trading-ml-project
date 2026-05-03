from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class EconomicEvent:
    event_date: date
    name: str
    impact: str
    country: str


_HARDCODED_EVENTS: list[EconomicEvent] = [
    # RBI MPC meetings 2026
    EconomicEvent(date(2026, 2, 7), "RBI MPC Decision", "HIGH", "IN"),
    EconomicEvent(date(2026, 4, 9), "RBI MPC Decision", "HIGH", "IN"),
    EconomicEvent(date(2026, 6, 6), "RBI MPC Decision", "HIGH", "IN"),
    EconomicEvent(date(2026, 8, 8), "RBI MPC Decision", "HIGH", "IN"),
    EconomicEvent(date(2026, 10, 8), "RBI MPC Decision", "HIGH", "IN"),
    EconomicEvent(date(2026, 12, 5), "RBI MPC Decision", "HIGH", "IN"),
    # India Union Budget
    EconomicEvent(date(2026, 2, 1), "Union Budget 2026", "HIGH", "IN"),
    # US FOMC 2026 (approx)
    EconomicEvent(date(2026, 1, 29), "FOMC Decision", "HIGH", "US"),
    EconomicEvent(date(2026, 3, 19), "FOMC Decision", "HIGH", "US"),
    EconomicEvent(date(2026, 5, 7), "FOMC Decision", "HIGH", "US"),
    EconomicEvent(date(2026, 6, 18), "FOMC Decision", "HIGH", "US"),
    EconomicEvent(date(2026, 7, 30), "FOMC Decision", "HIGH", "US"),
    EconomicEvent(date(2026, 9, 17), "FOMC Decision", "HIGH", "US"),
    EconomicEvent(date(2026, 11, 5), "FOMC Decision", "HIGH", "US"),
    EconomicEvent(date(2026, 12, 17), "FOMC Decision", "HIGH", "US"),
    # India CPI/WPI releases (monthly, approx 12th and 14th)
    EconomicEvent(date(2026, 1, 13), "India CPI", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 2, 13), "India CPI", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 3, 13), "India CPI", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 4, 14), "India CPI", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 5, 13), "India CPI", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 6, 12), "India CPI", "MEDIUM", "IN"),
    # India GDP releases
    EconomicEvent(date(2026, 2, 28), "India GDP Q3", "HIGH", "IN"),
    EconomicEvent(date(2026, 5, 29), "India GDP Q4", "HIGH", "IN"),
    EconomicEvent(date(2026, 8, 28), "India GDP Q1", "HIGH", "IN"),
    EconomicEvent(date(2026, 11, 27), "India GDP Q2", "HIGH", "IN"),
    # US NFP (first Friday of month)
    EconomicEvent(date(2026, 1, 9), "US Non-Farm Payrolls", "HIGH", "US"),
    EconomicEvent(date(2026, 2, 6), "US Non-Farm Payrolls", "HIGH", "US"),
    EconomicEvent(date(2026, 3, 6), "US Non-Farm Payrolls", "HIGH", "US"),
    EconomicEvent(date(2026, 4, 3), "US Non-Farm Payrolls", "HIGH", "US"),
    EconomicEvent(date(2026, 5, 1), "US Non-Farm Payrolls", "HIGH", "US"),
    EconomicEvent(date(2026, 6, 5), "US Non-Farm Payrolls", "HIGH", "US"),
    # NSE Derivatives Expiry (last Thursday of month)
    EconomicEvent(date(2026, 1, 29), "NSE Monthly Expiry", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 2, 26), "NSE Monthly Expiry", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 3, 26), "NSE Monthly Expiry", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 4, 30), "NSE Monthly Expiry", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 5, 28), "NSE Monthly Expiry", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 6, 25), "NSE Monthly Expiry", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 7, 30), "NSE Monthly Expiry", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 8, 27), "NSE Monthly Expiry", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 9, 24), "NSE Monthly Expiry", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 10, 29), "NSE Monthly Expiry", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 11, 26), "NSE Monthly Expiry", "MEDIUM", "IN"),
    EconomicEvent(date(2026, 12, 31), "NSE Monthly Expiry", "MEDIUM", "IN"),
]


class EconomicCalendar:
    """Hardcoded high-impact event calendar for India + US markets (2026).

    Replaces a live GDELT/news pipeline for the Phase 9 MVP.
    Refresh annually or integrate a live data source in Phase 10+.
    """

    def __init__(self, extra_events: list[EconomicEvent] | None = None) -> None:
        self._events = list(_HARDCODED_EVENTS)
        if extra_events:
            self._events.extend(extra_events)

    def blocked_dates(self, impact_filter: str | None = "HIGH") -> set[date]:
        filtered = [
            e for e in self._events
            if impact_filter is None or e.impact == impact_filter
        ]
        return {e.event_date for e in filtered}

    def events_on(self, d: date) -> list[EconomicEvent]:
        return [e for e in self._events if e.event_date == d]

    def upcoming(self, from_date: date, days: int = 30) -> list[EconomicEvent]:
        from datetime import timedelta
        end = from_date + timedelta(days=days)
        return sorted(
            [e for e in self._events if from_date <= e.event_date <= end],
            key=lambda e: e.event_date,
        )

    def all_events(self) -> list[dict]:
        return [
            {
                "date": e.event_date.isoformat(),
                "name": e.name,
                "impact": e.impact,
                "country": e.country,
            }
            for e in sorted(self._events, key=lambda e: e.event_date)
        ]
