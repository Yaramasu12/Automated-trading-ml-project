"""Unit tests for the daily options-chain forward-collection job."""
from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from trading_platform.data.options_chain_collector import OptionsChainCollector
from trading_platform.domain.enums import (
    AssetClass, Exchange, InstrumentType, OptionType, Segment,
)
from trading_platform.domain.models import Instrument, MarketBar


def _option(strike, ot, expiry, underlying="NIFTY"):
    return Instrument(
        symbol=f"{underlying}{int(strike)}{ot.value}", name=underlying, exchange=Exchange.NFO,
        segment=Segment.OPTIONS, asset_class=AssetClass.INDEX, instrument_type=InstrumentType.OPTION,
        token="1", lot_size=50, tick_size=0.05, expiry=expiry, strike=strike, option_type=ot,
        underlying=underlying,
    )


def _bar(close):
    from datetime import datetime, timezone
    return MarketBar(timestamp=datetime.now(timezone.utc), symbol="X", open=close, high=close,
                      low=close, close=close, volume=100)


class FakeHistory:
    """Maps instrument.symbol -> close price; raises for unmapped symbols."""

    def __init__(self, prices: dict[str, float], fail_symbols: set[str] | None = None):
        self.prices = prices
        self.fail_symbols = fail_symbols or set()
        self.calls: list[str] = []

    def get_candles(self, instrument, from_dt, to_dt, interval="ONE_DAY"):
        self.calls.append(instrument.symbol)
        if instrument.symbol in self.fail_symbols:
            raise RuntimeError("simulated candle fetch failure")
        price = self.prices.get(instrument.symbol)
        if price is None:
            return []
        return [_bar(price)]


class FakeDecisionPipeline:
    """Spot-price fallback path when no live tick is available — mirrors
    ShortVolExecutor's own use of decision_pipeline._fetch_bars()."""

    def __init__(self, closes: dict[str, float]):
        self.closes = closes

    def _fetch_bars(self, underlying, start, days):
        price = self.closes.get(underlying)
        return [_bar(price)] if price else []


class OptionsChainCollectorTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _runtime(self, opts, expiries, prices, fail_symbols=None, spot=24000.0, live_tick=True,
                 fallback_closes=None):
        master = SimpleNamespace(
            expiries=lambda u, seg=None: list(expiries),
            by_underlying=lambda u, seg=None: opts,
        )
        history = FakeHistory(prices, fail_symbols)
        live_feed = SimpleNamespace(
            latest_tick=lambda u: (SimpleNamespace(last_price=spot) if live_tick else None)
        )
        pipeline = FakeDecisionPipeline(fallback_closes or {})
        return SimpleNamespace(instrument_master=master, angel_one_history=history,
                               live_feed=live_feed, decision_pipeline=pipeline)

    def test_capture_writes_rows_for_strikes_around_spot(self):
        expiry = date.today() + timedelta(days=3)
        strikes = [23800, 23900, 24000, 24100, 24200]
        opts = [_option(s, ot, expiry) for s in strikes for ot in (OptionType.CE, OptionType.PE)]
        prices = {f"NIFTY{int(s)}{ot.value}": 100.0 for s in strikes for ot in (OptionType.CE, OptionType.PE)}
        runtime = self._runtime(opts, [expiry], prices)
        collector = OptionsChainCollector(runtime, out_dir=str(self.out_dir))

        result = collector.capture("NIFTY", strikes_each_side=2)

        self.assertEqual(result["rows"], 10)  # 5 strikes x 2 types
        path = self.out_dir / "NIFTY_chain_history.csv"
        self.assertTrue(path.exists())
        with path.open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["underlying"], "NIFTY")
        self.assertEqual(rows[0]["date"], date.today().isoformat())
        # IV should have been computed (non-empty) for a sane ATM price.
        self.assertTrue(any(r["iv"] != "" for r in rows))

    def test_capture_skips_symbol_that_fails_without_losing_the_rest(self):
        expiry = date.today() + timedelta(days=3)
        strikes = [24000, 24100]
        opts = [_option(s, ot, expiry) for s in strikes for ot in (OptionType.CE, OptionType.PE)]
        prices = {f"NIFTY{int(s)}{ot.value}": 100.0 for s in strikes for ot in (OptionType.CE, OptionType.PE)}
        runtime = self._runtime(opts, [expiry], prices, fail_symbols={"NIFTY24000CE"})
        collector = OptionsChainCollector(runtime, out_dir=str(self.out_dir))

        result = collector.capture("NIFTY", strikes_each_side=1)

        self.assertEqual(result["rows"], 3)  # 4 candidates minus the 1 that raised

    def test_capture_reports_error_when_no_upcoming_expiry(self):
        runtime = self._runtime([], [], {})
        collector = OptionsChainCollector(runtime, out_dir=str(self.out_dir))

        result = collector.capture("NIFTY")

        self.assertEqual(result["rows"], 0)
        self.assertIn("error", result)

    def test_capture_falls_back_to_decision_pipeline_when_no_live_tick(self):
        expiry = date.today() + timedelta(days=3)
        opts = [_option(24000, OptionType.CE, expiry)]
        prices = {"NIFTY24000CE": 100.0}
        runtime = self._runtime(opts, [expiry], prices, live_tick=False,
                                fallback_closes={"NIFTY": 24000.0})
        collector = OptionsChainCollector(runtime, out_dir=str(self.out_dir))

        result = collector.capture("NIFTY", strikes_each_side=0)

        self.assertEqual(result["rows"], 1)

    def test_capture_reports_error_when_no_spot_available(self):
        expiry = date.today() + timedelta(days=3)
        opts = [_option(24000, OptionType.CE, expiry)]
        runtime = self._runtime(opts, [expiry], {}, live_tick=False, fallback_closes={})

        collector = OptionsChainCollector(runtime, out_dir=str(self.out_dir))

        result = collector.capture("NIFTY")

        self.assertEqual(result["rows"], 0)
        self.assertIn("error", result)

    def test_status_reports_accumulated_days_across_captures(self):
        expiry = date.today() + timedelta(days=3)
        opts = [_option(24000, OptionType.CE, expiry), _option(24000, OptionType.PE, expiry)]
        prices = {"NIFTY24000CE": 100.0, "NIFTY24000PE": 100.0}
        runtime = self._runtime(opts, [expiry], prices)
        collector = OptionsChainCollector(runtime, out_dir=str(self.out_dir))

        collector.capture("NIFTY", strikes_each_side=0)
        status = collector.status()

        self.assertIn("NIFTY", status)
        self.assertEqual(status["NIFTY"]["trading_days_captured"], 1)
        self.assertEqual(status["NIFTY"]["total_rows"], 2)
        self.assertEqual(status["NIFTY"]["first_date"], date.today().isoformat())

    def test_status_empty_when_nothing_captured_yet(self):
        collector = OptionsChainCollector(SimpleNamespace(), out_dir=str(self.out_dir))
        self.assertEqual(collector.status(), {})


