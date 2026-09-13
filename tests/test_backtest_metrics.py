"""Regression tests for audit_results/2026-09-13/AUDIT_REPORT.md finding #4
(high): Trade.quantity is in LOTS (matching PortfolioLedger.apply_fill's own
accounting), but backtesting/metrics.py::round_trip_pnls multiplied
price-change directly by quantity with no contract multiplier — a
reproduced one-lot trade (lot_size 50, price +10) produced ₹500 ledger
profit but ₹10 in round_trip_pnls, able to flip a trade's sign after costs
and corrupt win rate/profit factor.
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from trading_platform.backtesting.metrics import round_trip_pnls
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.data.persistence import TradingDatabase
from trading_platform.domain.enums import OrderStatus, OrderType, ProductType, Side
from trading_platform.domain.models import Order
from trading_platform.portfolio.ledger import PortfolioLedger

_MASTER = build_default_universe(date(2026, 1, 5))


class LotSizeRoundTripPnlTests(unittest.TestCase):
    def test_reproduces_and_fixes_the_audit_scenario(self):
        # The audit's own probes.py: one lot, lot_size 50, price +10.
        # Before the fix: ledger PnL 500, round_trip_pnls output [10.0].
        intent = replace(
            _intent("RELIANCE", Side.BUY, 1, 100.0),
            instrument=replace(_MASTER.get("RELIANCE"), lot_size=50),
        )
        ledger = PortfolioLedger(1_000_000)
        ledger.apply_fill(Order(intent, status=OrderStatus.FILLED), 100.0, _now(), 0.0)
        sell = replace(intent, signal=replace(intent.signal, side=Side.SELL))
        ledger.apply_fill(Order(sell, status=OrderStatus.FILLED), 110.0, _now(), 0.0)

        actual_ledger_pnl = ledger.cash - 1_000_000
        self.assertEqual(actual_ledger_pnl, 500.0)

        pnls = round_trip_pnls(list(ledger.trades))
        self.assertEqual(pnls, [500.0])
        self.assertEqual(sum(pnls), actual_ledger_pnl)

    def test_equity_lot_size_one_is_unaffected(self):
        # Default lot_size=1 (equities) must behave exactly as before.
        intent = _intent("RELIANCE", Side.BUY, 10, 100.0)
        ledger = PortfolioLedger(1_000_000)
        ledger.apply_fill(Order(intent, status=OrderStatus.FILLED), 100.0, _now(), 0.0)
        sell = replace(intent, signal=replace(intent.signal, side=Side.SELL))
        ledger.apply_fill(Order(sell, status=OrderStatus.FILLED), 110.0, _now(), 0.0)

        pnls = round_trip_pnls(list(ledger.trades))
        self.assertEqual(pnls, [100.0])  # 10 shares * ₹10 gain
        self.assertEqual(sum(pnls), ledger.cash - 1_000_000)

    def test_charges_are_not_multiplied_by_lot_size(self):
        # Charges are already in rupees on the Trade (not per-lot) -- only
        # the price-change component gets the multiplier.
        intent = replace(
            _intent("RELIANCE", Side.BUY, 1, 100.0),
            instrument=replace(_MASTER.get("RELIANCE"), lot_size=50),
        )
        ledger = PortfolioLedger(1_000_000)
        ledger.apply_fill(Order(intent, status=OrderStatus.FILLED), 100.0, _now(), 10.0)
        sell = replace(intent, signal=replace(intent.signal, side=Side.SELL))
        ledger.apply_fill(Order(sell, status=OrderStatus.FILLED), 110.0, _now(), 10.0)

        pnls = round_trip_pnls(list(ledger.trades))
        # (110-100)*50 - 10 - 10 = 480, not 500-1000 or some multiplied charge figure.
        self.assertEqual(pnls, [480.0])

    def test_lot_size_persists_through_save_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = TradingDatabase(Path(tmp) / "trading.db")
            try:
                intent = replace(
                    _intent("RELIANCE", Side.BUY, 1, 100.0),
                    instrument=replace(_MASTER.get("RELIANCE"), lot_size=50),
                )
                ledger = PortfolioLedger(1_000_000)
                trade = ledger.apply_fill(Order(intent, status=OrderStatus.FILLED), 100.0, _now(), 0.0)
                self.assertEqual(trade.lot_size, 50)

                db.save_trade(trade, execution_mode="PAPER")
                rows = db.trades(execution_mode="PAPER")
                self.assertEqual(rows[0]["lot_size"], 50)
            finally:
                db.close()

    def test_lot_size_defaults_to_one_for_legacy_rows(self):
        # A trade constructed the old way (no lot_size kwarg) must still
        # behave as lot_size=1 -- backward compatible with every existing
        # call site that never dealt with a derivative multiplier.
        from trading_platform.domain.models import Trade
        trade = Trade("t1", "o1", "RELIANCE", Side.BUY, 1, 100.0, 0.0, _now(), "test")
        self.assertEqual(trade.lot_size, 1)


def _intent(symbol: str, side: Side, quantity: int, price: float):
    from trading_platform.domain.models import OrderIntent, Signal
    signal = Signal("test_strategy", symbol, side, 0.9, price, "test", _now(), metadata={})
    return OrderIntent(signal, _MASTER.get(symbol), quantity, OrderType.MARKET, ProductType.INTRADAY)


def _now() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    unittest.main()
