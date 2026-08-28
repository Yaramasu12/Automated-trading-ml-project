"""
Options Chain Collector.

`OptionsChainCollector` (below) is the original, production-wired EOD
snapshot collector: forward data collection for strategies this project
cannot test yet (skew-conditioned short-vol entries, volatility
term-structure, dispersion trading). One EOD snapshot per underlying per
trading day: resolve the nearest unexpired options expiry, pick strikes
around spot, fetch each contract's latest traded price via the same
rate-limited Angel One candle path already used by ShortVolExecutor, invert
Black-Scholes for IV (same ImpliedVolatilityCalculator the live short-vol
pricing already uses), and append one row per (date, underlying, expiry,
strike, option_type) to a growing CSV under data/options_chain/ — mirrors the
existing data/historical/*.csv convention. Intentionally best-effort:
every failure is caught and counted via note_swallowed rather than
propagated, so a rate-limit or a single bad contract can never affect the
money-path agent loop that calls it. Constructed as
`OptionsChainCollector(runtime)` in api/runtime.py — do not change this
constructor signature without updating that call site and
tests/test_options_chain_collector.py.

`StreamingOptionsChainCollector` (REDESIGN_PROMPT.md §3/§5) is a newer,
not-yet-wired-in design: periodic full-chain snapshots + IV-rank/IV-percentile
tracking via `MarketDataAdapter`/tick-bus, for the eventual VRP entry signal
(§4.2). It is not a drop-in replacement for `OptionsChainCollector` — it has
no CSV history, no `capture()`/`atm_iv_history()`/`status()` API, and nothing
constructs it yet. Do not rename it back to `OptionsChainCollector`; that
collision previously broke every test in this module plus
`TradingRuntime.__init__` itself (see memory redesign-prompt-status).
"""

from __future__ import annotations

import asyncio
import csv
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from collections import defaultdict

import polars as pl

from trading_platform.config import Settings
from trading_platform.data.market_adapter import FeedSource
from trading_platform.derivatives.engine import GreeksCalculator, ImpliedVolatilityCalculator
from trading_platform.domain.enums import OptionType, Segment
from trading_platform.logging_safety import note_swallowed

logger = logging.getLogger(__name__)


class _ChainRateLimited(Exception):
    """Internal marker: a per-strike price fetch hit Angel One's rate limit.

    `capture()` swallows ordinary per-strike failures (one bad contract must
    not lose the rest of the chain), but a rate-limit is different: it is
    global to the API key, so every remaining fetch in this sweep — and the
    decision pipeline's own candle calls — will fail too. Raising this lets
    `capture()` stop early and tell its caller, instead of quietly burning
    the rest of the rate-limit budget one swallowed exception at a time.
    """


# ---------------------------------------------------------------------------
# Data models (REDESIGN §3/§5 — used by StreamingOptionsChainCollector)
# ---------------------------------------------------------------------------

@dataclass
class OptionChainSnapshot:
    """Represents a full option chain snapshot for an underlying."""
    underlying: str  # e.g., "NIFTY", "BANKNIFTY"
    timestamp: int  # unix timestamp
    spot_price: float
    expiry_dates: list[str]  # upcoming expiry dates
    calls: list[dict[str, Any]]  # call option data
    puts: list[dict[str, Any]]  # put option data
    source: FeedSource = FeedSource.ANGEL_ONE

    @property
    def pcr(self) -> float:
        """Put-Call Ratio (OI-based)."""
        total_call_oi = sum(c.get("oi", 0) for c in self.calls)
        total_put_oi = sum(p.get("oi", 0) for p in self.puts)
        if total_put_oi == 0:
            return 0.0
        return total_call_oi / total_put_oi

    @property
    def atm_strike(self) -> float:
        """ATM strike (nearest to spot)."""
        if not self.calls and not self.puts:
            return 0.0
        all_strikes = {c["strike"] for c in self.calls} | {p["strike"] for p in self.puts}
        return min(all_strikes, key=lambda s: abs(s - self.spot_price)) if all_strikes else self.spot_price

    @property
    def atm_iv(self) -> float:
        """ATM implied volatility."""
        atm = self.atm_strike
        if self.calls:
            for c in self.calls:
                if abs(c["strike"] - atm) < 0.01:
                    return c.get("iv", 0.0)
        if self.puts:
            for p in self.puts:
                if abs(p["strike"] - atm) < 0.01:
                    return p.get("iv", 0.0)
        return 0.0


@dataclass
class IVRankRecord:
    """IV rank history record."""
    underlying: str
    expiry: str
    moneyness: str  # "ATM", "ITM_5", "OTM_5", etc.
    timestamp: int
    iv: float
    iv_min: float
    iv_max: float
    iv_rank: float  # percentile (0-100)
    iv_percentile: float  # alias for rank
    iv_zscore: float  # (iv - mean) / std


