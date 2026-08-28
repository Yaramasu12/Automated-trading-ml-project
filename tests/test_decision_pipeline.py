from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from trading_platform.agent.market_hours import now_ist
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.decision.pipeline import DecisionPipeline, MarketDataUnavailable
from trading_platform.domain.enums import ExecutionMode
from trading_platform.domain.models import MarketBar
from trading_platform.portfolio.ledger import PortfolioLedger
from trading_platform.risk.engine import RiskEngine
from trading_platform.strategies.factory import StrategyFactory


class DecisionPipelineTests(unittest.TestCase):
    def test_signal_scan_builds_features_regime_and_candidates(self):
        master = build_default_universe(date(2026, 1, 1))
        pipeline = DecisionPipeline(master, StrategyFactory(), RiskEngine(), PortfolioLedger(1_000_000))

        result = pipeline.scan(
            underlying="NIFTY",
            start=date(2026, 1, 1),
            days=30,
            execution_mode=ExecutionMode.BACKTEST,
            live_armed=False,
            kill_switch_active=False,
            strategy_names=["futures_trend", "defined_risk_option_spread"],
        )
        payload = result.to_dict()

        self.assertEqual(payload["underlying"], "NIFTY")
        self.assertIn("regime", payload)
        self.assertEqual(len(payload["candidates"]), 2)
        self.assertEqual(payload["submitted_orders"] if "submitted_orders" in payload else 0, 0)

    def test_live_scan_rejects_unarmed_candidates(self):
        master = build_default_universe(date(2026, 1, 1))
        pipeline = DecisionPipeline(master, StrategyFactory(), RiskEngine(), PortfolioLedger(1_000_000))

        result = pipeline.scan(
            underlying="RELIANCE",
            start=date(2026, 1, 1),
            days=30,
            execution_mode=ExecutionMode.LIVE,
            live_armed=False,
            kill_switch_active=False,
            strategy_names=["equity_momentum"],
        )
        candidates = result.to_dict()["candidates"]
        risk_reasons = [
            candidate["risk_decision"]["reason"]
            for candidate in candidates
            if candidate["risk_decision"] is not None
        ]

        self.assertIn("live_mode_not_armed", risk_reasons)


class FakeHistoryProvider:
    """Records which Instrument it was actually asked to fetch, so the test
    can assert _fetch_bars resolved the RIGHT contract before ever making a
    real API call."""

    def __init__(self, min_bars: int) -> None:
        self.requested_instruments: list = []
        self._min_bars = min_bars

    def get_candles(self, instrument, from_dt, to_dt, interval="ONE_DAY"):
        self.requested_instruments.append(instrument)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [
            MarketBar(
                timestamp=base + timedelta(days=i), symbol=instrument.symbol,
                open=100.0, high=101.0, low=99.0, close=100.5, volume=1000,
            )
            for i in range(self._min_bars + 5)
        ]


class FetchBarsCommodityFallbackTests(unittest.TestCase):
    """Regression test for the bug found via log monitoring 2026-07-28:
    _fetch_bars tried instrument_master.get("GOLD") then get("GOLD-EQ"),
    neither of which exists for an MCX commodity (bare NCDEX reference
    entries are filtered out at instrument-master parse time; there is no
    cash-equity listing either) — every commodity fell through to the
    synthetic-data path every single day. The real, only-valid instrument is
    the current front-month futures contract, resolved via select_future().
    """

    def test_commodity_underlying_resolves_via_select_future(self):
        # _fetch_bars calls now_ist().date() internally (not caller-supplied),
        # so the test universe's futures contracts must be anchored to the
        # same "today" or they've already expired by the time select_future
        # runs against the real current date.
        today = now_ist().date()
        master = build_default_universe(today)
        # build_default_universe adds a synthetic bare "GOLD" CASH-segment
        # entry as a convenience for OTHER tests ("so the scanner can look up
        # prices") — the real Angel One master has no such thing (the bare
        # NCDEX reference is filtered out entirely at parse time, being an
        # unsupported exchange). Remove it so this test exercises the actual
        # production fallback chain, not the test fixture's shortcut.
        del master.instruments["GOLD"]
        history = FakeHistoryProvider(min_bars=30)
        pipeline = DecisionPipeline(
            master, StrategyFactory(), RiskEngine(), PortfolioLedger(1_000_000),
            history_provider=history,
        )

        with patch.object(pipeline, "_load_bars_from_disk", return_value=None):
            bars = pipeline._fetch_bars("GOLD", today, 30)

        self.assertGreater(len(bars), 0)
        self.assertEqual(len(history.requested_instruments), 1)
        resolved = history.requested_instruments[0]
        # Must NOT be the bare "GOLD" or a fabricated "GOLD-EQ" — must be a
        # real, resolvable futures contract for the underlying.
        self.assertNotEqual(resolved.symbol, "GOLD")
        self.assertNotEqual(resolved.symbol, "GOLD-EQ")
        self.assertTrue(resolved.symbol.startswith("GOLD"))

    def test_equity_underlying_still_prefers_cash_eq(self):
        """Must not regress: an equity should still resolve to its cash-market
        instrument, not get redirected into the new futures fallback."""
        master = build_default_universe(date(2026, 1, 1))
        history = FakeHistoryProvider(min_bars=30)
        pipeline = DecisionPipeline(
            master, StrategyFactory(), RiskEngine(), PortfolioLedger(1_000_000),
            history_provider=history,
        )

        with patch.object(pipeline, "_load_bars_from_disk", return_value=None):
            bars = pipeline._fetch_bars("RELIANCE", date(2026, 1, 1), 30)

        self.assertGreater(len(bars), 0)
        resolved = history.requested_instruments[0]
        self.assertIn(resolved.symbol, ("RELIANCE", "RELIANCE-EQ"))


