"""Tests for trading_platform/research/strategy_validator.py — the bridge
that lets the DSR/PBO/Monte-Carlo gates already trusted for short_vol (and
that correctly rejected futures_trend) run against the other dormant
strategies in strategies/factory.py before any of them reaches live capital.
"""
from __future__ import annotations

import csv
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from trading_platform.backtesting.short_vol_backtest import DailyBar
from trading_platform.domain.enums import Side
from trading_platform.domain.models import Signal
from trading_platform.research.strategy_validator import (
    load_market_bars_csv,
    make_exposure_fn,
    to_daily_bars,
    validate_strategy,
)


def _write_csv(path: Path, rows: list[tuple[str, float, float, float, float, int]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for ts, o, h, l, c, v in rows:
            writer.writerow([ts, o, h, l, c, v])


class LoadMarketBarsCsvTests(unittest.TestCase):
    def test_parses_full_ohlcv_and_sorts_by_date(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "TEST.csv"
            _write_csv(path, [
                ("2026-01-02T00:00:00+05:30", 101, 103, 100, 102, 1000),
                ("2026-01-01T00:00:00+05:30", 100, 102, 99, 101, 900),
            ])
            bars = load_market_bars_csv(path, "TEST")

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].timestamp.date(), date(2026, 1, 1))
        self.assertEqual(bars[1].timestamp.date(), date(2026, 1, 2))
        self.assertEqual(bars[0].high, 102)
        self.assertEqual(bars[0].volume, 900)
        self.assertEqual(bars[0].symbol, "TEST")

    def test_skips_rows_with_non_positive_or_missing_close(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "TEST.csv"
            _write_csv(path, [
                ("2026-01-01T00:00:00+05:30", 100, 102, 99, 101, 900),
                ("2026-01-02T00:00:00+05:30", 100, 102, 99, 0, 900),
                ("not-a-date", 100, 102, 99, 101, 900),
            ])
            bars = load_market_bars_csv(path, "TEST")

        self.assertEqual(len(bars), 1)


class ToDailyBarsTests(unittest.TestCase):
    def test_index_alignment_with_market_bars(self):
        from trading_platform.domain.models import MarketBar

        market_bars = [
            MarketBar(timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), symbol="X",
                      open=1, high=1, low=1, close=1, volume=1),
            MarketBar(timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc), symbol="X",
                      open=2, high=2, low=2, close=2, volume=1),
        ]
        daily = to_daily_bars(market_bars)
        self.assertEqual(len(daily), len(market_bars))
        for mb, db in zip(market_bars, daily):
            self.assertEqual(mb.timestamp.date(), db.day)
            self.assertEqual(mb.close, db.close)


def _trending_up_bars(n: int, symbol: str = "TEST", start_price: float = 100.0) -> list:
    """Bars with steady net-up momentum but realistic noise, volume surge on
    the last several days -- built to reliably trip EquityMomentumStrategy's
    BUY condition (momentum_5/momentum_20 aligned positive, RSI < 72). A
    PERFECTLY monotonic series has zero down-days, which drives Wilder's RSI
    to exactly 100 (avg_loss==0) -- above the strategy's own 72 overbought
    filter and therefore never entering. Every 4th day dips slightly so RSI
    lands in a realistic mid-range instead of pegged at the ceiling."""
    from trading_platform.domain.models import MarketBar

    bars = []
    price = start_price
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        # +1.2%, +1.2%, -1.2% repeating -> net upward drift with a real RSI
        # (verified empirically: mom5=0.012, mom20=0.073, RSI=64 -- clears
        # momentum thresholds while staying under the 72 overbought filter).
        price *= 0.988 if i % 3 == 2 else 1.012
        vol = 2_000_000 if i >= n - 5 else 1_000_000  # volume surge near the end
        bars.append(MarketBar(
            timestamp=base + timedelta(days=i), symbol=symbol,
            open=price * 0.999, high=price * 1.002, low=price * 0.997,
            close=price, volume=vol,
        ))
    return bars


def _crash_after(bars: list, crash_pct: float) -> list:
    """Append one more bar that gaps down by crash_pct from the last close —
    used to force a stop-loss exit deterministically."""
    from trading_platform.domain.models import MarketBar

    last = bars[-1]
    crashed_close = last.close * (1 - crash_pct)
    return bars + [MarketBar(
        timestamp=last.timestamp + timedelta(days=1), symbol=last.symbol,
        open=crashed_close, high=crashed_close * 1.001, low=crashed_close * 0.99,
        close=crashed_close, volume=last.volume,
    )]