# ---------------------------------------------------------------------------
# IV Rank Calculator
# ---------------------------------------------------------------------------

class IVRankCalculator:
    """
    Computes IV-rank and IV-percentile from historical IV data.

    Maintains a rolling window of IV observations per (underlying, expiry, moneyness).
    """

    def __init__(self, window_size: int = 252):  # ~1 year of trading days
        self._window_size = window_size
        # Key: (underlying, expiry, moneyness) → list of (timestamp, iv)
        self._history: dict[tuple[str, str, str], list[tuple[int, float]]] = defaultdict(list)

    def add_observation(
        self,
        underlying: str,
        expiry: str,
        moneyness: str,
        timestamp: int,
        iv: float,
    ) -> None:
        """Add an IV observation."""
        key = (underlying, expiry, moneyness)
        self._history[key].append((timestamp, iv))

        # Trim to window size
        if len(self._history[key]) > self._window_size:
            self._history[key] = self._history[key][-self._window_size:]

    def compute_rank(
        self,
        underlying: str,
        expiry: str,
        moneyness: str,
        timestamp: int,
        iv: float,
    ) -> Optional[IVRankRecord]:
        """Compute IV-rank for an observation."""
        key = (underlying, expiry, moneyness)
        history = self._history.get(key, [])

        if len(history) < 10:  # Need minimum history
            return None

        iv_values = [v for _, v in history]
        iv_min = min(iv_values)
        iv_max = max(iv_values)
        iv_mean = sum(iv_values) / len(iv_values)
        iv_std = (sum((v - iv_mean) ** 2 for v in iv_values) / len(iv_values)) ** 0.5

        # IV rank: percentile of current IV in historical distribution
        if iv_max > iv_min:
            iv_rank = ((iv - iv_min) / (iv_max - iv_min)) * 100.0
        else:
            iv_rank = 50.0

        # Clamp to [0, 100]
        iv_rank = max(0.0, min(100.0, iv_rank))

        # IV z-score
        iv_zscore = (iv - iv_mean) / iv_std if iv_std > 0 else 0.0

        return IVRankRecord(
            underlying=underlying,
            expiry=expiry,
            moneyness=moneyness,
            timestamp=timestamp,
            iv=iv,
            iv_min=iv_min,
            iv_max=iv_max,
            iv_rank=iv_rank,
            iv_percentile=iv_rank,
            iv_zscore=iv_zscore,
        )

    def get_iv_rank_history(
        self,
        underlying: str,
        expiry: str,
        moneyness: str,
        limit: int = 100,
    ) -> list[IVRankRecord]:
        """Get IV-rank history for a (underlying, expiry, moneyness) key."""
        key = (underlying, expiry, moneyness)
        history = self._history.get(key, [])
        return [self.compute_rank(underlying, expiry, moneyness, ts, iv)
                for ts, iv in history[-limit:]]


# ---------------------------------------------------------------------------
# Streaming options chain collector (REDESIGN §3/§5 — not yet wired in)
# ---------------------------------------------------------------------------

