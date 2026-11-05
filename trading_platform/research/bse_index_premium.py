"""Per-index implied volatility from BSE's FREE daily F&O (derivatives) UDiFF
bhavcopy — the BSE analogue of research/index_premium.py for the SENSEX and
BANKEX index-option chains.

Why this exists: the platform already names SENSEX/BANKEX for the edge gate
(risk/edge_gate.py, data/instrument_master.py) but had ZERO historical IV /
premium data for them, so they could never validate or produce a signal. BSE
publishes the settlement price of every derivative contract each day, from
which each index's own ATM implied vol can be recovered exactly — the same
method index_premium.py already uses for the NSE indices.

Two things differ from NSE; everything downstream is reused unchanged:

  1. Transport. BSE serves the F&O UDiFF bhavcopy as a BARE CSV (not a zip,
     unlike NSE) at the download path below, behind the same kind of bot
     filter (needs a browser User-Agent + a bseindia.com Referer). Verified
     live 2026-11-05 for every trading day back to the 2024-07-08 UDiFF
     cutover; a holiday / not-yet-published day returns BSE's HTML error page
     with HTTP 200 (not a 404), which fetch_bse_zip detects and treats as "no
     file". fetch_bse_zip wraps the CSV in an in-memory zip so the shared
     parse_index_options (which reads a zip) works byte-for-byte.

  2. Column schema is IDENTICAL to NSE's F&O UDiFF (TckrSymb, XpryDt,
     StrkPric, OptnTp, SttlmPric, UndrlygPric, TtlTradgVol, OpnIntrst,
     NewBrdLotQty; index options carry FinInstrmTp=IDO, matched by the shared
     parser's ",IDO," line filter), so parse_index_options / atm_rows /
     implied_vol / iv30_for_day / compute_reading are reused with no change —
     only the index set (SENSEX, BANKEX) is passed in.

Verified live 2026-11-05: ATM IV recovers to a sane ~13-17% band for both
SENSEX and BANKEX. REPORTING ONLY, exactly like index_premium.py — nothing
here is wired into any order-submission or entry-decision path.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date

from trading_platform.research.index_premium import fetch_urls, parse_index_options

BSE_INDICES = ("SENSEX", "BANKEX")
_BSE_REFERER = "https://www.bseindia.com/"

# BSE F&O (derivatives) UDiFF bhavcopy — a bare CSV, verified live 2026-11-05:
#   https://www.bseindia.com/download/Bhavcopy/Derivative/
#     BhavCopy_BSE_FO_0_0_0_<YYYYMMDD>_F_0000.CSV
# No pre-UDiFF per-contract index-option settlement history exists on BSE in a
# comparable format (the old BSE_EQD_BHAVCOPY_*.csv was discontinued on the
# 2024-07-08 UDiFF cutover and did not carry it), so there is no legacy URL to
# chain as a fallback — unlike the NSE builder's old/new pair.
_CSV_URL = ("https://www.bseindia.com/download/Bhavcopy/Derivative/"
            "BhavCopy_BSE_FO_0_0_0_{d:%Y%m%d}_F_0000.CSV")


def bse_urls_for(d: date) -> list[str]:
    """Candidate download URLs for the day's BSE F&O UDiFF bhavcopy."""
    return [_CSV_URL.format(d=d)]


def fetch_bse_zip(d: date, retries: int = 3) -> bytes | None:
    """Bytes of an in-memory zip wrapping the day's BSE F&O bhavcopy CSV, or
    None when BSE has no file for the day (holiday / not yet published).

    The CSV is wrapped in a zip so the shared parse_index_options — which
    reads a zip exactly as the NSE pipeline does — is reused unchanged.
    """
    raw = fetch_urls(bse_urls_for(d), _BSE_REFERER, retries)
    if raw is None:
        return None
    # A holiday / not-yet-published day returns BSE's HTML error page with
    # HTTP 200 (not a 404). The real file is a UDiFF CSV whose first column is
    # always "TradDt"; anything else is the error page — treat it as no file
    # rather than persisting HTML as if it were data.
    if not raw.lstrip()[:6] == b"TradDt":
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"BhavCopy_BSE_FO_{d:%Y%m%d}.csv", raw)
    return buf.getvalue()


def parse_bse_index_options(raw_zip: bytes) -> list[dict]:
    """SENSEX / BANKEX index-option rows, via the shared NSE-format parser."""
    return parse_index_options(raw_zip, indices=BSE_INDICES)
