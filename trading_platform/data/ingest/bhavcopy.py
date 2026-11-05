"""UDiFF bhavcopy parsing + persistence into the Phase-2 market-data tables.

NSE and BSE both publish the daily F&O (and NSE the cash) bhavcopy in the
IDENTICAL UDiFF column schema (TckrSymb, XpryDt, StrkPric, OptnTp, SttlmPric,
ClsPric, UndrlygPric, TtlTradgVol, OpnIntrst, ChngInOpnIntrst, NewBrdLotQty,
FinInstrmTp, Sgmt, …) — the same schema research/index_premium.py already
reads for index options. This module generalises that to EVERY row and
classifies each into:

  * OPTION  (OptnTp in CE/PE)           -> eod_option_quote + instrument(OPT)
  * FUTURE  (has an expiry, no OptnTp)  -> eod_ohlcv        + instrument(FUT)
  * CASH    (no expiry)                 -> eod_ohlcv        + instrument(EQ)

Raw traded prices (open/high/low/close/settle) are stored VERBATIM. The only
modeled quantity written is the per-index 30-day ATM implied vol, computed by
reusing research/index_premium.py's atm_rows + iv30_for_day UNCHANGED, written
to the labeled index_atm_iv30.atm_iv30 column — never conflated with a price.

Point-in-time correct: only the day's own rows are used; no lookahead.
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime

from trading_platform.research.index_premium import (
    INDICES,
    atm_rows,
    iv30_for_day,
)
from trading_platform.research.bse_index_premium import BSE_INDICES

# Every index whose own-chain ATM IV we recover from the day's option rows.
_ALL_INDICES = tuple(sorted(set(INDICES) | set(BSE_INDICES)))
_IV_METHOD = "bhavcopy_b76_atm"


# ─── instrument id ──────────────────────────────────────────────────────────

def _fmt_strike(strike: float | None) -> str:
    if strike is None:
        return ""
    # 23350.0 and 23350 must map to one id; keep a half-paise strike distinct.
    return f"{strike:.4f}".rstrip("0").rstrip(".")


def make_instrument_id(
    exchange: str,
    instrument_type: str,
    tckr_symbol: str,
    expiry: date | None = None,
    strike: float | None = None,
    option_type: str | None = None,
) -> str:
    """Deterministic id so re-ingesting the same contract upserts the same
    row (idempotent). Encodes the full option/future identity; cash & index
    collapse to exchange:type:symbol."""
    it = instrument_type.upper()
    if it == "OPT":
        exp = expiry.strftime("%Y%m%d") if expiry else ""
        return f"{exchange}:OPT:{tckr_symbol}:{exp}:{_fmt_strike(strike)}:{(option_type or '').upper()}"
    if it == "FUT":
        exp = expiry.strftime("%Y%m%d") if expiry else ""
        return f"{exchange}:FUT:{tckr_symbol}:{exp}"
    if it == "INDEX":
        return f"{exchange}:INDEX:{tckr_symbol}"
    return f"{exchange}:CASH:{tckr_symbol}"


# ─── parsing ─────────────────────────────────────────────────────────────────

@dataclass
class OhlcvRecord:
    instrument_id: str
    exchange: str
    segment: str
    instrument_type: str
    tckr_symbol: str
    expiry: date | None
    lot_size: int | None
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    settle: float | None
    volume: float | None
    oi: float | None
    underlying: str | None = None


@dataclass
class OptionQuoteRecord:
    instrument_id: str
    exchange: str
    tckr_symbol: str
    underlying: str
    expiry: date | None
    strike: float | None
    option_type: str
    lot_size: int | None
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    settle: float | None
    volume: float | None
    oi: float | None
    change_oi: float | None
    underlying_px: float | None


@dataclass
class ParsedBhavcopy:
    trade_date: date | None
    exchange: str
    ohlcv: list[OhlcvRecord] = field(default_factory=list)
    options: list[OptionQuoteRecord] = field(default_factory=list)
    # index-option dicts in atm_rows() input shape, for iv30 recovery
    index_option_rows: list[dict] = field(default_factory=list)


def _f(v) -> float | None:
    try:
        x = float(str(v).strip())
        return x
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    x = _f(v)
    return int(x) if x is not None else None


def _parse_expiry(v: str) -> date | None:
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%Y %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def parse_udiff_bhavcopy(raw_zip: bytes, exchange: str) -> ParsedBhavcopy:
    """Parse a UDiFF bhavcopy zip into classified records. Unparseable rows
    are skipped (never crash a whole day's ingest on one bad line)."""
    zf = zipfile.ZipFile(io.BytesIO(raw_zip))
    text = zf.read(zf.namelist()[0]).decode("utf-8", "replace")
    out = ParsedBhavcopy(trade_date=None, exchange=exchange)
    rd = csv.DictReader(io.StringIO(text))
    if rd.fieldnames is None or "TckrSymb" not in rd.fieldnames:
        return out  # not a UDiFF file (e.g. legacy format) — nothing to do here

    for r in rd:
        try:
            tckr = (r.get("TckrSymb") or "").strip()
            if not tckr:
                continue
            if out.trade_date is None:
                out.trade_date = _parse_expiry(r.get("TradDt") or r.get("BizDt") or "")
            ot = (r.get("OptnTp") or "").strip().upper()
            expiry = _parse_expiry(r.get("XpryDt") or "")
            strike = _f(r.get("StrkPric"))
            o, h, l, c = (_f(r.get("OpnPric")), _f(r.get("HghPric")),
                          _f(r.get("LwPric")), _f(r.get("ClsPric")))
            settle = _f(r.get("SttlmPric"))
            vol = _f(r.get("TtlTradgVol"))
            oi = _f(r.get("OpnIntrst"))
            chg_oi = _f(r.get("ChngInOpnIntrst"))
            und_px = _f(r.get("UndrlygPric"))
            lot = _i(r.get("NewBrdLotQty"))

            if ot in ("CE", "PE") and strike is not None:
                iid = make_instrument_id(exchange, "OPT", tckr, expiry, strike, ot)
                out.options.append(OptionQuoteRecord(
                    instrument_id=iid, exchange=exchange, tckr_symbol=tckr,
                    underlying=tckr, expiry=expiry, strike=strike, option_type=ot,
                    lot_size=lot, open=o, high=h, low=l, close=c, settle=settle,
                    volume=vol, oi=oi, change_oi=chg_oi, underlying_px=und_px,
                ))
                # Feed the shared ATM-IV recovery only for indices, with a
                # positive settlement price, exactly like parse_index_options.
                px = settle if (settle and settle > 0) else None
                if tckr in _ALL_INDICES and expiry is not None and px:
                    out.index_option_rows.append(dict(
                        sym=tckr, exp=expiry, k=strike, ot=ot, px=px,
                        vol=vol or 0.0, oi=oi or 0.0, lot=lot or 0, und=und_px or 0.0,
                    ))
            elif expiry is not None:
                iid = make_instrument_id(exchange, "FUT", tckr, expiry)
                out.ohlcv.append(OhlcvRecord(
                    instrument_id=iid, exchange=exchange, segment="FO",
                    instrument_type="FUT", tckr_symbol=tckr, expiry=expiry,
                    lot_size=lot, open=o, high=h, low=l, close=c, settle=settle,
                    volume=vol, oi=oi, underlying=tckr,
                ))
            else:
                iid = make_instrument_id(exchange, "CASH", tckr)
                out.ohlcv.append(OhlcvRecord(
                    instrument_id=iid, exchange=exchange, segment="CASH",
                    instrument_type="EQ", tckr_symbol=tckr, expiry=None,
                    lot_size=lot, open=o, high=h, low=l, close=c, settle=settle,
                    volume=vol, oi=oi, underlying=None,
                ))
        except Exception:
            continue
    return out


# ─── persistence ───────────────────────────────────────────────────────────

def persist_bhavcopy(db, parsed: ParsedBhavcopy, *, source: str,
                     trade_date: date | None = None) -> dict[str, int]:
    """Idempotently upsert a parsed bhavcopy into the Phase-2 tables. Returns
    per-table row counts. Re-running the same day overwrites in place (every
    write is an upsert on the natural key), so it is safe to resume/backfill."""
    td = trade_date or parsed.trade_date
    if td is None:
        return {"instruments": 0, "eod_ohlcv": 0, "eod_option_quote": 0, "index_atm_iv30": 0}
    counts = {"instruments": 0, "eod_ohlcv": 0, "eod_option_quote": 0, "index_atm_iv30": 0}

    for rec in parsed.ohlcv:
        db.upsert_instrument(
            instrument_id=rec.instrument_id, exchange=rec.exchange, segment=rec.segment,
            instrument_type=rec.instrument_type, tckr_symbol=rec.tckr_symbol,
            underlying=rec.underlying, expiry=rec.expiry, lot_size=rec.lot_size,
            seen_date=td,
        )
        counts["instruments"] += 1
        db.upsert_eod_ohlcv(
            trade_date=td, instrument_id=rec.instrument_id, open=rec.open, high=rec.high,
            low=rec.low, close=rec.close, settle=rec.settle, volume=rec.volume, oi=rec.oi,
            source=source,
        )
        counts["eod_ohlcv"] += 1

    for q in parsed.options:
        db.upsert_instrument(
            instrument_id=q.instrument_id, exchange=q.exchange, segment="FO",
            instrument_type="OPT", tckr_symbol=q.tckr_symbol, underlying=q.underlying,
            expiry=q.expiry, strike=q.strike, option_type=q.option_type,
            lot_size=q.lot_size, seen_date=td,
        )
        counts["instruments"] += 1
        db.upsert_eod_option_quote(
            trade_date=td, instrument_id=q.instrument_id, underlying=q.underlying,
            expiry=q.expiry, strike=q.strike, option_type=q.option_type,
            open=q.open, high=q.high, low=q.low, close=q.close, settle=q.settle,
            volume=q.volume, oi=q.oi, change_oi=q.change_oi, underlying_px=q.underlying_px,
            # implied_vol stays NULL: these are raw traded quotes. Modeled IV
            # lives only in index_atm_iv30, so a price is never conflated with it.
            implied_vol=None, iv_method=None, source=source,
        )
        counts["eod_option_quote"] += 1

    # Modeled per-index 30-day ATM IV, via the SAME research pipeline the live
    # premium monitor and the historical IV builder use (one implementation).
    if parsed.index_option_rows:
        rows = atm_rows(td, parsed.index_option_rows)
        for idx, (iv30, spot_parity) in iv30_for_day(rows).items():
            db.upsert_index_atm_iv30(
                trade_date=td, index_symbol=idx, spot_parity=spot_parity,
                atm_iv30=iv30, source=source,
            )
            counts["index_atm_iv30"] += 1

    return counts
