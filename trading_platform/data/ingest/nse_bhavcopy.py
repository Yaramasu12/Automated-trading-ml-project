"""NSE bhavcopy fetch + ingest for the Phase-2 market-data store.

REUSES trading_platform/research/index_premium.py's fetch_zip (which itself
uses the shared fetch_urls UA/Referer/backoff core and the UDiFF-cutover URL
pair) — ZERO Angel One. parse + persist are the shared
trading_platform/data/ingest/bhavcopy.py path.
"""
from __future__ import annotations

from datetime import date

from trading_platform.research.index_premium import fetch_zip
from trading_platform.data.ingest.bhavcopy import (
    parse_udiff_bhavcopy,
    persist_bhavcopy,
)

SOURCE = "nse_fo"
EXCHANGE = "NSE"


def fetch(d: date, retries: int = 3) -> bytes | None:
    """The day's NSE F&O bhavcopy zip, or None on a holiday / no file."""
    return fetch_zip(d, retries=retries)


def ingest_day(db, d: date, *, raw_zip: bytes | None = None,
               source: str = SOURCE) -> dict[str, int] | None:
    """Fetch (unless raw_zip is supplied), parse and persist one day.

    Returns per-table row counts, or None when NSE has no file for the day
    (holiday). Per-day idempotent via the upserts in persist_bhavcopy.
    """
    raw = raw_zip if raw_zip is not None else fetch(d)
    if raw is None:
        return None
    parsed = parse_udiff_bhavcopy(raw, EXCHANGE)
    return persist_bhavcopy(db, parsed, source=source, trade_date=d)
