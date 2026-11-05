"""BSE bhavcopy fetch + ingest for the Phase-2 market-data store.

REUSES trading_platform/research/bse_index_premium.py's fetch_bse_zip (bare
CSV wrapped into a zip, HTML-error-page rejection, shared UA/Referer/backoff)
— ZERO Angel One. parse + persist are the shared
trading_platform/data/ingest/bhavcopy.py path; BSE's UDiFF schema is identical
to NSE's, so the same parser reads it unchanged.
"""
from __future__ import annotations

from datetime import date

from trading_platform.research.bse_index_premium import fetch_bse_zip
from trading_platform.data.ingest.bhavcopy import (
    parse_udiff_bhavcopy,
    persist_bhavcopy,
)

SOURCE = "bse_fo"
EXCHANGE = "BSE"


def fetch(d: date, retries: int = 3) -> bytes | None:
    """The day's BSE F&O bhavcopy wrapped in a zip, or None on a holiday /
    no file / HTML error page."""
    return fetch_bse_zip(d, retries=retries)


def ingest_day(db, d: date, *, raw_zip: bytes | None = None,
               source: str = SOURCE) -> dict[str, int] | None:
    """Fetch (unless raw_zip is supplied), parse and persist one day.

    Returns per-table row counts, or None when BSE has no file for the day.
    Per-day idempotent via the upserts in persist_bhavcopy.
    """
    raw = raw_zip if raw_zip is not None else fetch(d)
    if raw is None:
        return None
    parsed = parse_udiff_bhavcopy(raw, EXCHANGE)
    return persist_bhavcopy(db, parsed, source=source, trade_date=d)
