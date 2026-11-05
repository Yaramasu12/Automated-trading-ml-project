#!/usr/bin/env python3
"""Backfill the Phase-2 market-data store from the existing historical CSVs.

Replays, idempotently, into the new DB tables:

  * data/historical/*__ONE_DAY*.csv   -> index_spot  (index symbols)
                                         eod_ohlcv    (equity symbols)
                                         + instrument_master
    Resolution is byte-identical to MarketDataStore.daily_closes' CSV
    fallback (deep + pipeline overlay, else parity-spot, else plain), so a
    rewired validator reads the SAME bars from the DB as it did from disk.

  * data/historical/iv/atm_by_expiry.csv      (NSE) -> eod_option_quote
  * data/historical/iv_bse/atm_by_expiry.csv  (BSE)    + instrument_master(OPT)
                                                        + index_atm_iv30
    The per-day ATM rows are the recorded output of research/index_premium.py
    atm_rows(); index_atm_iv30 is rebuilt from them via the SAME iv30_for_day
    the IV30 CSVs were built with, so iv30() matches whether read from DB or
    the <INDEX>__IV30_deep.csv fallback.

Every write is an upsert on the natural key — re-running is safe and
non-destructive. Reports row counts per table.

    python scripts/backfill_market_store.py
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from trading_platform.data.persistence import TradingDatabase  # noqa: E402
from trading_platform.data.market_store import INDEX_SYMBOLS, _exchange_for  # noqa: E402
from trading_platform.data.ingest.bhavcopy import make_instrument_id  # noqa: E402
from trading_platform.research.index_premium import iv30_for_day  # noqa: E402

HIST = REPO / "data" / "historical"


def _load_ohlcv(path: Path) -> dict[date, dict]:
    """{day: {open,high,low,close,volume}} from a history CSV. Rows with an
    unparseable date or non-positive close are skipped (same rule as
    short_vol_backtest.load_daily_closes)."""
    out: dict[date, dict] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                day = datetime.fromisoformat(str(row.get("timestamp") or "").strip()).date()
                close = float(row["close"])
            except (ValueError, KeyError, TypeError):
                continue
            if close <= 0:
                continue
            out[day] = {
                "open": _f(row.get("open")), "high": _f(row.get("high")),
                "low": _f(row.get("low")), "close": close, "volume": _f(row.get("volume")),
            }
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _resolve_ohlcv(symbol: str) -> dict[date, dict]:
    """Full-OHLC analogue of MarketDataStore._daily_closes_csv precedence."""
    deep = HIST / f"{symbol}__ONE_DAY_deep.csv"
    pipe = HIST / f"{symbol}__ONE_DAY_pipeline.csv"
    parity = HIST / f"{symbol}__PARITY_SPOT_deep.csv"
    plain = HIST / f"{symbol}__ONE_DAY.csv"
    if deep.exists():
        bars = _load_ohlcv(deep)
        if pipe.exists():
            bars.update(_load_ohlcv(pipe))
        return bars
    if parity.exists():
        return _load_ohlcv(parity)
    if plain.exists():
        return _load_ohlcv(plain)
    return {}


def _symbols_from_one_day() -> list[str]:
    syms = set()
    for p in HIST.glob("*__ONE_DAY*.csv"):
        syms.add(p.name.split("__ONE_DAY")[0])
    return sorted(syms)


def backfill_bars(db) -> tuple[dict[str, int], dict[tuple[str, date], float]]:
    counts = {"instruments": 0, "index_spot": 0, "eod_ohlcv": 0}
    index_close: dict[tuple[str, date], float] = {}
    for sym in _symbols_from_one_day():
        bars = _resolve_ohlcv(sym)
        if not bars:
            continue
        is_index = sym in INDEX_SYMBOLS
        exchange = _exchange_for(sym)
        with db.bulk_writes():   # one commit per symbol (backfill speedup)
            if is_index:
                iid = make_instrument_id(exchange, "INDEX", sym)
                db.upsert_instrument(instrument_id=iid, exchange=exchange, segment="INDEX",
                                     instrument_type="INDEX", tckr_symbol=sym, underlying=sym,
                                     seen_date=min(bars))
                counts["instruments"] += 1
                for day, b in bars.items():
                    db.upsert_index_spot(trade_date=day, index_symbol=sym, open=b["open"],
                                         high=b["high"], low=b["low"], close=b["close"],
                                         source="csv_backfill")
                    counts["index_spot"] += 1
                    index_close[(sym, day)] = b["close"]
            else:
                iid = make_instrument_id(exchange, "CASH", sym)
                db.upsert_instrument(instrument_id=iid, exchange=exchange, segment="CASH",
                                     instrument_type="EQ", tckr_symbol=sym, seen_date=min(bars))
                counts["instruments"] += 1
                for day, b in bars.items():
                    db.upsert_eod_ohlcv(trade_date=day, instrument_id=iid, open=b["open"],
                                        high=b["high"], low=b["low"], close=b["close"],
                                        volume=b["volume"], source="csv_backfill")
                    counts["eod_ohlcv"] += 1
    return counts, index_close


def backfill_options(db, index_close: dict[tuple[str, date], float]) -> dict[str, int]:
    counts = {"instruments": 0, "eod_option_quote": 0, "index_atm_iv30": 0}
    sources = [(HIST / "iv" / "atm_by_expiry.csv", "NSE", "nse_atm_csv"),
               (HIST / "iv_bse" / "atm_by_expiry.csv", "BSE", "bse_atm_csv")]
    for path, exchange, src in sources:
        if not path.exists():
            continue
        by_day: dict[date, list[dict]] = defaultdict(list)
        with db.bulk_writes(), open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    td = date.fromisoformat(r["date"][:10])
                    exp = date.fromisoformat(r["expiry"][:10])
                    strike = float(r["strike"])
                    und = r["index"]
                except (ValueError, KeyError):
                    continue
                by_day[td].append(r)
                iv = r.get("iv")
                iv_val = float(iv) if iv not in (None, "") else None
                und_px = _f(r.get("undrlyg"))
                lot = _f(r.get("lot"))
                for leg, px_key, vol_key, oi_key in (
                    ("CE", "call", "vol_ce", "oi_ce"),
                    ("PE", "put", "vol_pe", "oi_pe"),
                ):
                    px = _f(r.get(px_key))
                    if px is None:
                        continue
                    iid = make_instrument_id(exchange, "OPT", und, exp, strike, leg)
                    db.upsert_instrument(instrument_id=iid, exchange=exchange, segment="FO",
                                         instrument_type="OPT", tckr_symbol=und, underlying=und,
                                         expiry=exp, strike=strike, option_type=leg,
                                         lot_size=int(lot) if lot else None, seen_date=td)
                    counts["instruments"] += 1
                    db.upsert_eod_option_quote(
                        trade_date=td, instrument_id=iid, underlying=und, expiry=exp,
                        strike=strike, option_type=leg, close=px, settle=px,
                        volume=_f(r.get(vol_key)), oi=_f(r.get(oi_key)), underlying_px=und_px,
                        implied_vol=iv_val, iv_method="bhavcopy_b76_atm", source=src)
                    counts["eod_option_quote"] += 1
        # Rebuild index_atm_iv30 from each day's ATM rows, via the same
        # iv30_for_day the IV30 CSVs were built with.
        with db.bulk_writes():
            for td, rows in by_day.items():
                for idx, (iv30, spot_parity) in iv30_for_day(rows).items():
                    db.upsert_index_atm_iv30(
                        trade_date=td, index_symbol=idx, spot_parity=spot_parity,
                        spot_close=index_close.get((idx, td)), atm_iv30=round(iv30, 4),
                        source=src)
                    counts["index_atm_iv30"] += 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars-only", action="store_true")
    ap.add_argument("--options-only", action="store_true")
    args = ap.parse_args()

    db = TradingDatabase()
    print(f"backfill backend={db.backend} hist={HIST}", flush=True)
    bar_counts, index_close = {"instruments": 0, "index_spot": 0, "eod_ohlcv": 0}, {}
    if not args.options_only:
        bar_counts, index_close = backfill_bars(db)
        print(f"bars: {bar_counts}", flush=True)
    opt_counts = {"instruments": 0, "eod_option_quote": 0, "index_atm_iv30": 0}
    if not args.bars_only:
        if not index_close and not args.options_only:
            pass
        opt_counts = backfill_options(db, index_close)
        print(f"options: {opt_counts}", flush=True)
    total_instruments = bar_counts.get("instruments", 0) + opt_counts.get("instruments", 0)
    print("backfill complete:")
    print(f"  instruments upserted : {total_instruments}")
    print(f"  index_spot rows      : {bar_counts.get('index_spot', 0)}")
    print(f"  eod_ohlcv rows       : {bar_counts.get('eod_ohlcv', 0)}")
    print(f"  eod_option_quote rows: {opt_counts.get('eod_option_quote', 0)}")
    print(f"  index_atm_iv30 rows  : {opt_counts.get('index_atm_iv30', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
