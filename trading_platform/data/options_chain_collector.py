"""Daily options-chain snapshot collector — forward data collection for
strategies this project cannot test yet: skew-conditioned short-vol entries,
volatility term-structure, and dispersion trading. All three need historical
IV across strikes/expiries; Angel One has no historical chain archive and
individual weekly contracts only trade 1-3 weeks, so the only honest path is
to start capturing real snapshots now and let history accumulate.

One EOD snapshot per underlying per trading day: resolve the nearest
unexpired options expiry, pick strikes around spot, fetch each contract's
latest traded price via the same rate-limited Angel One candle path already
used by ShortVolExecutor, invert Black-Scholes for IV (same
ImpliedVolatilityCalculator the live short-vol pricing already uses), and
append one row per (date, underlying, expiry, strike, option_type) to a
growing CSV under data/options_chain/ — mirrors the existing
data/historical/*.csv convention.

This is intentionally a SEPARATE, best-effort path: every failure is caught
and counted via note_swallowed rather than propagated, so a rate-limit or a
single bad contract can never affect the money-path agent loop that calls it.
"""
from __future__ import annotations

import csv
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_platform.derivatives.engine import GreeksCalculator, ImpliedVolatilityCalculator
from trading_platform.domain.enums import OptionType, Segment
from trading_platform.logging_safety import note_swallowed

logger = logging.getLogger(__name__)

_FIELDNAMES = ["date", "underlying", "expiry", "dte", "option_type", "strike",
               "spot", "ltp", "iv", "delta"]


