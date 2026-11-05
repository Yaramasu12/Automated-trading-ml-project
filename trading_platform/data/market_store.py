"""MarketDataStore — the single read API over the Phase-2 market-data store.

One place validation/retraining read daily bars, modeled index IV, and option
quotes from, replacing scattered CSV reads and live Angel One pulls. EVERY
method is DB-FIRST with a CSV FALLBACK to today's data/historical files, so
behaviour is identical to today until the DB is populated and nothing breaks
mid-migration: an empty / unreachable DB transparently yields the same result
the old code read straight off disk.

DailyBar is imported from the backtester so the shape is byte-identical to the
one short_vol_backtest / the validators already use. The CSV fallback for
daily_closes reproduces validate_index_strategies.load_bars precisely (deep +
pipeline overlay, else the put-call-parity spot series), so a rewired
validator produces the same bars whether it reads the DB or the disk.

premium_snapshot delegates to the EXISTING index_vol_premium path
(db.latest_index_premium) — the live short-vol gate's own reading, left
entirely untouched.
"""
from __future__ import annotations

import csv
import logging
from datetime import date, datetime
from pathlib import Path

from trading_platform.backtesting.short_vol_backtest import DailyBar, load_daily_closes
from trading_platform.data.instrument_master import INDEX_UNDERLYINGS

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parent.parent.parent
_DEFAULT_HIST = _REPO / "data" / "historical"

# Symbols that are indices (served from index_spot / index_atm_iv30), not
# tradeable cash instruments. INDIAVIX is the NIFTY vol index (control input).
INDEX_SYMBOLS = frozenset(set(INDEX_UNDERLYINGS) | {"INDIAVIX"})


def _exchange_for(symbol: str) -> str:
    spec = INDEX_UNDERLYINGS.get(symbol)
    if spec and spec.get("exchange"):
        return str(spec["exchange"])
    return "NSE"


