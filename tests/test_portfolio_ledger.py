"""Tests for PortfolioLedger's realized_pnl tracking.

Found 2026-09-08 via TradingQA's own recommendations report (rank #1
finding): /portfolio/positions reported realized_pnl=0.0 while
/portfolio/target-progress (AnnualTargetTracker, computed independently as
current_equity - start_capital) correctly showed -Rs 12,641.58 -- a live,
Rs 12,641 discrepancy between two figures that are supposed to be the same
number. Root cause: mark_to_market() computed the top-level realized_pnl by
summing position.realized_pnl across self.positions.values(), but
runtime.py's restore_state() only re-populates self.positions from
load_positions()'s "WHERE quantity != 0" query -- a fully CLOSED position
(entry, then exit -- the common case) is never restored, so its
accumulated realized_pnl silently vanished from the sum on every restart,
even though `cash` (restored directly from the persisted snapshot, not
reconstructed from position objects) correctly kept the loss.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from trading_platform.domain.enums import (
    AssetClass, Exchange, InstrumentType, OrderPriority, OrderType,
    ProductType, Segment, Side,
)
from trading_platform.domain.models import Instrument, Order, OrderIntent, Signal
from trading_platform.portfolio.ledger import PortfolioLedger


def _instrument(symbol: str = "TEST") -> Instrument:
    return Instrument(
        symbol=symbol, name=symbol, exchange=Exchange.NSE, segment=Segment.CASH,
        asset_class=AssetClass.EQUITY, instrument_type=InstrumentType.EQUITY,
        token="X", lot_size=1,
    )


def _order(symbol: str, side: Side, quantity: int, price: float) -> Order:
    signal = Signal(
        strategy_name="test", symbol=symbol, side=side, confidence=0.8, price=price,
        reason="test", created_at=datetime.now(timezone.utc),
    )
    intent = OrderIntent(
        signal=signal, instrument=_instrument(symbol), quantity=quantity,
        order_type=OrderType.MARKET, product_type=ProductType.INTRADAY,
        priority=OrderPriority.ENTRY,
    )
    return Order(intent=intent)


class ApplyFillRealizedPnlTests(unittest.TestCase):
    def test_ledger_wide_realized_pnl_accumulates_on_a_closing_fill(self):
        ledger = PortfolioLedger(initial_capital=1_000_000)
        now = datetime.now(timezone.utc)

        ledger.apply_fill(_order("TEST", Side.BUY, 10, 100.0), fill_price=100.0, timestamp=now)
        self.assertEqual(ledger.realized_pnl, 0.0)  # opening a position realizes nothing

        # Close at a loss: sell 10 @ 90 after buying 10 @ 100 -> -100 realized.
        ledger.apply_fill(_order("TEST", Side.SELL, 10, 90.0), fill_price=90.0, timestamp=now)
        self.assertAlmostEqual(ledger.realized_pnl, -100.0)

    def test_realized_pnl_survives_even_after_the_position_fully_closes(self):
        # This is the exact bug: mark_to_market() must not derive its
        # top-level realized_pnl by re-summing self.positions.values(),
        # because a fully-closed position (quantity=0) is exactly what
        # restore_state() drops on the next restart (load_positions()'s own
        # "WHERE quantity != 0" filter). self.realized_pnl must be the
        # ledger-wide total, independent of what still happens to be in
        # self.positions right now.
        ledger = PortfolioLedger(initial_capital=1_000_000)
        now = datetime.now(timezone.utc)
        ledger.apply_fill(_order("TEST", Side.BUY, 10, 100.0), fill_price=100.0, timestamp=now)
        ledger.apply_fill(_order("TEST", Side.SELL, 10, 90.0), fill_price=90.0, timestamp=now)

        # Simulate what restore_state() actually does after a restart: it
        # never re-adds a fully-closed position, so self.positions is empty
        # even though real money was lost.
        ledger.positions.clear()

        snapshot = ledger.mark_to_market(now, {})
        self.assertAlmostEqual(snapshot.realized_pnl, -100.0)

    def test_restore_pattern_matches_cash_not_position_reconstruction(self):
        """Mirrors runtime.py's restore_state(): cash and realized_pnl are
        both restored DIRECTLY from the persisted snapshot dict, not
        reconstructed from whatever happens to still be in self.positions —
        this is the fix's whole point, so assert it explicitly rather than
        only via mark_to_market's derived output."""
        persisted_snapshot = {"cash": 987_924.01, "equity": 987_358.41, "realized_pnl": -12_641.58}

        ledger = PortfolioLedger(initial_capital=1_000_000)
        # As restore_state() does:
        ledger.cash = persisted_snapshot["cash"]
        ledger.realized_pnl = persisted_snapshot.get("realized_pnl", 0.0) or 0.0

        snapshot = ledger.mark_to_market(datetime.now(timezone.utc), {})
        self.assertAlmostEqual(snapshot.realized_pnl, -12_641.58)

    def test_partial_close_realizes_only_the_closed_portion(self):
        ledger = PortfolioLedger(initial_capital=1_000_000)
        now = datetime.now(timezone.utc)
        ledger.apply_fill(_order("TEST", Side.BUY, 10, 100.0), fill_price=100.0, timestamp=now)
        ledger.apply_fill(_order("TEST", Side.SELL, 4, 110.0), fill_price=110.0, timestamp=now)

        # 4 units closed @ +10 profit each = +40; 6 units remain open.
        self.assertAlmostEqual(ledger.realized_pnl, 40.0)
        self.assertEqual(ledger.positions["TEST"].quantity, 6)

    def test_multiple_round_trips_accumulate_correctly(self):
        ledger = PortfolioLedger(initial_capital=1_000_000)
        now = datetime.now(timezone.utc)
        # Round trip 1: +50
        ledger.apply_fill(_order("TEST", Side.BUY, 10, 100.0), fill_price=100.0, timestamp=now)
        ledger.apply_fill(_order("TEST", Side.SELL, 10, 105.0), fill_price=105.0, timestamp=now)
        # Round trip 2: -30
        ledger.apply_fill(_order("TEST", Side.BUY, 10, 100.0), fill_price=100.0, timestamp=now)
        ledger.apply_fill(_order("TEST", Side.SELL, 10, 97.0), fill_price=97.0, timestamp=now)

        self.assertAlmostEqual(ledger.realized_pnl, 50.0 - 30.0)


if __name__ == "__main__":
    unittest.main()