class StreamingOptionsChainCollector:
    """
    Collects option chain snapshots periodically via a MarketDataAdapter and
    publishes to the Redis tick bus, computing IV-rank per moneyness band.

    Not constructed anywhere yet — `OptionsChainCollector` below is the one
    api/runtime.py actually uses. This is the target design once the
    MarketDataAdapter/tick-bus plumbing (§3.2) is wired into the real runtime.
    """

    # Underlyings to track (expandable)
    DEFAULT_UNDERLYINGS: list[str] = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]

    # Moneyness bands for IV-rank tracking
    MONEYNESS_BANDS: list[str] = ["ATM", "ITM_5", "ITM_10", "OTM_5", "OTM_10", "OTM_15"]

    def __init__(
        self,
        settings: Settings,
        tick_bus: Any,  # TickBus for publishing
        underlying: Optional[str] = None,
        interval_sec: int = 30,  # snapshot every 30-60s
    ) -> None:
        self._settings = settings
        self._tick_bus = tick_bus
        self._underlying = underlying or "NIFTY"
        self._interval_sec = interval_sec

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._calculator = IVRankCalculator()

        # IV-rank cache (key → latest rank)
        self._iv_rank_cache: dict[str, IVRankRecord] = {}

        # Monitor stale chains
        self._last_snapshot_time: float = 0.0
        self._staleness_threshold_sec: float = 120.0  # 2 min in market hours

    async def start(self) -> None:
        """Start periodic chain collection."""
        self._running = True
        self._task = asyncio.create_task(self._collect_loop())
        logger.info("StreamingOptionsChainCollector starting for %s (interval=%ds)", self._underlying, self._interval_sec)

    async def stop(self) -> None:
        """Stop chain collection."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("StreamingOptionsChainCollector stopped")

    async def _collect_loop(self) -> None:
        """Main collection loop."""
        while self._running:
            try:
                await self._collect_one()
            except Exception as e:
                logger.error("Chain collection error: %s", e)

            # Sleep in small increments for responsiveness
            for _ in range(self._interval_sec * 10):
                await asyncio.sleep(0.1)
                if not self._running:
                    break

    async def _collect_one(self) -> None:
        """Collect one chain snapshot."""
        now = time.time()
        self._last_snapshot_time = now

        # Determine which underlyings to collect
        underlyings = [self._underlying] if self._underlying else self.DEFAULT_UNDERLYINGS

        for underlying in underlyings:
            try:
                # Get option chain from the market data adapter
                chain = await self._fetch_chain(underlying)
                if chain is None:
                    logger.warning("No chain data for %s", underlying)
                    continue

                # Publish to tick bus
                await self._tick_bus.publish_chain_snapshot(chain)

                # Compute IV-rank for each moneyness band
                await self._compute_iv_rank(chain)

                # Monitor staleness
                if chain.atm_iv > 0:
                    logger.debug(
                        "Chain %s: spot=%.2f, ATM_IV=%.2f, PCR=%.3f",
                        underlying, chain.spot_price, chain.atm_iv, chain.pcr,
                    )

            except Exception as e:
                logger.error("Chain collection failed for %s: %s", underlying, e)

    async def _fetch_chain(self, underlying: str) -> Optional[OptionChainSnapshot]:
        """Fetch option chain from the market data adapter."""
        # Try Upstox first (richer data)
        if self._settings.upstox_enabled:
            from trading_platform.data.upstox_feed import create_upstox_adapter
            from trading_platform.config import Settings as AppSettings
            try:
                adapter = create_upstox_adapter(AppSettings())
                raw_chain = await adapter.get_option_chain(underlying)
                if raw_chain:
                    return self._parse_chain(raw_chain, FeedSource.UPSTOX)
            except Exception as e:
                logger.warning("Upstox chain fetch failed for %s: %s", underlying, e)

        # Fallback to Angel One
        from trading_platform.data.angel_one_adapter import create_angel_one_adapter
        from trading_platform.config import Settings as AppSettings
        try:
            adapter = create_angel_one_adapter(AppSettings())
            raw_chain = await adapter.get_option_chain(underlying)
            if raw_chain:
                return self._parse_chain(raw_chain, FeedSource.ANGEL_ONE)
        except Exception as e:
            logger.warning("Angel One chain fetch failed for %s: %s", underlying, e)

        return None

    def _parse_chain(
        self,
        raw_chain: list[dict[str, Any]],
        source: FeedSource,
    ) -> OptionChainSnapshot:
        """Parse raw chain data → OptionChainSnapshot."""
        calls = []
        puts = []
        spot_price = 0.0
        expiry_dates: set[str] = set()

        for item in raw_chain:
            strike = float(item.get("strike", 0))
            option_type = item.get("option_type", item.get("type", "CE"))
            iv = float(item.get("iv", item.get("implied_volatility", 0)))
            oi = int(item.get("oi", item.get("open_interest", 0)))
            delta = float(item.get("delta", 0))
            gamma = float(item.get("gamma", 0))
            theta = float(item.get("theta", 0))
            vega = float(item.get("vega", 0))

            if option_type in ("CE", "CALL", "C"):
                calls.append({
                    "strike": strike,
                    "iv": iv,
                    "oi": oi,
                    "delta": delta,
                    "gamma": gamma,
                    "theta": theta,
                    "vega": vega,
                    "expiry": item.get("expiry", item.get("expiration_date", "")),
                })
            else:
                puts.append({
                    "strike": strike,
                    "iv": iv,
                    "oi": oi,
                    "delta": delta,
                    "gamma": gamma,
                    "theta": theta,
                    "vega": vega,
                    "expiry": item.get("expiry", item.get("expiration_date", "")),
                })

            if item.get("expiry", item.get("expiration_date", "")):
                expiry_dates.add(item.get("expiry", item.get("expiration_date", "")))

            # Spot price from the data or compute from ATM
            if item.get("spot_price"):
                spot_price = float(item["spot_price"])

        # If no spot price in data, compute from ATM
        if spot_price == 0:
            all_strikes = {c["strike"] for c in calls} | {p["strike"] for p in puts}
            if all_strikes:
                spot_price = min(all_strikes, key=lambda s: s) + 1  # rough estimate

        return OptionChainSnapshot(
            underlying=self._underlying,
            timestamp=int(time.time()),
            spot_price=spot_price,
            expiry_dates=sorted(expiry_dates),
            calls=calls,
            puts=puts,
            source=source,
        )

    async def _compute_iv_rank(self, chain: OptionChainSnapshot) -> None:
        """Compute IV-rank for each moneyness band and add to chain snapshot."""
        atm = chain.atm_strike
        if atm == 0:
            return

        # Determine moneyness for each strike
        for call in chain.calls:
            strike = call["strike"]
            moneyness = self._classify_moneyness(strike, atm)
            iv = call["iv"]
            if iv <= 0:
                continue

            for expiry in chain.expiry_dates:
                rank = self._calculator.compute_rank(
                    chain.underlying, expiry, moneyness,
                    chain.timestamp, iv,
                )
                if rank:
                    key = (chain.underlying, expiry, moneyness)
                    self._iv_rank_cache[key] = rank
                    call["iv_rank"] = rank.iv_rank
                    call["iv_zscore"] = rank.iv_zscore

        for put in chain.puts:
            strike = put["strike"]
            moneyness = self._classify_moneyness(strike, atm)
            iv = put["iv"]
            if iv <= 0:
                continue

            for expiry in chain.expiry_dates:
                rank = self._calculator.compute_rank(
                    chain.underlying, expiry, moneyness,
                    chain.timestamp, iv,
                )
                if rank:
                    key = (chain.underlying, expiry, moneyness)
                    self._iv_rank_cache[key] = rank
                    put["iv_rank"] = rank.iv_rank
                    put["iv_zscore"] = rank.iv_zscore

    def _classify_moneyness(self, strike: float, atm: float) -> str:
        """Classify strike into moneyness band."""
        if abs(strike - atm) < atm * 0.01:
            return "ATM"
        pct = (strike - atm) / atm * 100
        if pct > 10:
            return "OTM_15" if pct > 14 else "OTM_10" if pct > 9 else "OTM_5"
        else:
            return "ITM_5" if pct < -4 else "ITM_10" if pct < -9 else "ITM_15"

    def get_iv_rank(self, underlying: str, expiry: str, moneyness: str) -> Optional[IVRankRecord]:
        """Get latest IV-rank for a key."""
        return self._iv_rank_cache.get((underlying, expiry, moneyness))

    def get_iv_rank_histogram(
        self, underlying: str, expiry: str, moneyness: str
    ) -> pl.DataFrame:
        """Get IV-rank history as Polars DataFrame."""
        records = self._calculator.get_iv_rank_history(underlying, expiry, moneyness, limit=252)
        if not records:
            return pl.DataFrame()

        return pl.DataFrame([
            {
                "timestamp": r.timestamp,
                "iv": r.iv,
                "iv_rank": r.iv_rank,
                "iv_zscore": r.iv_zscore,
            }
            for r in records
        ])

    def check_staleness(self) -> bool:
        """Check if chain data is stale (market hours active)."""
        if self._last_snapshot_time == 0:
            return False
        return (time.time() - self._last_snapshot_time) > self._staleness_threshold_sec


def create_streaming_chain_collector(
    settings: Settings,
    tick_bus: Any,
    underlying: Optional[str] = None,
) -> StreamingOptionsChainCollector:
    """Create a StreamingOptionsChainCollector."""
    return StreamingOptionsChainCollector(
        settings=settings,
        tick_bus=tick_bus,
        underlying=underlying,
        interval_sec=30,
    )


# ---------------------------------------------------------------------------
# Options chain collector (original — production-wired, do not rename/replace)
# ---------------------------------------------------------------------------

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
            msg = str(exc).lower()
            # Same phrasing Angel One returns for candle throttling elsewhere
            # (see decision/pipeline.py's identical check). Distinguishing
            # this from an ordinary per-contract failure is the whole fix —
            # see _ChainRateLimited's docstring.
            if "too many requests" in msg or "access rate" in msg or "exceeding" in msg:
                raise _ChainRateLimited(str(exc)) from exc
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
                try:
                    price = self._latest_price(inst)
                except _ChainRateLimited:
                    if rows:
                        self._append_csv(underlying, rows)
                    logger.warning(
                        "options_chain_collector: %s rate-limited by Angel One after "
                        "%d/%d strikes — stopping this sweep early",
                        underlying, len(rows), len(selected) * 2,
                    )
                    return {
                        "underlying": underlying, "expiry": expiry.isoformat(),
                        "rows": len(rows), "rate_limited": True,
                    }
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
