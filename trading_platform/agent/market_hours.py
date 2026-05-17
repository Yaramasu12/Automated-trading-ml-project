from __future__ import annotations

import zoneinfo
from datetime import date, datetime, time, timedelta

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# ── Equity (NSE/BSE) session ──────────────────────────────────────────────────
_MARKET_OPEN = time(9, 15)
_MARKET_CLOSE = time(15, 30)
_ENTRY_CUTOFF = time(15, 20)   # no new entries after this
_EOD_SQUAREOFF = time(15, 25)  # force-close all positions
_PREMARKET = time(9, 0)

# ── MCX (commodity) session — non-agri: 09:00–23:30 IST ──────────────────────
_MCX_OPEN = time(9, 0)
_MCX_CLOSE = time(23, 30)
_MCX_ENTRY_CUTOFF = time(23, 25)   # no new commodity entries after this
_MCX_EOD_SQUAREOFF = time(23, 25)  # force-close all commodity positions

# NSE holidays 2026
_HOLIDAYS: set[date] = {
    date(2026, 1, 26),  # Republic Day
    date(2026, 3, 25),  # Holi
    date(2026, 4, 3),   # Good Friday
    date(2026, 4, 14),  # Ambedkar Jayanti
    date(2026, 5, 1),   # Maharashtra Day
    date(2026, 8, 15),  # Independence Day
    date(2026, 10, 2),  # Gandhi Jayanti
    date(2026, 11, 4),  # Diwali Laxmi Puja
    date(2026, 12, 25), # Christmas
}


def now_ist() -> datetime:
    return datetime.now(IST)


def is_trading_day(d: date | None = None) -> bool:
    d = d or now_ist().date()
    return d.weekday() < 5 and d not in _HOLIDAYS


def market_status(dt: datetime | None = None) -> str:
    now = dt or now_ist()
    if not is_trading_day(now.date()):
        return "CLOSED"
    t = now.time()
    if t < _PREMARKET:
        return "PRE_MARKET_EARLY"
    if t < _MARKET_OPEN:
        return "PRE_MARKET"
    if t < _ENTRY_CUTOFF:
        return "OPEN"
    if t < _EOD_SQUAREOFF:
        return "ENTRY_CUTOFF"
    if t <= _MARKET_CLOSE:
        return "EOD_SQUAREOFF"
    return "AFTER_HOURS"


def is_market_open(dt: datetime | None = None) -> bool:
    return market_status(dt) == "OPEN"


def is_entry_allowed(dt: datetime | None = None) -> bool:
    return market_status(dt) in ("OPEN",)


def is_eod_squareoff(dt: datetime | None = None) -> bool:
    return market_status(dt) == "EOD_SQUAREOFF"


def is_premarket(dt: datetime | None = None) -> bool:
    return market_status(dt) == "PRE_MARKET"


def seconds_to_next_open(dt: datetime | None = None) -> float:
    now = dt or now_ist()
    nxt = now.replace(hour=_MARKET_OPEN.hour, minute=_MARKET_OPEN.minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    while not is_trading_day(nxt.date()):
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


# ── MCX helpers ───────────────────────────────────────────────────────────────

def is_mcx_entry_allowed(dt: datetime | None = None) -> bool:
    """MCX non-agri commodities: entry allowed 09:00–23:25 IST on trading days.

    The MCX session runs entirely within a single calendar day (never past midnight),
    so no date-rollover logic is needed.  The explicit time range check below is
    sufficient.
    """
    now = dt or now_ist()
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    # Guard: if t is after midnight (00:00–08:59) the IST date has already rolled
    # to the next day.  The session ended at 23:30 on the previous day, so we are
    # no longer in a valid MCX session.
    if t < _MCX_OPEN:
        return False
    return t < _MCX_ENTRY_CUTOFF


def is_mcx_eod_squareoff(dt: datetime | None = None) -> bool:
    """True during the 23:25–23:30 IST window — force-close all commodity positions."""
    now = dt or now_ist()
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return _MCX_EOD_SQUAREOFF <= t <= _MCX_CLOSE


def seconds_to_mcx_open(dt: datetime | None = None) -> float:
    now = dt or now_ist()
    nxt = now.replace(hour=_MCX_OPEN.hour, minute=_MCX_OPEN.minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    while not is_trading_day(nxt.date()):
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()
