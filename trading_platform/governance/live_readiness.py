"""Phase-1 live-readiness aggregator.

Seven gates, evaluated in order. Returns a structured payload with
per-gate evidence. The runtime's ``arm_live(True)`` consults the
aggregator and refuses to arm unless every gate passes.

The seven gates are:

1. ``execution_mode``           — runtime mode is LIVE / LIVE_*
2. ``live_trading_flag``        — settings.live_trading_enabled
3. ``real_money_confirmation``  — operator typed the magic phrase
4. ``broker_credentials``       — Angel One creds + TOTP configured
5. ``kill_switch``              — kill switch is *not* latched
6. ``instrument_freshness``     — real refresh in last N hours, source ≠ synthetic
7. ``feed_staleness``           — feed running; every subscribed symbol ticked recently

Design rule: every gate produces a ``GateResult`` even when an earlier
one fails. Operators want the full punch list, not the first
roadblock.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from trading_platform.data.feed_staleness import FeedStalenessTracker


# Default: 12 h max-age on the instrument master.
# Angel One publishes the master at start-of-day. A 12 h window covers
# the trading session plus the morning refresh.
DEFAULT_INSTRUMENT_MAX_AGE_SECONDS = 43_200.0

LIVE_MODE_PREFIX = "LIVE"

REAL_MONEY_PHRASE = "I_ACCEPT_REAL_MONEY_LIVE_ORDERS"


# ──────────────────────────────────────────────────────────────────────
# InstrumentFreshnessTracker
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _FreshnessRecord:
    refreshed_at: datetime
    source: str
    parsed_count: int
    is_synthetic: bool


class InstrumentFreshnessTracker:
    """Records when the instrument master was last refreshed and from
    where. Used by the ``instrument_freshness`` gate.

    The tracker is process-local; restarts forget freshness, which is
    intentional — a fresh process should be forced to refresh before
    arming live.
    """

    def __init__(
        self,
        max_age_seconds: float = DEFAULT_INSTRUMENT_MAX_AGE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.max_age_seconds = float(max_age_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._record: _FreshnessRecord | None = None
        self._lock = threading.Lock()

    def record_refresh(
        self,
        *,
        source: str,
        parsed_count: int,
        when: datetime | None = None,
    ) -> None:
        ts = when or self._clock()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        is_synthetic = source.lower() == "synthetic" or parsed_count <= 0
        with self._lock:
            self._record = _FreshnessRecord(
                refreshed_at=ts,
                source=source,
                parsed_count=int(parsed_count),
                is_synthetic=is_synthetic,
            )

    def mark_synthetic(self, parsed_count: int = 0) -> None:
        """Convenience for the boot path — tells the tracker the
        current master is the synthetic universe.
        """
        self.record_refresh(source="synthetic", parsed_count=parsed_count)

    def reset(self) -> None:
        with self._lock:
            self._record = None

    # ── Inspection ───────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        with self._lock:
            record = self._record
        if record is None:
            return {
                "is_synthetic": True,
                "last_refresh_at": None,
                "source": None,
                "parsed_count": 0,
                "max_age_seconds": self.max_age_seconds,
                "age_seconds": None,
                "is_stale": True,
            }
        age = (self._clock() - record.refreshed_at).total_seconds()
        return {
            "is_synthetic": record.is_synthetic,
            "last_refresh_at": record.refreshed_at.isoformat(),
            "source": record.source,
            "parsed_count": record.parsed_count,
            "max_age_seconds": self.max_age_seconds,
            "age_seconds": round(age, 2),
            "is_stale": age > self.max_age_seconds,
        }

    def gate(self) -> "GateResult":
        status = self.status()
        if status["last_refresh_at"] is None:
            return GateResult(
                name="instrument_freshness",
                passed=False,
                reason="never_refreshed",
                evidence=status,
            )
        if status["is_synthetic"]:
            return GateResult(
                name="instrument_freshness",
                passed=False,
                reason="instrument_master_is_synthetic",
                evidence=status,
            )
        if status["is_stale"]:
            return GateResult(
                name="instrument_freshness",
                passed=False,
                reason=f"instrument_master_stale:{int(status['age_seconds'])}s",
                evidence=status,
            )
        if status["parsed_count"] <= 0:
            return GateResult(
                name="instrument_freshness",
                passed=False,
                reason="instrument_master_empty",
                evidence=status,
            )
        return GateResult(
            name="instrument_freshness", passed=True, reason="ok", evidence=status
        )


# ──────────────────────────────────────────────────────────────────────
# Gate result + readiness payload
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class LiveReadiness:
    armed_eligible: bool
    blocking_reasons: tuple[str, ...]
    evaluated_at: datetime
    gates: tuple[GateResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "armed_eligible": self.armed_eligible,
            "blocking_reasons": list(self.blocking_reasons),
            "evaluated_at": self.evaluated_at.isoformat(),
            "gates": [g.to_dict() for g in self.gates],
        }


# ──────────────────────────────────────────────────────────────────────
# Aggregator
# ──────────────────────────────────────────────────────────────────────


class LiveReadinessAggregator:
    """Composes the seven gates into one ``LiveReadiness`` payload.

    The aggregator never touches global state — it asks small,
    explicit accessors. That makes it easy to test (inject a fake
    settings object, fake feed snapshot, fake freshness tracker) and
    keeps the runtime free to wire it however it likes.
    """

    def __init__(
        self,
        *,
        instrument_freshness: InstrumentFreshnessTracker,
        feed_staleness: FeedStalenessTracker,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.instrument_freshness = instrument_freshness
        self.feed_staleness = feed_staleness
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ── Individual gates ─────────────────────────────────────────────

    def _gate_execution_mode(self, execution_mode: str) -> GateResult:
        if execution_mode and execution_mode.upper().startswith(LIVE_MODE_PREFIX):
            return GateResult("execution_mode", True, "ok",
                              {"execution_mode": execution_mode})
        return GateResult(
            "execution_mode",
            False,
            "execution_mode_not_live",
            {"execution_mode": execution_mode},
        )

    def _gate_live_flag(self, live_trading_enabled: bool) -> GateResult:
        if live_trading_enabled:
            return GateResult("live_trading_flag", True, "ok",
                              {"live_trading_enabled": True})
        return GateResult(
            "live_trading_flag",
            False,
            "live_trading_flag_off",
            {"live_trading_enabled": False},
        )

    def _gate_confirmation(self, phrase: str) -> GateResult:
        if (phrase or "") == REAL_MONEY_PHRASE:
            return GateResult("real_money_confirmation", True, "ok", {})
        return GateResult(
            "real_money_confirmation",
            False,
            "confirmation_phrase_missing",
            {"expected": REAL_MONEY_PHRASE},
        )

    def _gate_credentials(self, broker_configured: bool, broker_name: str) -> GateResult:
        if broker_configured:
            return GateResult("broker_credentials", True, "ok",
                              {"broker": broker_name, "configured": True})
        return GateResult(
            "broker_credentials",
            False,
            "broker_credentials_missing",
            {"broker": broker_name, "configured": False},
        )

    def _gate_kill_switch(self, kill_switch_active: bool) -> GateResult:
        if not kill_switch_active:
            return GateResult("kill_switch", True, "ok", {"active": False})
        return GateResult(
            "kill_switch",
            False,
            "kill_switch_active",
            {"active": True},
        )

    def _gate_instrument_freshness(self) -> GateResult:
        return self.instrument_freshness.gate()

    def _gate_feed_staleness(
        self,
        *,
        feed_running: bool,
        subscribed_symbols: Iterable[str],
    ) -> GateResult:
        gate = self.feed_staleness.gate(subscribed_symbols, feed_running=feed_running)
        return GateResult(
            "feed_staleness",
            passed=gate.passed,
            reason=gate.reason,
            evidence=gate.evidence,
        )

    # ── Aggregation ──────────────────────────────────────────────────

    def evaluate(
        self,
        *,
        execution_mode: str,
        live_trading_enabled: bool,
        real_money_confirmation: str,
        broker_configured: bool,
        broker_name: str,
        kill_switch_active: bool,
        feed_running: bool,
        subscribed_symbols: Iterable[str],
    ) -> LiveReadiness:
        gates: tuple[GateResult, ...] = (
            self._gate_execution_mode(execution_mode),
            self._gate_live_flag(live_trading_enabled),
            self._gate_confirmation(real_money_confirmation),
            self._gate_credentials(broker_configured, broker_name),
            self._gate_kill_switch(kill_switch_active),
            self._gate_instrument_freshness(),
            self._gate_feed_staleness(
                feed_running=feed_running,
                subscribed_symbols=subscribed_symbols,
            ),
        )
        blocking = tuple(g.reason for g in gates if not g.passed)
        return LiveReadiness(
            armed_eligible=not blocking,
            blocking_reasons=blocking,
            evaluated_at=self._clock(),
            gates=gates,
        )


__all__ = [
    "DEFAULT_INSTRUMENT_MAX_AGE_SECONDS",
    "REAL_MONEY_PHRASE",
    "GateResult",
    "InstrumentFreshnessTracker",
    "LiveReadiness",
    "LiveReadinessAggregator",
]
