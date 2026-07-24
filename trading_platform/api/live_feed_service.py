"""LiveFeedService — paper-safe live market-data feed control, extracted from runtime.

Continues the runtime decomposition (review finding #1). Owns start/stop/snapshot of
the live tick feed, feed-symbol resolution, and single-symbol tick lookup. Deps are
injected under their EXACT runtime attribute names so the method bodies are a verbatim
move from TradingRuntime.

Reassignment notes (the subtle part of extracting from the coupled core):
  * ``instrument_master`` is rebuilt on instrument refresh, so the runtime
    reconstructs this service in ``_rebuild_market_engines`` (same approach as
    OptionsService) rather than letting it hold a stale master.
  * ``execution_mode`` and live-order eligibility change WITHOUT a rebuild (mode
    switch / arm), so they are read through injected callables to stay live.
  * ``live_feed``, ``instrument_freshness``, ``monitor`` and ``settings`` are
    constructed once and never reassigned, so they are injected by value.
"""
from __future__ import annotations

from typing import Any, Callable

from trading_platform.agent.market_hours import (
    is_entry_allowed,
    is_mcx_entry_allowed,
    market_status,
    now_ist,
)
from trading_platform.logging_safety import note_swallowed


class LiveFeedService:
    def __init__(
        self,
        *,
        live_feed: Any,
        instrument_master: Any,
        instrument_freshness: Any,
        monitor: Any,
        settings: Any,
        can_submit_live_orders: Callable[[], bool],
        load_cached_instruments: Callable[[], bool],
        get_execution_mode: Callable[[], Any],
    ) -> None:
        self.live_feed = live_feed
        self.instrument_master = instrument_master
        self.instrument_freshness = instrument_freshness
        self.monitor = monitor
        self.settings = settings
        self._can_submit_live_orders = can_submit_live_orders
        self._load_cached_instruments_if_available = load_cached_instruments
        self._get_execution_mode = get_execution_mode

    def start_live_feed(self, symbols: list[str] | None = None) -> dict:
        if not self.settings.angel_one_configured:
            raise ValueError("Angel One credentials are required to start the live market-data feed")
        if self.instrument_freshness.status()["is_synthetic"]:
            self._load_cached_instruments_if_available()
        requested_symbols = symbols or list(self.settings.live_feed_default_symbols)
        symbols = self._resolve_feed_symbols(requested_symbols)
        if not symbols:
            raise ValueError("No feed symbols could be resolved from the current instrument master")
        if len(symbols) > self.settings.live_feed_max_symbols:
            symbols = symbols[: self.settings.live_feed_max_symbols]
        # Register ALL instruments so the feed has token+exchange mappings for everything
        self.live_feed.register_instruments(self.instrument_master.all())
        self.live_feed.subscribe(symbols)
        self.live_feed.start()
        self.monitor.record_event(
            "live_feed_started",
            f"Paper-safe live tick feed started for {len(symbols)} symbol(s)",
        )
        return {
            "started": True,
            "mode": "paper_market_data",
            "live_orders_possible": self._can_submit_live_orders(),
            "symbols": symbols,
            "symbol_count": len(symbols),
            "max_symbols": self.settings.live_feed_max_symbols,
        }

    def stop_live_feed(self) -> dict:
        self.live_feed.stop()
        self.monitor.record_event("live_feed_stopped", "Live tick feed stopped")
        return {"stopped": True}

    def live_feed_snapshot(self) -> dict:
        snap = self.live_feed.snapshot()
        snap["mode"] = "paper_market_data" if not self._get_execution_mode().value.startswith("LIVE") else "live_market_data"
        snap["live_orders_possible"] = self._can_submit_live_orders()
        snap["default_symbols"] = list(self.settings.live_feed_default_symbols)
        snap["max_symbols"] = self.settings.live_feed_max_symbols
        snap["freshness"] = self._freshness_summary(snap)
        return snap

    def _freshness_summary(self, snap: dict) -> dict:
        """Compact freshness block the dashboard renders directly, so the UI
        never has to *infer* staleness from message timing.

        - market_status / market_open: is a trading session live right now? When
          the market is closed a still feed is EXPECTED, not an error.
        - freshest_tick_age_seconds: age of the newest tick across subscribed
          symbols (None if nothing has ever ticked).
        - stale: feed should be flowing (running + a session open) but the newest
          tick is older than the hard threshold — the genuine "frozen" condition.
        """
        now = now_ist()
        market_open = bool(is_entry_allowed(now) or is_mcx_entry_allowed(now))
        staleness = snap.get("staleness") or {}
        hard = float(staleness.get("hard_seconds") or 0.0)
        ages = [
            row.get("age_seconds")
            for row in staleness.get("per_symbol", [])
            if isinstance(row.get("age_seconds"), (int, float))
        ]
        freshest = min(ages) if ages else None
        running = bool(snap.get("running"))
        stale = bool(
            running and market_open and (freshest is None or (hard > 0 and freshest > hard))
        )
        return {
            "market_status": market_status(now),
            "market_open": market_open,
            "freshest_tick_age_seconds": freshest,
            "hard_seconds": hard or None,
            "stale": stale,
            "as_of": now.isoformat(),
        }

    def _resolve_feed_symbols(self, requested_symbols: list[str] | tuple[str, ...]) -> list[str]:
        today = now_ist().date()
        resolved: list[str] = []
        seen: set[str] = set()
        all_symbols = set(self.instrument_master.instruments)
        for raw_symbol in requested_symbols:
            symbol = str(raw_symbol).strip().upper()
            if not symbol:
                continue
            candidates: list[str] = []
            if symbol in all_symbols:
                candidates.append(symbol)
            cash_symbol = f"{symbol}-EQ"
            if cash_symbol in all_symbols:
                candidates.append(cash_symbol)
            try:
                future = self.instrument_master.select_future(symbol, today)
                candidates.append(future.symbol)
            except Exception as exc:
                note_swallowed("resolve_feed_symbols.select_future", exc)
            for candidate in candidates:
                if candidate not in seen:
                    resolved.append(candidate)
                    seen.add(candidate)
                    break
        return resolved

    def latest_tick(self, symbol: str) -> dict:
        tick = self.live_feed.latest_tick(symbol.upper())
        if tick is None:
            return {"symbol": symbol.upper(), "available": False}
        return {"available": True, **tick.to_dict()}