class MarketDataStore:
    def __init__(self, db=None, hist_dir: Path | str | None = None) -> None:
        self._db = db
        self._db_tried = db is not None
        self.hist = Path(hist_dir) if hist_dir else _DEFAULT_HIST

    # ------------------------------------------------------------------
    # DB handle (lazy; CSV-only if it can't be built)
    # ------------------------------------------------------------------

    @property
    def db(self):
        if self._db is None and not self._db_tried:
            self._db_tried = True
            try:
                from trading_platform.data.persistence import TradingDatabase
                self._db = TradingDatabase()
            except Exception as exc:  # pragma: no cover - env dependent
                logger.debug("MarketDataStore: no DB, CSV-only (%s)", exc)
                self._db = None
        return self._db

    def is_index(self, symbol: str) -> bool:
        return symbol.upper() in INDEX_SYMBOLS

    # ------------------------------------------------------------------
    # daily_closes — underlying daily bars (DB-first, CSV fallback)
    # ------------------------------------------------------------------

    def daily_closes(self, symbol: str) -> list[DailyBar]:
        sym = symbol.upper()
        try:
            db = self.db
            if db is not None:
                rows = (db.index_spot_closes(sym) if self.is_index(sym)
                        else db.eod_ohlcv_closes(sym))
                if rows:
                    return [DailyBar(d, c) for d, c in rows]
        except Exception as exc:
            logger.debug("daily_closes DB read failed for %s: %s", sym, exc)
        return self._daily_closes_csv(sym)

    def _daily_closes_csv(self, sym: str) -> list[DailyBar]:
        """Byte-identical to validate_index_strategies.load_bars: deep (with a
        pipeline overlay) when present, else the parity-spot series, else the
        plain one-day file, else empty."""
        deep = self.hist / f"{sym}__ONE_DAY_deep.csv"
        pipe = self.hist / f"{sym}__ONE_DAY_pipeline.csv"
        parity = self.hist / f"{sym}__PARITY_SPOT_deep.csv"
        plain = self.hist / f"{sym}__ONE_DAY.csv"
        if deep.exists():
            by_day = {b.day: b for b in load_daily_closes(deep)}
            if pipe.exists():
                by_day.update({b.day: b for b in load_daily_closes(pipe)})
            return [by_day[d] for d in sorted(by_day)]
        if parity.exists():
            return load_daily_closes(parity)
        if plain.exists():
            return load_daily_closes(plain)
        return []

    # ------------------------------------------------------------------
    # iv30 — modeled 30-day ATM implied vol (DB-first, CSV fallback)
    # ------------------------------------------------------------------

    def iv30(self, index: str) -> dict[date, float]:
        idx = index.upper()
        try:
            db = self.db
            if db is not None:
                rows = db.index_iv30_series(idx)
                if rows:
                    return {d: v for d, v in rows}
        except Exception as exc:
            logger.debug("iv30 DB read failed for %s: %s", idx, exc)
        path = self.hist / f"{idx}__IV30_deep.csv"
        if path.exists():
            return {b.day: b.close for b in load_daily_closes(path)}
        return {}

    # ------------------------------------------------------------------
    # option_quotes — EOD option quotes (DB-first, CSV fallback)
    # ------------------------------------------------------------------

    def option_quotes(
        self,
        underlying: str,
        *,
        expiry: date | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict]:
        und = underlying.upper()
        try:
            db = self.db
            if db is not None:
                rows = db.option_quotes(und, expiry=expiry, start=start, end=end)
                if rows:
                    return rows
        except Exception as exc:
            logger.debug("option_quotes DB read failed for %s: %s", und, exc)
        return self._option_quotes_csv(und, expiry, start, end)

    def _option_quotes_csv(self, und, expiry, start, end) -> list[dict]:
        """Fallback over the historical ATM-by-expiry CSVs (NSE + BSE). Each
        source row is one ATM strike with a CE and a PE; expand to one quote
        per leg, keeping modeled iv in its own labeled column."""
        out: list[dict] = []
        for sub, src in (("iv", "nse_atm_csv"), ("iv_bse", "bse_atm_csv")):
            path = self.hist / sub / "atm_by_expiry.csv"
            if not path.exists():
                continue
            with open(path, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if (r.get("index") or "").upper() != und:
                        continue
                    try:
                        td = date.fromisoformat(r["date"][:10])
                        exp = date.fromisoformat(r["expiry"][:10])
                        strike = float(r["strike"])
                    except (ValueError, KeyError):
                        continue
                    if expiry is not None and exp != expiry:
                        continue
                    if start is not None and td < start:
                        continue
                    if end is not None and td > end:
                        continue
                    iv = r.get("iv")
                    iv_val = float(iv) if iv not in (None, "") else None
                    for leg, px_key, vol_key, oi_key in (
                        ("CE", "call", "vol_ce", "oi_ce"),
                        ("PE", "put", "vol_pe", "oi_pe"),
                    ):
                        try:
                            px = float(r[px_key])
                        except (ValueError, KeyError):
                            continue
                        out.append({
                            "trade_date": td, "underlying": und, "expiry": exp,
                            "strike": strike, "option_type": leg,
                            "close": px, "settle": px,
                            "volume": _maybe_float(r.get(vol_key)),
                            "oi": _maybe_float(r.get(oi_key)),
                            "underlying_px": _maybe_float(r.get("undrlyg")),
                            "implied_vol": iv_val, "iv_method": "bhavcopy_b76_atm",
                            "source": src,
                        })
        out.sort(key=lambda q: (q["trade_date"], q["expiry"], q["strike"], q["option_type"]))
        return out

    # ------------------------------------------------------------------
    # premium_snapshot — delegate to the EXISTING index_vol_premium path
    # ------------------------------------------------------------------

    def premium_snapshot(self, underlying: str) -> dict | None:
        """Most recent vol-premium reading for `underlying`, from the SAME
        index_vol_premium table the live edge gate reads (db.latest_index_
        premium). Returns the raw row dict, or None. The live gate path is
        not touched by this method."""
        try:
            db = self.db
            if db is None:
                return None
            for r in db.latest_index_premium():
                if r.get("index_symbol") == underlying.upper():
                    return r
        except Exception as exc:
            logger.debug("premium_snapshot failed for %s: %s", underlying, exc)
        return None

    # ------------------------------------------------------------------
    # csv_path_for — historical CSV path (for the file-based dormant validator)
    # ------------------------------------------------------------------

    def csv_path_for(self, symbol: str) -> Path | None:
        """The historical daily-bar CSV for `symbol`: deep if present, else the
        plain file, else None. Byte-identical to the old
        validate_dormant_strategies._csv_path, so routing that validator's
        path lookup through the store changes nothing about what it reads."""
        sym = symbol.upper()
        deep = self.hist / f"{sym}__ONE_DAY_deep.csv"
        if deep.exists():
            return deep
        plain = self.hist / f"{sym}__ONE_DAY.csv"
        return plain if plain.exists() else None


def _maybe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
