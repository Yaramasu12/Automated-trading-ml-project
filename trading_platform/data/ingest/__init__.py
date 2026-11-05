"""Phase-2 market-data ingestion.

DB-backed EOD ingestion from the FREE daily NSE / BSE bhavcopy — ZERO Angel
One, which is the whole point of decoupling validation/retraining from the
live feed. The fetchers REUSE the UA/Referer/backoff HTTP core and the
UDiFF parsing already proven in trading_platform/research/index_premium.py
and bse_index_premium.py; this package only adds the full cash + F&O row
classification and the per-day idempotent writes into the Phase-2 tables
(instrument_master / eod_ohlcv / eod_option_quote / index_atm_iv30).

Nothing here touches index_vol_premium or any live-order path.
"""
from __future__ import annotations

from trading_platform.data.ingest.bhavcopy import (
    ParsedBhavcopy,
    make_instrument_id,
    parse_udiff_bhavcopy,
    persist_bhavcopy,
)

__all__ = [
    "ParsedBhavcopy",
    "make_instrument_id",
    "parse_udiff_bhavcopy",
    "persist_bhavcopy",
]