class AtmIvHistoryTests(unittest.TestCase):
    """atm_iv_history() feeds derivatives.engine.compute_iv_rank's lookback
    series for underlyings without a published VIX-like index."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_rows(self, underlying: str, rows: list[dict]) -> None:
        collector = OptionsChainCollector(SimpleNamespace(), out_dir=str(self.out_dir))
        collector._append_csv(underlying, rows)

    def _row(self, d, strike, otype, spot, iv):
        return {"date": d, "underlying": "NIFTY", "expiry": "2100-01-01", "dte": 5,
                "option_type": otype, "strike": strike, "spot": spot, "ltp": 100.0, "iv": iv, "delta": 0.5}

    def test_no_file_yet_returns_empty(self):
        collector = OptionsChainCollector(SimpleNamespace(), out_dir=str(self.out_dir))
        self.assertEqual(collector.atm_iv_history("NIFTY"), [])

    def test_averages_ce_and_pe_at_nearest_strike_per_date(self):
        # Two strikes captured; spot=24000 is nearest to strike 24000, not 23900.
        self._write_rows("NIFTY", [
            self._row("2026-08-01", 23900, "CE", 24000, 0.14),
            self._row("2026-08-01", 24000, "CE", 24000, 0.15),
            self._row("2026-08-01", 24000, "PE", 24000, 0.17),
        ])
        collector = OptionsChainCollector(SimpleNamespace(), out_dir=str(self.out_dir))

        history = collector.atm_iv_history("NIFTY")

        self.assertEqual(len(history), 1)
        self.assertAlmostEqual(history[0], 16.0)  # avg(0.15, 0.17) * 100, 23900 excluded

    def test_one_entry_per_trading_day_across_multiple_dates(self):
        self._write_rows("NIFTY", [
            self._row("2026-08-01", 24000, "CE", 24000, 0.15),
            self._row("2026-08-02", 24100, "CE", 24100, 0.16),
            self._row("2026-08-03", 24200, "CE", 24200, 0.14),
        ])
        collector = OptionsChainCollector(SimpleNamespace(), out_dir=str(self.out_dir))

        history = collector.atm_iv_history("NIFTY")

        self.assertEqual(len(history), 3)

    def test_lookback_days_limits_to_most_recent(self):
        rows = [self._row(f"2026-08-{d:02d}", 24000, "CE", 24000, 0.15) for d in range(1, 11)]
        self._write_rows("NIFTY", rows)
        collector = OptionsChainCollector(SimpleNamespace(), out_dir=str(self.out_dir))

        history = collector.atm_iv_history("NIFTY", lookback_days=3)

        self.assertEqual(len(history), 3)

    def test_rows_missing_iv_are_skipped_not_treated_as_zero(self):
        self._write_rows("NIFTY", [
            self._row("2026-08-01", 24000, "CE", 24000, ""),   # IV computation failed for this leg
            self._row("2026-08-01", 24000, "PE", 24000, 0.15),
        ])
        collector = OptionsChainCollector(SimpleNamespace(), out_dir=str(self.out_dir))

        history = collector.atm_iv_history("NIFTY")

        self.assertEqual(len(history), 1)
        self.assertAlmostEqual(history[0], 15.0)  # only the PE leg counted


class ChainSnapshotWindowTests(unittest.TestCase):
    def test_window_is_before_entry_cutoff_and_eod_squareoff(self):
        from datetime import datetime
        from trading_platform.agent.market_hours import (
            IST, is_chain_snapshot_window, is_eod_squareoff,
        )
        trading_day = date(2026, 7, 27)  # Monday
        inside = datetime(trading_day.year, trading_day.month, trading_day.day, 15, 15, tzinfo=IST)
        before = datetime(trading_day.year, trading_day.month, trading_day.day, 15, 5, tzinfo=IST)
        after = datetime(trading_day.year, trading_day.month, trading_day.day, 15, 20, tzinfo=IST)

        self.assertTrue(is_chain_snapshot_window(inside))
        self.assertFalse(is_chain_snapshot_window(before))
        self.assertFalse(is_chain_snapshot_window(after))
        # Must never overlap the EOD square-off window — they gate different behavior.
        self.assertFalse(is_eod_squareoff(inside))

    def test_window_false_on_non_trading_day(self):
        from datetime import datetime
        from trading_platform.agent.market_hours import IST, is_chain_snapshot_window
        saturday = datetime(2026, 8, 1, 15, 15, tzinfo=IST)  # a Saturday
        self.assertFalse(is_chain_snapshot_window(saturday))


class ChainSnapshotRoutineTests(unittest.IsolatedAsyncioTestCase):
    """The agent-loop hook must be pure best-effort: it must never raise, even
    if the collector is missing or every capture() call fails, since a fatal
    exception here would take down the whole tick loop that runs real order
    logic afterward."""

    async def test_routine_calls_capture_for_each_configured_underlying(self):
        from trading_platform.agent.trading_agent import CHAIN_SNAPSHOT_UNDERLYINGS, TradingAgent

        captured = []
        collector = SimpleNamespace(capture=lambda u, **kw: captured.append(u) or {"rows": 1})
        runtime = SimpleNamespace(options_chain_collector=collector)
        agent = TradingAgent(runtime)

        await agent._chain_snapshot_routine()

        self.assertEqual(captured, CHAIN_SNAPSHOT_UNDERLYINGS)

    async def test_routine_never_raises_when_capture_fails(self):
        from trading_platform.agent.trading_agent import TradingAgent

        def _boom(u, **kw):
            raise RuntimeError("simulated failure")
        collector = SimpleNamespace(capture=_boom)
        runtime = SimpleNamespace(options_chain_collector=collector)
        agent = TradingAgent(runtime)

        await agent._chain_snapshot_routine()  # must not raise

    async def test_routine_noop_when_collector_absent(self):
        from trading_platform.agent.trading_agent import TradingAgent

        runtime = SimpleNamespace()  # no options_chain_collector attribute
        agent = TradingAgent(runtime)

        await agent._chain_snapshot_routine()  # must not raise


if __name__ == "__main__":
    unittest.main()
