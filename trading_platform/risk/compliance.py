from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trading_platform.agent.market_hours import IST
from trading_platform.domain.models import OrderIntent


@dataclass
class ComplianceResult:
    approved: bool
    reason: str
    checked_at: str


# Strategy names used by test harnesses, diagnostics, and manual approval previews.
# Orders from these strategies are exempt from the daily order cap so they never
# exhaust the production budget that real trading signals need.
_TEST_STRATEGY_NAMES: frozenset[str] = frozenset({
    "manual_preview",
    "trace_fill_test",
    "manual_approval_test",
    "momentum_v1",      # seeded MARL stub policy — not a real production strategy
    "noop_baseline",
})


class ComplianceGuard:
    """Pre-submission compliance checks: position limits, duplicate detection,
    banned symbols, and intraday order caps.

    Runs before capital-protection and risk-engine checks so we never waste
    broker API calls on non-compliant orders.
    """

    def __init__(
        self,
        max_orders_per_day: int = 200,
        max_qty_per_order: int = 5000,
        banned_symbols: set[str] | None = None,
    ) -> None:
        self.max_orders_per_day = max_orders_per_day
        self.max_qty_per_order = max_qty_per_order
        self.banned_symbols: set[str] = banned_symbols or set()
        self._orders_today = 0
        self._last_reset_date: str = ""

    def check(self, intent: OrderIntent) -> ComplianceResult:
        now = datetime.now(timezone.utc)
        # Use IST date for the daily reset — the NSE/MCX trading day runs
        # 09:15–23:30 IST. Resetting on UTC midnight (05:30 IST) would fire
        # mid-session and could exhaust the daily cap before the day ends.
        today = datetime.now(IST).date().isoformat()

        if today != self._last_reset_date:
            self._orders_today = 0
            self._last_reset_date = today

        if intent.instrument.symbol in self.banned_symbols:
            return ComplianceResult(
                approved=False,
                reason=f"Symbol {intent.instrument.symbol} is on the banned list",
                checked_at=now.isoformat(),
            )

        if intent.quantity <= 0:
            return ComplianceResult(
                approved=False,
                reason=f"Invalid quantity: {intent.quantity}",
                checked_at=now.isoformat(),
            )

        if intent.quantity > self.max_qty_per_order:
            return ComplianceResult(
                approved=False,
                reason=f"Quantity {intent.quantity} exceeds max {self.max_qty_per_order}",
                checked_at=now.isoformat(),
            )

        # Test/diagnostic strategies are exempt from the daily cap so they never
        # exhaust the budget available for real production signals.
        is_test = intent.signal.strategy_name in _TEST_STRATEGY_NAMES
        if not is_test and self._orders_today >= self.max_orders_per_day:
            return ComplianceResult(
                approved=False,
                reason=f"Daily order cap reached ({self._orders_today}/{self.max_orders_per_day})",
                checked_at=now.isoformat(),
            )

        if not is_test:
            self._orders_today += 1
        return ComplianceResult(approved=True, reason="OK", checked_at=now.isoformat())

    def ban_symbol(self, symbol: str) -> None:
        self.banned_symbols.add(symbol)

    def unban_symbol(self, symbol: str) -> None:
        self.banned_symbols.discard(symbol)

    @property
    def orders_today(self) -> int:
        return self._orders_today
