"""Phase-1 per-symbol feed staleness tracker.

The existing ``LiveTickFeed.snapshot()`` reports
``{running, tick_count}`` — neither of which proves *this* symbol
ticked in the last 15 seconds. A disconnected WebSocket sits at
``running=true`` forever; strategies happily trade on cached prices.

This tracker records the last-tick timestamp per symbol and answers
two questions:

1. ``snapshot()`` — full per-symbol staleness for monitoring.
2. ``gate(symbols)`` — pass/fail decision for the readiness check.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


# Default thresholds. Cash + index typically tick several times per
# second during market hours; 15 s without a tick on a subscribed
# symbol is suspicious. 60 s is "definitely broken." Override per
# segment in production via ``FeedStalenessTracker(hard_seconds=, ...)``.
DEFAULT_HARD_THRESHOLD_SECONDS = 15.0
DEFAULT_WARN_THRESHOLD_SECONDS = 60.0


@dataclass(frozen=True)
class SymbolStaleness:
    symbol: str
    last_tick_at: datetime | None
    age_seconds: float | None  # None when no tick has arrived
    is_warn: bool
    is_stale: bool

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "age_seconds": round(self.age_seconds, 2) if self.age_seconds is not None else None,
            "is_warn": self.is_warn,
            "is_stale": self.is_stale,
        }


@dataclass(frozen=True)
class FeedReadinessGate:
    passed: bool
    reason: str
    evidence: dict

    def to_dict(self) -> dict:
        return {"passed": self.passed, "reason": self.reason, "evidence": dict(self.evidence)}


class FeedStalenessTracker:
    """Per-symbol last-tick tracker. Thread-safe."""

    def __init__(
        self,
        hard_seconds: float = DEFAULT_HARD_THRESHOLD_SECONDS,
        warn_seconds: float = DEFAULT_WARN_THRESHOLD_SECONDS,
        clock=None,
    ) -> None:
        if hard_seconds <= 0:
            raise ValueError("hard_seconds must be > 0")
        if warn_seconds < hard_seconds:
            raise ValueError("warn_seconds must be ≥ hard_seconds")
        self.hard_seconds = float(hard_seconds)
        self.warn_seconds = float(warn_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_tick: dict[str, datetime] = {}
        self._lock = threading.Lock()

    # ── Recording ────────────────────────────────────────────────────

    def record(self, symbol: str, when: datetime | None = None) -> None:
        ts = when or self._clock()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        with self._lock:
            self._last_tick[symbol.upper()] = ts

    def reset(self, symbol: str | None = None) -> None:
        """Drop tracking state for a symbol (or all symbols when ``None``).

        Used when a symbol unsubscribes — we don't want it counted
        against staleness afterwards.
        """
        with self._lock:
            if symbol is None:
                self._last_tick.clear()
            else:
                self._last_tick.pop(symbol.upper(), None)

    # ── Inspection ───────────────────────────────────────────────────

    def last_tick_at(self, symbol: str) -> datetime | None:
        with self._lock:
            return self._last_tick.get(symbol.upper())

    def age_seconds(self, symbol: str) -> float | None:
        ts = self.last_tick_at(symbol)
        if ts is None:
            return None
        return (self._clock() - ts).total_seconds()

    def is_stale(self, symbol: str) -> bool:
        age = self.age_seconds(symbol)
        if age is None:
            return True  # never ticked → stale by default
        return age > self.hard_seconds

    def status(self, symbol: str) -> SymbolStaleness:
        age = self.age_seconds(symbol)
        ts = self.last_tick_at(symbol)
        return SymbolStaleness(
            symbol=symbol.upper(),
            last_tick_at=ts,
            age_seconds=age,
            is_warn=age is not None and age > self.warn_seconds,
            is_stale=age is None or age > self.hard_seconds,
        )

    def snapshot(self, symbols: Iterable[str] | None = None) -> dict:
        """Full snapshot. When ``symbols`` is given, restrict to that
        set; otherwise reports every symbol that has ever ticked.
        """
        if symbols is None:
            with self._lock:
                target = list(self._last_tick.keys())
        else:
            target = [s.upper() for s in symbols]
        per_symbol = [self.status(s) for s in target]
        stale = [s.symbol for s in per_symbol if s.is_stale]
        warn = [s.symbol for s in per_symbol if s.is_warn and not s.is_stale]
        return {
            "hard_seconds": self.hard_seconds,
            "warn_seconds": self.warn_seconds,
            "tracked_symbols": len(per_symbol),
            "stale_symbols": stale,
            "warn_symbols": warn,
            "per_symbol": [s.to_dict() for s in per_symbol],
        }

    # ── Readiness gate ───────────────────────────────────────────────

    def gate(
        self,
        subscribed_symbols: Iterable[str],
        *,
        feed_running: bool,
    ) -> FeedReadinessGate:
        """Pass/fail decision used by the live-readiness aggregator.

        Fails for any of:

        * ``feed_running == False``
        * ``len(subscribed_symbols) == 0``
        * any subscribed symbol has no tick or is stale beyond hard threshold
        """
        symbols = [s.upper() for s in subscribed_symbols]
        if not feed_running:
            return FeedReadinessGate(
                passed=False,
                reason="feed_not_running",
                evidence={"feed_running": False},
            )
        if not symbols:
            return FeedReadinessGate(
                passed=False,
                reason="no_symbols_subscribed",
                evidence={"subscribed": []},
            )
        per_symbol = [self.status(s) for s in symbols]
        stale = [s.symbol for s in per_symbol if s.is_stale]
        if stale:
            max_age = max((s.age_seconds or float("inf")) for s in per_symbol if s.is_stale)
            return FeedReadinessGate(
                passed=False,
                reason=f"stale_symbols:{len(stale)}",
                evidence={
                    "max_stale_seconds": (
                        round(max_age, 2) if max_age != float("inf") else None
                    ),
                    "stale_symbols": stale,
                    "per_symbol": [s.to_dict() for s in per_symbol],
                },
            )
        return FeedReadinessGate(
            passed=True,
            reason="ok",
            evidence={
                "subscribed_count": len(symbols),
                "max_stale_seconds": max(
                    (s.age_seconds or 0.0) for s in per_symbol
                ),
            },
        )


__all__ = [
    "DEFAULT_HARD_THRESHOLD_SECONDS",
    "DEFAULT_WARN_THRESHOLD_SECONDS",
    "FeedReadinessGate",
    "FeedStalenessTracker",
    "SymbolStaleness",
]