class FailingHistoryProvider:
    def get_candles(self, instrument, from_dt, to_dt, interval="ONE_DAY"):
        raise RuntimeError("candle API unavailable")


class BarsWereSyntheticTests(unittest.TestCase):
    """2026-08-22: _fetch_bars fails CLOSED, not synthetic, whenever a real
    history_provider is configured and every real-data attempt fails --
    raises MarketDataUnavailable instead of substituting fabricated bars, so
    a caller can never silently act on a signal built from fake data. Only
    when history_provider is None (explicit backtest/demo mode, no real
    broker configured) do synthetic bars remain the correct, intended return
    value -- covered by test_no_history_provider_is_flagged_synthetic below,
    which must keep passing unchanged.

    Also covers a previously-silent path: a rate-limit cooldown skipped the
    real fetch and fell through to synthetic bars with no exception raised at
    all -- now it fails closed exactly like the real-fetch-exception case."""

    def _pipeline(self, history_provider):
        master = build_default_universe(date(2026, 1, 1))
        return DecisionPipeline(
            master, StrategyFactory(), RiskEngine(), PortfolioLedger(1_000_000),
            history_provider=history_provider,
        )

    def test_real_fetch_is_not_flagged_synthetic(self):
        pipeline = self._pipeline(FakeHistoryProvider(min_bars=30))
        with patch.object(pipeline, "_load_bars_from_disk", return_value=None):
            pipeline._fetch_bars("RELIANCE", date(2026, 1, 1), 30)
        self.assertFalse(pipeline.bars_were_synthetic("RELIANCE"))

    def test_history_provider_failure_raises_instead_of_returning_synthetic(self):
        pipeline = self._pipeline(FailingHistoryProvider())
        with patch.object(pipeline, "_load_bars_from_disk", return_value=None):
            with self.assertRaises(MarketDataUnavailable):
                pipeline._fetch_bars("RELIANCE", date(2026, 1, 1), 30)

    def test_no_history_provider_is_flagged_synthetic(self):
        """The one case where synthetic bars are still the correct, intended
        return value: no real broker configured at all (backtest/demo mode).
        This must NOT raise and must NOT change."""
        pipeline = self._pipeline(None)
        pipeline._fetch_bars("RELIANCE", date(2026, 1, 1), 30)
        self.assertTrue(pipeline.bars_were_synthetic("RELIANCE"))

    def test_rate_limit_cooldown_raises_instead_of_falling_through_to_synthetic(self):
        """Regression: this branch previously returned synthetic bars with NO
        exception raised at all -- silently even quieter than the
        real-fetch-exception path. Now it must fail closed identically."""
        import time as _time
        pipeline = self._pipeline(FakeHistoryProvider(min_bars=30))
        pipeline._candle_cooldown_until = _time.monotonic() + 60.0
        with patch.object(pipeline, "_load_bars_from_disk", return_value=None):
            with self.assertRaises(MarketDataUnavailable):
                pipeline._fetch_bars("RELIANCE", date(2026, 1, 1), 30)

    def test_unknown_underlying_defaults_to_not_synthetic(self):
        """bars_were_synthetic() for an underlying never fetched must not
        read as a fallback -- it simply hasn't been asked about."""
        pipeline = self._pipeline(FakeHistoryProvider(min_bars=30))
        self.assertFalse(pipeline.bars_were_synthetic("NEVER_FETCHED"))


if __name__ == "__main__":
    unittest.main()
