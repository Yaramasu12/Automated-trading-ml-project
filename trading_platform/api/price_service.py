"""PriceResolutionService — one canonical "current price" for any symbol.

Extracted 2026-08-05 after a FINNIFTY short-vol condor's stop-loss went
unnoticed for hours: option marks and exit-management price reads were each
re-implementing their own live-tick-then-fallback chain (or, in one case,
skipping the live tick entirely and going straight to a rate-limited
historical-candle API), so different parts of the platform disagreed about
what a position was actually worth. This module is the single fallback
chain everyone should call instead of re-deriving it:

    1. A live tick, if fresh (live_feed.latest_tick, staleness-checked
       against the feed's own FeedStalenessTracker.hard_seconds threshold —
       reused, not a second staleness concept).
    2. For options with no live tick: a theoretical Black-Scholes mark off
       the live *underlying* tick (runtime._theoretical_option_mark).
    3. ExitManager's sticky last-known mark.
    4. The position's entry price, as a last-resort, clearly-flagged-stale
       fallback.

Deliberately NOT used for historical-bar feature engineering (regime,
momentum, RSI inputs in decision/pipeline.py, master_orchestrator.py) — that
is a legitimate, different use of daily candles, not the "current tradeable
price" concept this service exists for.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from trading_platform.logging_safety import note_swallowed

PriceSource = Literal["live", "model", "sticky", "entry"]


@dataclass(frozen=True)
class PriceResolution:
    price: float | None
    source: PriceSource | None
    is_stale: bool


class PriceResolutionService:
    def __init__(
        self,
        *,
        live_feed: Any,
        exit_manager: Any,
        theoretical_option_mark: Callable[[Any, datetime], float | None],
    ) -> None:
        self.live_feed = live_feed
        self.exit_manager = exit_manager
        self._theoretical_option_mark = theoretical_option_mark

    def resolve(self, symbol: str, instrument: Any = None, entry_price: float | None = None) -> PriceResolution:
        """Best current price for `symbol`. `instrument` enables the
        theoretical-option-mark fallback; `entry_price` enables the final
        last-resort fallback. Both optional — omit what the caller doesn't
        have and that step is simply skipped."""
        try:
            tick = self.live_feed.latest_tick(symbol)
        except Exception as exc:
            note_swallowed("price_resolution.live_tick", exc)
            tick = None
        if tick is not None and getattr(tick, "last_price", 0) and tick.last_price > 0:
            # An option's LTP only means something if it actually traded
            # today. Angel One echoes a stale reference price — sometimes
            # from a much earlier session, back when the strike was near the
            # money — for a contract with zero trades today, tagged with a
            # wrapper timestamp that refreshes on every poll. Confirmed
            # 2026-08-21: a FINNIFTY put ~700 points OTM carried volume=0,
            # open/high/low=0, and a "live" last_price of 351.5, while
            # neighbouring similarly-OTM strikes (with real volume) priced at
            # 2-6 — the feed's own staleness tracker never caught it, because
            # only the TRADE data was stale, not the tick delivery. An
            # untraded option has no real current price to report; fall
            # through to the theoretical mark below instead of trusting the
            # echo. Indices/spot ticks legitimately carry volume=0 always, so
            # this only applies when `instrument` is an option.
            is_untraded_option = (
                instrument is not None
                and getattr(instrument, "option_type", None) is not None
                and not getattr(tick, "volume", 0)
            )
            if not is_untraded_option:
                is_stale = self._is_tick_stale(symbol)
                return PriceResolution(price=float(tick.last_price), source="live", is_stale=is_stale)

        if instrument is not None:
            try:
                model_price = self._theoretical_option_mark(instrument, datetime.now(timezone.utc))
            except Exception as exc:
                note_swallowed("price_resolution.theoretical_mark", exc)
                model_price = None
            if model_price is not None and model_price > 0:
                return PriceResolution(price=float(model_price), source="model", is_stale=False)

        try:
            sticky = self.exit_manager.current_mark(symbol)
        except Exception as exc:
            note_swallowed("price_resolution.sticky_mark", exc)
            sticky = None
        if sticky is not None and sticky > 0:
            return PriceResolution(price=float(sticky), source="sticky", is_stale=True)

        if entry_price is not None and entry_price > 0:
            return PriceResolution(price=float(entry_price), source="entry", is_stale=True)

        return PriceResolution(price=None, source=None, is_stale=True)

    def _is_tick_stale(self, symbol: str) -> bool:
        tracker = getattr(self.live_feed, "staleness_tracker", None)
        if tracker is None:
            return False
        try:
            return bool(tracker.is_stale(symbol))
        except Exception as exc:
            note_swallowed("price_resolution.staleness_check", exc)
            return False
