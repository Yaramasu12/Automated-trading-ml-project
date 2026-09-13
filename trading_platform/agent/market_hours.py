from __future__ import annotations

import logging
import zoneinfo
from datetime import date, datetime, time, timedelta

logger = logging.getLogger(__name__)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# ── Equity (NSE/BSE) session ──────────────────────────────────────────────────
_MARKET_OPEN = time(9, 15)
_MARKET_CLOSE = time(15, 30)
_ENTRY_CUTOFF = time(15, 20)   # no new entries after this
_EOD_SQUAREOFF = time(15, 25)  # force-close all positions
_PREMARKET = time(9, 0)
# Options-chain EOD snapshot window: quotes are still live and liquid, but
# strictly before the entry cutoff/EOD square-off so chain capture can never
# compete with real order flow for the rate-limited candle API.
_CHAIN_SNAPSHOT_START = time(15, 10)
_CHAIN_SNAPSHOT_END = time(15, 20)

# ── MCX (commodity) session — non-agri: 09:00–23:30 IST ──────────────────────
_MCX_OPEN = time(9, 0)
_MCX_CLOSE = time(23, 30)
_MCX_ENTRY_CUTOFF = time(23, 25)   # no new commodity entries after this
_MCX_EOD_SQUAREOFF = time(23, 25)  # force-close all commodity positions

# NSE holidays 2026 + 2027
#
# audit_results/2026-09-13/AUDIT_REPORT.md finding #8 (high): the 2026 list
# below was wrong on multiple dates and missing several holidays entirely —
# most urgently, 14 September 2026 (Ganesh Chaturthi) was classified as a
# trading day, and this app runs unattended. Re-verified 2026-09-13 against
# NSE's published circular via two independent secondary sources that agree
# exactly on all 15 dates (nseindia.com's own circular page timed out on
# fetch; re-verify directly against
# https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf and
# https://www.nseindia.com/resources/exchange-communication-holidays when
# next reachable):
#   https://groww.in/p/nse-holidays
#   https://www.smallcase.com/learn/nse-holidays-2026/
# Corrected: Holi was 25 March (actually 3 March — a real trading day was
# being closed while the actual holiday was being traded). "Diwali Laxmi
# Puja" on 4 November was both the wrong date and the wrong holiday: 8
# November 2026 is a Sunday with a special Muhurat trading SESSION (not a
# closure) for Laxmi Pujan; the actual observed closure is "Diwali-
# Balipratipada" on 10 November. Added: Ram Navami, Mahavir Jayanti, Bakri
# Id, Muharram, Ganesh Chaturthi, Dussehra, Guru Nanak Jayanti — all
# present on both source calendars, all previously missing. Independence
# Day (15 Aug 2026) falls on a Saturday and so is absent from both sources'
# "15 weekday closures" lists — kept here anyway since is_trading_day()
# already excludes weekends separately, so it's redundant, not wrong.
_HOLIDAYS: set[date] = {
    # 2026
    date(2026, 1, 26),  # Republic Day
    date(2026, 3, 3),   # Holi
    date(2026, 3, 26),  # Ram Navami
    date(2026, 3, 31),  # Mahavir Jayanti
    date(2026, 4, 3),   # Good Friday
    date(2026, 4, 14),  # Ambedkar Jayanti
    date(2026, 5, 1),   # Maharashtra Day
    date(2026, 5, 28),  # Bakri Id
    date(2026, 6, 26),  # Muharram
    date(2026, 8, 15),  # Independence Day (Saturday — redundant, see above)
    date(2026, 9, 14),  # Ganesh Chaturthi
    date(2026, 10, 2),  # Gandhi Jayanti
    date(2026, 10, 20), # Dussehra
    date(2026, 11, 10), # Diwali-Balipratipada
    date(2026, 11, 24), # Guru Nanak Jayanti
    date(2026, 12, 25), # Christmas
    # 2027 — NSE had not published its 2027 circular as of this correction
    # (2026-09-13); these are the SAME entries as before, still tentative
    # for exactly the reason the original comment gave. Do not trust them
    # the way the corrected, source-cited 2026 list above can be trusted —
    # re-verify against NSE's own circular once published, the same way
    # the 2026 errors above were found.
    date(2027, 1, 26),  # Republic Day
    date(2027, 3, 15),  # Holi (tentative — verify NSE circular)
    date(2027, 3, 26),  # Good Friday (tentative — verify NSE circular)
    date(2027, 4, 14),  # Ambedkar Jayanti / Dr. Ambedkar Jayanti
    date(2027, 5, 1),   # Maharashtra Day
    date(2027, 8, 15),  # Independence Day
    date(2027, 10, 2),  # Gandhi Jayanti
    date(2027, 10, 20), # Diwali Laxmi Puja (tentative — verify NSE circular)
    date(2027, 12, 25), # Christmas
}

_MAX_HOLIDAY_YEAR = max(d.year for d in _HOLIDAYS)


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    """Normalise a timestamp to IST for market-hours comparisons.

    Aware timestamps are converted; naive ones are assumed to be IST already, which
    is the convention the rest of this module follows (``market_status`` reads
    ``dt.time()`` directly). Use this whenever a caller-supplied timestamp is compared
    against an IST clock time — the runtime passes UTC (``datetime.now(timezone.utc)``)
    while tests and backtests pass naive IST, so ``.hour`` is only meaningful after
    normalising.
    """
    return dt.astimezone(IST) if dt.tzinfo is not None else dt


def is_trading_day(d: date | None = None) -> bool:
    d = d or now_ist().date()
    if d.year > _MAX_HOLIDAY_YEAR:
        logger.warning(
            "market_hours: no holiday data for %d — update _HOLIDAYS in market_hours.py",
            d.year,
        )
    return d.weekday() < 5 and d not in _HOLIDAYS


def market_status(dt: datetime | None = None) -> str:
    now = to_ist(dt) if dt is not None else now_ist()
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


def is_chain_snapshot_window(dt: datetime | None = None) -> bool:
    """True during the 15:10-15:20 IST window — options quotes still live and
    liquid, strictly before the entry cutoff/EOD square-off."""
    now = dt or now_ist()
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return _CHAIN_SNAPSHOT_START <= t < _CHAIN_SNAPSHOT_END


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
