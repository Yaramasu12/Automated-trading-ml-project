"""MCX commodity-futures bhavcopy fetch + ingest for the Phase-2/3 market-data
store. ZERO Angel One — same decoupling principle as the NSE/BSE ingesters.

SOURCE (confirmed empirically, 2026-11-05):
  MCX publishes its EOD bhavcopy through the ASP.NET web-method
    POST https://www.mcxindia.com/backpage.aspx/GetDateWiseBhavCopy
    body: {"Date": "YYYYMMDD", "InstrumentName": "ALL"}
    response: {"d": {"Data": [ {row}, ... ]}}, each row a contract with columns
      Date, Symbol, InstrumentName, ExpiryDate, StrikePrice, OptionType,
      Open, High, Low, Close, PreviousClose, Volume, Value, OpenInterest
    InstrumentName in {FUTCOM, FUTIDX, OPTCOM, OPTFUT, OPTIDX}; FUTCOM = the
    commodity futures this module persists. Dates come back as ASP.NET
    "/Date(ms)/" epoch-millis strings. This schema is corroborated by two
    independently-maintained open-source clients (RuchiTanmay/mcxlib and
    NikhilSuthar/indian-market-data).

BLOCKER (reported, not stubbed-over): MCX fronts the site with an Akamai WAF.
A plain GET of the bhavcopy page returns HTTP 403; a plain web-method POST on
the repo's standard urllib stack returns an Akamai block page (HTTP 200 with a
404-styled HTML body, `<!--SID : DCxx-->`), NOT the JSON. Retrieving the real
payload requires Chrome TLS-fingerprint impersonation (e.g. curl-cffi), which
NikhilSuthar/indian-market-data documents as the only way past MCX's WAF. The
fetch below is wired to the confirmed real endpoint but, with the stdlib stack,
MCX will serve the block page — so `fetch` RAISES a clear MCXBlockedError
rather than silently returning fabricated data. Parsing + persistence are the
verified-schema path and are exercised by tests against a fixture built to the
exact MCX column schema above. To populate the store in production, supply the
real JSON via `raw_json=` from a TLS-impersonating client at the edge; the
parse/persist path is unchanged.

Classification: every FUTCOM row -> instrument_master(segment='FUT',
instrument_type='COMMODITY_FUT') + eod_ohlcv(source='mcx_bhavcopy'), reusing
the Phase-2 OhlcvRecord / persist_bhavcopy path unchanged. MCX bhavcopy exposes
no separate settlement column; for a futures contract the daily Close IS the
settlement price, so settle := Close (documented; never modeled).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

from trading_platform.data.ingest.bhavcopy import (
    OhlcvRecord,
    ParsedBhavcopy,
    make_instrument_id,
    persist_bhavcopy,
)

SOURCE = "mcx_bhavcopy"
EXCHANGE = "MCX"
_BHAV_URL = "https://www.mcxindia.com/backpage.aspx/GetDateWiseBhavCopy"
# The commodity-futures instrument code; the objective is commodity FUTURES
# only (not FUTIDX/OPT*), so we persist exactly these rows.
_COMMODITY_FUT_CODE = "FUTCOM"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_DATE_MS = re.compile(r"/Date\((-?\d+)(?:[+-]\d+)?\)/")


class MCXBlockedError(RuntimeError):
    """MCX's Akamai WAF served a block page instead of the bhavcopy JSON."""


def _f(v):
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _i(v):
    x = _f(v)
    return int(x) if x is not None else None


