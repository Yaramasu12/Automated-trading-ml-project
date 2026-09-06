"""Regression test for the exit_reason column added to trades (TradingQA Phase 1).

Exit-reason attribution didn't exist as queryable data before this: ExitPlan
correctly computed which trigger fired, but that value was never persisted past
the in-memory object, so no report could ever compute a real stop-loss-hit-rate.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from trading_platform.data.persistence import TradingDatabase
from trading_platform.domain.enums import Side
from trading_platform.domain.models import Trade


class TestExitReasonPersistence(unittest.TestCase):
    def test_closed_trade_round_trips_with_exit_reason(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            db = TradingDatabase(Path(tmp) / "trading.db")
            # Windows cannot unlink a file that still has an open handle, so the
            # connection has to go before TemporaryDirectory removes the directory.
            try:
                db.save_trade(
                    Trade("t-entry", "o1", "RELIANCE", Side.BUY, 1, 100.0, 0.0, now, "equity_momentum"),
                    execution_mode="PAPER",
                )
                db.save_trade(
                    Trade("t-exit", "o2", "RELIANCE", Side.SELL, 1, 103.8, 0.0, now, "equity_momentum"),
                    execution_mode="PAPER",
                    exit_reason="TARGET",
                )
                rows = {r["trade_id"]: r for r in db.trades(symbol="RELIANCE", execution_mode="PAPER")}
                self.assertIsNone(rows["t-entry"]["exit_reason"])
                self.assertEqual(rows["t-exit"]["exit_reason"], "TARGET")
            finally:
                db.close()

    def test_exit_reason_defaults_to_none_when_omitted(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            db = TradingDatabase(Path(tmp) / "trading.db")
            try:
                db.save_trade(
                    Trade("t1", "o1", "TCS", Side.BUY, 1, 100.0, 0.0, now, "manual_preview"),
                )
                rows = db.trades(symbol="TCS", include_test=True)
                self.assertEqual(len(rows), 1)
                self.assertIsNone(rows[0]["exit_reason"])
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