class MakeExposureFnTests(unittest.TestCase):
    def test_enters_long_on_real_buy_signal_and_exits_on_stop_loss(self):
        market_bars = _trending_up_bars(30)
        market_bars = _crash_after(market_bars, crash_pct=0.05)  # > 1.5% stop
        daily_bars = [DailyBar(day=b.timestamp.date(), close=b.close) for b in market_bars]

        exposure_fn = make_exposure_fn("equity_momentum", "TEST", market_bars)
        exposures = exposure_fn(daily_bars, {"stop_target_scale": 1.0})

        self.assertEqual(len(exposures), len(daily_bars))
        # Must actually take a position at some point (real signal, not silently flat).
        self.assertTrue(any(e != 0.0 for e in exposures))
        # The crash bar (last one) must have flattened the position -- stop-loss fired.
        self.assertEqual(exposures[-1], 0.0)

    def test_flat_when_strategy_never_signals(self):
        # A perfectly flat, zero-volume series should never trip momentum's
        # entry conditions (momentum_5/20 both ~0) -- exposure must stay 0.
        from trading_platform.domain.models import MarketBar

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        market_bars = [
            MarketBar(timestamp=base + timedelta(days=i), symbol="TEST",
                      open=100, high=100, low=100, close=100, volume=1000)
            for i in range(30)
        ]
        daily_bars = [DailyBar(day=b.timestamp.date(), close=b.close) for b in market_bars]

        exposure_fn = make_exposure_fn("equity_momentum", "TEST", market_bars)
        exposures = exposure_fn(daily_bars, {})

        self.assertTrue(all(e == 0.0 for e in exposures))

    def test_stop_target_scale_widens_or_tightens_the_exit(self):
        # equity_momentum's default stop is 1.5%; scale=0.5 -> 0.75% stop
        # (a 2% dip must force an exit), scale=3.0 -> 4.5% stop (a 2% dip
        # must NOT). This is the concrete behavior the knob exists to change.
        market_bars = _trending_up_bars(30)
        market_bars = _crash_after(market_bars, crash_pct=0.02)
        daily_bars = [DailyBar(day=b.timestamp.date(), close=b.close) for b in market_bars]

        exposure_fn = make_exposure_fn("equity_momentum", "TEST", market_bars)
        tight = exposure_fn(daily_bars, {"stop_target_scale": 0.5})
        wide = exposure_fn(daily_bars, {"stop_target_scale": 3.0})

        # Both runs must actually have entered a position at some point
        # before the crash bar, or this comparison would be vacuous.
        self.assertTrue(any(e != 0.0 for e in tight[:-1]))
        self.assertTrue(any(e != 0.0 for e in wide[:-1]))
        self.assertEqual(tight[-1], 0.0, "tight stop should have exited on the 2% crash")
        self.assertNotEqual(wide[-1], 0.0, "wide stop should have stayed in on the 2% crash")

    def test_exception_in_generate_signal_is_treated_as_no_signal(self):
        market_bars = _trending_up_bars(30)
        daily_bars = [DailyBar(day=b.timestamp.date(), close=b.close) for b in market_bars]
        exposure_fn = make_exposure_fn("equity_momentum", "TEST", market_bars)

        with mock.patch(
            "trading_platform.strategies.equity.EquityMomentumStrategy.generate_signal",
            side_effect=RuntimeError("boom"),
        ):
            exposures = exposure_fn(daily_bars, {})

        self.assertTrue(all(e == 0.0 for e in exposures))


class ValidateStrategyEndToEndTests(unittest.TestCase):
    """Integration-style: runs the real pipeline against real cached
    historical data. Does not assert PASS/FAIL -- whether a given strategy
    clears the gates is a genuine research finding, not a fixed expectation
    -- only that the full path runs, returns a well-formed record, and the
    reported numbers are internally consistent."""

    def test_mean_reversion_on_real_reliance_history_runs_end_to_end(self):
        csv_path = Path("data/historical/RELIANCE__ONE_DAY_deep.csv")
        if not csv_path.exists():
            self.skipTest("real historical CSV not present in this checkout")

        record = validate_strategy("mean_reversion", "RELIANCE", csv_path)

        self.assertEqual(record.strategy_name, "mean_reversion")
        self.assertEqual(record.symbol, "RELIANCE")
        self.assertIsInstance(record.passed, bool)
        self.assertIsInstance(record.cagr, float)
        self.assertGreaterEqual(record.max_drawdown, 0.0)
        self.assertLessEqual(record.max_drawdown, 1.0)


if __name__ == "__main__":
    unittest.main()