def _mcx_date(v) -> date | None:
    """Parse an MCX date: ASP.NET /Date(ms)/, ISO, or dd-mon/dd-mm forms."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = _DATE_MS.search(s)
    if m:
        # epoch millis UTC -> calendar date (MCX stamps midnight-ish IST).
        return datetime.fromtimestamp(int(m.group(1)) / 1000.0, tz=timezone.utc).date()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d%b%Y", "%Y%m%d",
                "%d-%b-%y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _extract_rows(payload) -> list[dict]:
    """Normalise the many shapes MCX / a caller may hand us into a row list:
    bytes/str JSON, the full {"d": {"Data": [...]}} envelope (where .d may be a
    JSON-encoded string), a {"Data": [...]} dict, or a bare list of row dicts."""
    obj = payload
    if isinstance(obj, (bytes, bytearray)):
        obj = obj.decode("utf-8", "replace")
    if isinstance(obj, str):
        obj = json.loads(obj)
    if isinstance(obj, dict) and "d" in obj:
        d = obj["d"]
        if isinstance(d, str):
            d = json.loads(d)
        obj = d
    if isinstance(obj, dict) and "Data" in obj:
        obj = obj["Data"]
    if isinstance(obj, dict):  # single row
        obj = [obj]
    return [r for r in (obj or []) if isinstance(r, dict)]


def parse_mcx_bhavcopy(payload, *, only=(_COMMODITY_FUT_CODE,)) -> ParsedBhavcopy:
    """Parse an MCX bhavcopy payload into FUTCOM OhlcvRecords. Non-FUTCOM rows
    (options / index futures) and rows with a non-positive Close are skipped.
    Unparseable rows never crash the day's ingest."""
    rows = _extract_rows(payload)
    out = ParsedBhavcopy(trade_date=None, exchange=EXCHANGE)
    want = {str(x).upper() for x in only}
    for r in rows:
        try:
            instr = (r.get("InstrumentName") or r.get("Instrument") or "").strip().upper()
            if instr not in want:
                continue
            sym = (r.get("Symbol") or r.get("Commodity") or "").strip().upper()
            if not sym:
                continue
            if out.trade_date is None:
                out.trade_date = _mcx_date(r.get("Date") or r.get("TradeDate"))
            expiry = _mcx_date(r.get("ExpiryDate") or r.get("Expiry"))
            close = _f(r.get("Close"))
            if close is None or close <= 0:
                continue  # a futures row with no positive close/settle is unusable
            o, h, l = _f(r.get("Open")), _f(r.get("High")), _f(r.get("Low"))
            vol = _f(r.get("Volume"))
            oi = _f(r.get("OpenInterest") or r.get("OpenInterest ") or r.get("OI"))
            iid = make_instrument_id(EXCHANGE, "FUT", sym, expiry)
            out.ohlcv.append(OhlcvRecord(
                instrument_id=iid, exchange=EXCHANGE, segment="FUT",
                instrument_type="COMMODITY_FUT", tckr_symbol=sym, expiry=expiry,
                lot_size=None, open=o, high=h, low=l, close=close,
                # MCX publishes no separate settle field for futures; the daily
                # Close IS the contract settlement price (documented, raw).
                settle=close, volume=vol, oi=oi, underlying=sym,
            ))
        except Exception:
            continue
    return out


def fetch(d: date, retries: int = 3) -> dict | None:
    """The day's MCX bhavcopy JSON, or None on a holiday / no data.

    Wired to the confirmed real web-method. NOTE: on the repo's stdlib HTTP
    stack MCX's Akamai WAF returns a block page, not JSON — this raises
    MCXBlockedError in that case. In production, pass the JSON via
    ingest_day(raw_json=...) from a TLS-impersonating fetcher at the edge."""
    body = json.dumps({"Date": d.strftime("%Y%m%d"), "InstrumentName": "ALL"}).encode()
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.mcxindia.com",
        "Referer": "https://www.mcxindia.com/market-data/bhavcopy",
    }
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(_BHAV_URL, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
            if "json" not in ctype.lower() or raw[:1] not in (b"{", b"["):
                raise MCXBlockedError(
                    "MCX returned a non-JSON response (Akamai WAF block page); "
                    "a TLS-impersonating client is required — see module docstring.")
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None  # holiday / no file
            last_exc = exc
            time.sleep(3 * (attempt + 1))
        except MCXBlockedError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    raise MCXBlockedError(f"MCX bhavcopy fetch failed for {d}: {last_exc}")


def ingest_day(db, d: date, *, raw_json=None, source: str = SOURCE) -> dict[str, int] | None:
    """Fetch (unless raw_json is supplied), parse and persist one MCX day.

    Returns per-table row counts, or None when MCX has no data for the day.
    Per-day idempotent via the upserts in persist_bhavcopy (shared Phase-2
    path). Only FUTCOM commodity futures are persisted."""
    raw = raw_json if raw_json is not None else fetch(d)
    if raw is None:
        return None
    parsed = parse_mcx_bhavcopy(raw)
    return persist_bhavcopy(db, parsed, source=source, trade_date=d)