class OptionsChainCollector:
    """Holds a `runtime` reference (like ShortVolExecutor does) and reads
    instrument_master/angel_one_history/live_feed/decision_pipeline off it
    lazily on every call, rather than caching them at construction time —
    instrument_master and decision_pipeline are both REPLACED on instrument
    refresh (see TradingRuntime._rebuild_market_engines), so a cached
    reference would silently go stale after the first refresh."""

    def __init__(self, runtime: Any, out_dir: str = "data/options_chain") -> None:
        self._rt = runtime
        self._out_dir = Path(out_dir)
        self._iv_calc = ImpliedVolatilityCalculator()
        self._greeks_calc = GreeksCalculator()

    def _spot_price(self, underlying: str) -> float:
        try:
            tick = self._rt.live_feed.latest_tick(underlying)
            if tick and getattr(tick, "last_price", 0) and tick.last_price > 0:
                return float(tick.last_price)
        except Exception as exc:
            note_swallowed("options_chain_collector.spot_tick", exc)
        # Fallback: the same robust cash/EQ/futures resolution chain
        # ShortVolExecutor already relies on for its own spot price (handles
        # the case a bare index/underlying token isn't itself candle-fetchable).
        try:
            bars = self._rt.decision_pipeline._fetch_bars(underlying, date.today() - timedelta(days=7), 5)
            if bars:
                return float(bars[-1].close)
        except Exception as exc:
            note_swallowed("options_chain_collector.spot_fallback", exc)
        return 0.0

    def _latest_price(self, instrument: Any) -> float | None:
        try:
            to_dt = datetime.now(); from_dt = to_dt - timedelta(days=7)
            bars = self._rt.angel_one_history.get_candles(instrument, from_dt, to_dt, "ONE_DAY")
            if bars and bars[-1].close > 0:
                return float(bars[-1].close)
        except Exception as exc:
            note_swallowed("options_chain_collector.option_price", exc)
        return None

    def capture(self, underlying: str, strikes_each_side: int = 5) -> dict:
        """Capture and persist one EOD chain snapshot. Best-effort throughout —
        returns a summary dict rather than raising, so a caller in the agent's
        tick loop can never be broken by a data problem here."""
        today = date.today()
        try:
            expiries = self._rt.instrument_master.expiries(underlying, Segment.OPTIONS)
        except Exception as exc:
            note_swallowed("options_chain_collector.expiries", exc)
            return {"underlying": underlying, "rows": 0, "error": "no instrument master expiries"}
        # Skip same-day/next-day expiries: an option trading at near-pure
        # intrinsic value with almost no time left inverts to a near-zero,
        # numerically meaningless IV (observed directly: dte=1 NIFTY calls
        # here gave IV~0.001 while genuinely 1-2 weeks out gave sane values)
        # — not useful for a skew/term-structure dataset, so prefer the next
        # expiry out instead of burning API calls capturing noise.
        upcoming = sorted(e for e in expiries if (e - today).days >= 2)
        if not upcoming:
            return {"underlying": underlying, "rows": 0, "error": "no upcoming expiry with >=2 DTE"}
        expiry = upcoming[0]
        dte = max((expiry - today).days, 1)

        spot = self._spot_price(underlying)
        if spot <= 0:
            return {"underlying": underlying, "rows": 0, "error": "no spot price"}

        opts = [i for i in self._rt.instrument_master.by_underlying(underlying, Segment.OPTIONS)
                if i.expiry == expiry and i.strike]
        strikes = sorted({i.strike for i in opts})
        if not strikes:
            return {"underlying": underlying, "rows": 0, "error": "no strikes listed"}
        nearest_idx = min(range(len(strikes)), key=lambda idx: abs(strikes[idx] - spot))
        lo = max(0, nearest_idx - strikes_each_side)
        hi = min(len(strikes), nearest_idx + strikes_each_side + 1)
        selected = strikes[lo:hi]

        rows: list[dict] = []
        for strike in selected:
            for otype in (OptionType.CE, OptionType.PE):
                inst = next((i for i in opts if i.strike == strike and i.option_type == otype), None)
                if inst is None:
                    continue
                price = self._latest_price(inst)
                if price is None or price <= 0:
                    continue
                iv = None
                delta = None
                try:
                    iv = self._iv_calc.calculate(price, spot, strike, dte, otype)
                    greeks = self._greeks_calc.calculate(spot, strike, dte, iv, otype)
                    delta = float(greeks.delta)
                except Exception as exc:
                    note_swallowed("options_chain_collector.iv", exc)
                rows.append({
                    "date": today.isoformat(), "underlying": underlying,
                    "expiry": expiry.isoformat(), "dte": dte, "option_type": otype.value,
                    "strike": strike, "spot": spot, "ltp": price,
                    "iv": round(iv, 5) if iv is not None else "", "delta": round(delta, 4) if delta is not None else "",
                })
        if rows:
            self._append_csv(underlying, rows)
        logger.info("options_chain_collector: %s expiry=%s captured %d/%d strikes",
                    underlying, expiry, len(rows), len(selected) * 2)
        return {"underlying": underlying, "expiry": expiry.isoformat(), "rows": len(rows)}

    def atm_iv_history(self, underlying: str, lookback_days: int | None = None) -> list[float]:
        """Per-trading-day ATM implied vol (vol points, e.g. 15.2 not 0.152)
        derived from the accumulated chain-history CSV: for each captured
        date, average the CE+PE IV at the strike nearest that date's own
        spot. This is the IV-rank lookback series for underlyings with no
        published VIX-like index (see derivatives.engine.compute_iv_rank and
        ShortVolExecutor's use of it) — empty (or short) until enough EOD
        snapshots have accumulated, since this collector runs once/trading
        day; callers must handle "not enough history yet" honestly rather
        than treat an empty/short list as zero IV.
        """
        path = self._csv_path(underlying)
        if not path.exists():
            return []
        by_date: dict[str, list[dict]] = {}
        try:
            with path.open() as fh:
                for row in csv.DictReader(fh):
                    by_date.setdefault(row["date"], []).append(row)
        except Exception as exc:
            note_swallowed("options_chain_collector.atm_iv_history_read", exc)
            return []
        dates = sorted(by_date.keys())
        if lookback_days:
            dates = dates[-lookback_days:]
        history: list[float] = []
        for d in dates:
            rows = by_date[d]
            try:
                spot = float(rows[0]["spot"])
                strikes = sorted({float(r["strike"]) for r in rows if r.get("strike")})
            except (KeyError, ValueError, IndexError):
                continue
            if not strikes:
                continue
            nearest = min(strikes, key=lambda s: abs(s - spot))
            ivs: list[float] = []
            for r in rows:
                try:
                    if r.get("iv") and float(r["strike"]) == nearest:
                        ivs.append(float(r["iv"]) * 100.0)  # annualised fraction -> vol points
                except ValueError:
                    continue
            if ivs:
                history.append(sum(ivs) / len(ivs))
        return history

    def _csv_path(self, underlying: str) -> Path:
        return self._out_dir / f"{underlying}_chain_history.csv"

    def _append_csv(self, underlying: str, rows: list[dict]) -> None:
        path = self._csv_path(underlying)
        self._out_dir.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists()
        with path.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_FIELDNAMES)
            if write_header:
                w.writeheader()
            for row in rows:
                w.writerow(row)

    def status(self) -> dict:
        """Read-only progress report: how much chain history has accumulated
        so far per underlying, for tracking readiness toward eventually
        testing skew/term-structure/dispersion hypotheses."""
        out: dict[str, dict] = {}
        if not self._out_dir.exists():
            return out
        for path in sorted(self._out_dir.glob("*_chain_history.csv")):
            underlying = path.name.replace("_chain_history.csv", "")
            dates: set[str] = set()
            rows = 0
            try:
                with path.open() as fh:
                    for r in csv.DictReader(fh):
                        dates.add(r["date"])
                        rows += 1
            except Exception as exc:
                note_swallowed("options_chain_collector.status_read", exc)
                continue
            out[underlying] = {
                "trading_days_captured": len(dates),
                "total_rows": rows,
                "first_date": min(dates) if dates else None,
                "last_date": max(dates) if dates else None,
            }
        return out
