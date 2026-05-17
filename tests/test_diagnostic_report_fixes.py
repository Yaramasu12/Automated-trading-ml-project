from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from trading_platform.ai.feature_store import FeatureStore
from trading_platform.ai.features import FeatureSnapshot
from trading_platform.api.runtime import TradingRuntime
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.data.persistence import TradingDatabase
from trading_platform.domain.enums import ExecutionMode, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal, Trade
from trading_platform.portfolio.ledger import PortfolioSnapshot
from trading_platform.risk.capital_protection import CapitalProtection


def _future_intent(*, opens_position: bool = True) -> OrderIntent:
    instrument = build_default_universe().select_future("NIFTY", datetime.now(timezone.utc).date())
    signal = Signal(
        strategy_name="futures_trend",
        symbol=instrument.symbol,
        side=Side.BUY,
        confidence=0.8,
        price=24_000.0,
        reason="test",
        created_at=datetime.now(timezone.utc),
        metadata={"opens_position": opens_position},
    )
    return OrderIntent(
        signal=signal,
        instrument=instrument,
        quantity=1,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
    )


class CapitalProtectionDiagnosticFixTests(unittest.TestCase):
    def test_futures_use_margin_exposure_not_full_notional(self):
        snapshot = PortfolioSnapshot(
            cash=500_000.0,
            equity=1_000_000.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            drawdown=0.0,
            peak_equity=1_000_000.0,
            open_positions=0,
        )
        result = CapitalProtection(max_position_pct=0.05, max_futures_margin_pct=0.20).check(
            _future_intent(),
            snapshot,
        )
        self.assertTrue(result.approved, result.reason)
        self.assertAlmostEqual(result.required_capital, 24_000.0 * 50 * 0.12)

    def test_position_reducing_order_bypasses_drawdown_halt(self):
        snapshot = PortfolioSnapshot(
            cash=100_000.0,
            equity=898_000.0,
            realized_pnl=-100_000.0,
            unrealized_pnl=0.0,
            drawdown=0.101,
            peak_equity=1_000_000.0,
            open_positions=1,
        )
        result = CapitalProtection(drawdown_halt_pct=0.10).check(
            _future_intent(opens_position=False),
            snapshot,
        )
        self.assertTrue(result.approved, result.reason)


class PersistenceDiagnosticFixTests(unittest.TestCase):
    def test_test_trades_are_flagged_and_filtered_from_default_analytics(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = TradingDatabase(Path(tmp) / "trading.db")
            now = datetime.now(timezone.utc)
            db.save_trade(
                Trade("t1", "o1", "RELIANCE", Side.BUY, 1, 100.0, 0.0, now, "manual_preview"),
                execution_mode="PAPER",
            )
            db.save_trade(
                Trade("t2", "o2", "RELIANCE", Side.BUY, 1, 100.0, 0.0, now, "equity_momentum"),
                execution_mode="PAPER",
            )
            self.assertEqual(db.trade_count("PAPER"), 1)
            self.assertEqual(db.trade_count("PAPER", include_test=True), 2)
            self.assertEqual(len(db.trades(execution_mode="PAPER")), 1)
            self.assertEqual(len(db.trades(execution_mode="PAPER", include_test=True)), 2)


class RuntimeControlDiagnosticFixTests(unittest.TestCase):
    def test_kill_switch_activation_records_reason(self):
        runtime = TradingRuntime()
        payload = runtime.set_kill_switch(True, reason="diagnostic_test")
        self.assertTrue(payload["kill_switch_active"])
        events = runtime.db.recent_risk_events(limit=1)
        self.assertEqual(events[0]["event_type"], "kill_switch_activated")
        self.assertEqual(events[0]["reason"], "diagnostic_test")


class FeatureStoreRetentionTests(unittest.TestCase):
    def test_append_trims_oldest_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FeatureStore(Path(tmp), max_rows_per_symbol=3)
            for idx in range(5):
                store.append(
                    "NIFTY",
                    datetime(2026, 5, 1 + idx).date(),
                    FeatureSnapshot(
                        symbol="NIFTY",
                        close=100 + idx,
                        momentum_5=0.0,
                        momentum_20=0.0,
                        realized_volatility=0.01,
                        volume_ratio=1.0,
                        trend_strength=1.0,
                    ),
                    "TRENDING",
                )
            records = store.load("NIFTY")
            self.assertEqual(len(records), 3)
            self.assertEqual(records[0]["date"], "2026-05-03")


if __name__ == "__main__":
    unittest.main()
