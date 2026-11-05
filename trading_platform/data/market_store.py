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
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from trading_platform.backtesting.short_vol_backtest import DailyBar, load_daily_closes
from trading_platform.data.instrument_master import INDEX_UNDERLYINGS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RollEvent:
    """One roll from `from_expiry` to `to_expiry` on `roll_date`. `gap` is the
    back-adjustment offset applied (to_price - from_price, measured when both
    contracts trade on the roll date); `has_overlap` is False when the outgoing
    contract had no price that day (then gap is 0 and the splice is not price-
    continuous — flagged so it is auditable, never silently smoothed)."""
    roll_date: date
    from_expiry: date
    to_expiry: date
    from_price: float | None
    to_price: float | None
    gap: float
    has_overlap: bool


@dataclass
class ContinuousFuture:
    """A back-adjusted continuous daily series for `underlying`.

    `bars` is the difference back-adjusted close series (newest contract left
    unadjusted, older history lifted by the cumulative roll gaps) — so the
    day-over-day change across a roll is an in-contract move, never a spurious
    cross-contract jump. `rolls` records every splice for audit, and
    `roll_dates` marks the roll-boundary days so a caller can refuse to compute
    a return straight across one. Raw eod_ohlcv is never mutated to build this."""
    underlying: str
    roll: str
    bars: list[DailyBar] = field(default_factory=list)
    rolls: list[RollEvent] = field(default_factory=list)
    roll_dates: frozenset = field(default_factory=frozenset)

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
    # Futures curve + continuous (back-adjusted) series — Phase 3
    # ------------------------------------------------------------------

    def futures_curve(self, underlying: str) -> dict[date, dict[date, dict]]:
        """{expiry: {trade_date: {"close": float, "oi": float|None}}} for every
        listed futures contract of `underlying`, from raw eod_ohlcv (DB-only;
        the EOD store has no per-contract CSV fallback). Serves index/stock
        futures (segment FO) and MCX commodity futures (segment FUT) alike.
        `close` falls back to `settle` when the traded close is null. Generalises
        the per-contract curve research_commodity_carry built from Angel One."""
        und = underlying.upper()
        try:
            db = self.db
            if db is None:
                return {}
            rows = db.eod_ohlcv_futures(und)
        except Exception as exc:
            logger.debug("futures_curve DB read failed for %s: %s", und, exc)
            return {}
        curve: dict[date, dict[date, dict]] = {}
        for r in rows:
            exp, td = r.get("expiry"), r.get("trade_date")
            if exp is None or td is None:
                continue
            close = r.get("close")
            if close is None:
                close = r.get("settle")
            if close is None or close <= 0:
                continue
            curve.setdefault(exp, {})[td] = {"close": float(close),
                                             "oi": _maybe_float(r.get("oi"))}
        return curve

    def front_month_series(self, underlying: str, *, as_of: date | None = None
                           ) -> dict[date, float]:
        """The single nearest-to-expiry still-unexpired contract's OWN close
        history ({trade_date: close}) — the roll-free single-contract series
        research_commodity_pairs / research_index_basis use. `as_of` picks which
        contract is 'front' (default: the latest date present in the data)."""
        curve = self.futures_curve(underlying)
        if not curve:
            return {}
        if as_of is None:
            as_of = max((d for e in curve for d in curve[e]), default=None)
        if as_of is None:
            return {}
        live = sorted(e for e in curve if e > as_of)
        front = live[0] if live else max(curve)
        return {d: v["close"] for d, v in sorted(curve[front].items())}

    def continuous_future(self, underlying: str, roll: str = "oi",
                          days_before_expiry: int = 2) -> ContinuousFuture:
        """Build a back-adjusted continuous daily series for `underlying`.

        roll='oi'  : hold the front contract, roll to the next when the next
                     contract's open interest exceeds the front's (the dominant-
                     contract rule), OR within `days_before_expiry` of expiry.
        roll='calendar' : roll purely on `days_before_expiry`.

        Difference back-adjustment removes the price gap at each splice, so the
        continuous level is correct and NO return is ever computed across a roll
        boundary (the change across a roll equals an in-contract move). Every
        roll gap is recorded in the result for audit. Raw eod_ohlcv untouched."""
        und = underlying.upper()
        curve = self.futures_curve(und)
        if not curve:
            return ContinuousFuture(und, roll)
        expiries = sorted(curve)
        all_dates = sorted({d for e in expiries for d in curve[e]})

        segments: list[tuple[date, list[tuple[date, float]]]] = []
        rolls: list[RollEvent] = []
        cur_exp: date | None = None

        for d in all_dates:
            live = sorted(e for e in expiries if e > d and d in curve[e])
            if not live:
                continue
            front = live[0]
            if cur_exp is None or cur_exp not in live:
                new_exp = front  # no current contract, or it expired / stopped trading
            else:
                new_exp = cur_exp
                dte = (cur_exp - d).days
                nxts = [e for e in live if e > cur_exp]
                if dte <= days_before_expiry and nxts:
                    new_exp = nxts[0]
                elif roll == "oi" and nxts:
                    nxt = nxts[0]
                    cur_oi = curve[cur_exp][d].get("oi") or 0.0
                    nxt_oi = curve[nxt][d].get("oi") or 0.0
                    if nxt_oi > cur_oi:
                        new_exp = nxt
            if cur_exp is not None and new_exp < cur_exp:
                new_exp = cur_exp  # never roll backwards to an earlier expiry
            if new_exp != cur_exp:
                if cur_exp is not None:
                    has_ov = d in curve.get(cur_exp, {}) and d in curve[new_exp]
                    fp = curve[cur_exp][d]["close"] if d in curve.get(cur_exp, {}) else None
                    tp = curve[new_exp][d]["close"]
                    gap = (tp - fp) if (has_ov and fp is not None) else 0.0
                    rolls.append(RollEvent(d, cur_exp, new_exp, fp, tp, gap, bool(has_ov)))
                cur_exp = new_exp
                segments.append((cur_exp, []))
            segments[-1][1].append((d, curve[cur_exp][d]["close"]))

        # Difference back-adjustment: newest segment unadjusted; each older
        # segment lifted by the cumulative gap of every roll after it.
        offsets = [0.0] * len(segments)
        for k in range(len(segments) - 2, -1, -1):
            offsets[k] = offsets[k + 1] + rolls[k].gap
        bars: list[DailyBar] = []
        for off, (_exp, seg_bars) in zip(offsets, segments):
            for d, c in seg_bars:
                bars.append(DailyBar(d, c + off))
        bars.sort(key=lambda b: b.day)
        return ContinuousFuture(und, roll, bars, rolls,
                                frozenset(r.roll_date for r in rolls))

    # ------------------------------------------------------------------
    # Corporate-action back-adjustment — Phase 3, point-in-time correct
    # ------------------------------------------------------------------

    def adjusted_closes(self, symbol: str, *, as_of: date | None = None
                        ) -> list[DailyBar]:
        """Raw daily closes of `symbol` back-adjusted for corporate actions,
        POINT-IN-TIME: only actions with ex_date <= `as_of` are applied, so a
        backtest 'as of D' never sees a split that had not happened by D. A bar
        strictly BEFORE an action's ex_date is scaled onto the post-action price
        scale; a bar on/after the ex_date is already post-action and untouched.
        Raw eod_ohlcv is never mutated — the adjustment is computed here.

        Only ratio actions (SPLIT / BONUS) adjust the price; DIV rows are kept
        in the table for audit but are NOT applied to price (cash-dividend back-
        adjustment needs the ex-date close to model and is deliberately left as
        a labeled follow-up rather than approximated)."""
        sym = symbol.upper()
        bars = self.daily_closes(sym)
        actions = self._applicable_corp_actions(sym, as_of)
        if not actions:
            return bars
        out: list[DailyBar] = []
        for b in bars:
            factor = 1.0
            for ex_date, f in actions:
                if b.day < ex_date:  # pre-ex bars lifted onto the post-action scale
                    factor *= f
            out.append(DailyBar(b.day, b.close * factor))
        return out

    def _applicable_corp_actions(self, sym: str, as_of: date | None
                                 ) -> list[tuple[date, float]]:
        """[(ex_date, price_factor)] for the SPLIT/BONUS actions on `sym` with
        ex_date <= as_of. price_factor = ratio_den/ratio_num (a 2:1 split =>
        0.5), the ex-date price multiplier used to back-adjust pre-ex bars."""
        try:
            db = self.db
            if db is None:
                return []
            rows = db.corporate_actions(sym)
        except Exception as exc:
            logger.debug("corporate_actions DB read failed for %s: %s", sym, exc)
            return []
        out: list[tuple[date, float]] = []
        for r in rows:
            ex = r.get("ex_date")
            if ex is None or (as_of is not None and ex > as_of):
                continue
            atype = (r.get("action_type") or "").upper()
            num, den = r.get("ratio_num"), r.get("ratio_den")
            if atype in ("SPLIT", "BONUS") and num and den and num > 0 and den > 0:
                out.append((ex, float(den) / float(num)))
        return out

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
