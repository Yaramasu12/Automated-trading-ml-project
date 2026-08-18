"""Tests for the short-vol historical backtest (REDESIGN §5 gate evidence).

These guard the properties that decide whether the backtest's output can be
trusted as promotion evidence — chiefly that it cannot silently flatter the
strategy. Several are regression tests for real modelling errors found while
building it.
"""
from __future__ import annotations

import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from trading_platform.backtesting.short_vol_backtest import (
    DailyBar,
    ShortVolBacktester,
    black_scholes,
    evaluate_short_vol_gates,
    load_daily_closes,
    run_sweep,
    smile_iv,
)
from trading_platform.domain.enums import OptionType


def _synthetic_series(n: int = 400, start: float = 20000.0, drift: float = 0.0002,
                      vol: float = 0.01, seed: int = 7) -> list[DailyBar]:
    import numpy as np
    rng = np.random.default_rng(seed)
    px, out, day = start, [], date(2020, 1, 1)
    for _ in range(n):
        px *= math.exp(drift + vol * float(rng.normal()))
        day += timedelta(days=1)
        if day.weekday() < 5:                 # weekdays only
            out.append(DailyBar(day, px))
    return out


class BlackScholesTests(unittest.TestCase):
    def test_expiry_returns_intrinsic(self):
        self.assertAlmostEqual(black_scholes(21000, 20000, 0.0, 0.15, OptionType.CE), 1000.0)
        self.assertAlmostEqual(black_scholes(19000, 20000, 0.0, 0.15, OptionType.PE), 1000.0)
        self.assertAlmostEqual(black_scholes(19000, 20000, 0.0, 0.15, OptionType.CE), 0.0)

    def test_put_call_parity(self):
        s, k, t, iv, r = 20000.0, 20100.0, 0.25, 0.14, 0.065
        call = black_scholes(s, k, t, iv, OptionType.CE, r)
        put = black_scholes(s, k, t, iv, OptionType.PE, r)
        self.assertAlmostEqual(call - put, s - k * math.exp(-r * t), places=4)

    def test_price_increases_with_vol(self):
        lo = black_scholes(20000, 20500, 0.1, 0.10, OptionType.CE)
        hi = black_scholes(20000, 20500, 0.1, 0.25, OptionType.CE)
        self.assertGreater(hi, lo)


class SmileTests(unittest.TestCase):
    def test_downside_strikes_priced_at_higher_iv(self):
        """Index put skew is the whole reason short-vol collects what it does.
        Pricing OTM puts at flat ATM vol would understate the credit paid to
        buy them back and flatter the strategy."""
        atm = 0.14
        otm_put = smile_iv(atm, 20000, 18000, OptionType.PE)   # 10% below
        self.assertGreater(otm_put, atm)

    def test_atm_is_unchanged(self):
        self.assertAlmostEqual(smile_iv(0.14, 20000, 20000, OptionType.CE), 0.14)

    def test_upside_skew_is_milder_than_downside(self):
        atm = 0.14
        down = smile_iv(atm, 20000, 18000, OptionType.PE) - atm
        up = atm - smile_iv(atm, 20000, 22000, OptionType.CE)
        self.assertGreater(down, up)

    def test_far_otm_call_iv_is_floored(self):
        """Without a floor, far-OTM calls price at ~0 vol -> ~0 credit."""
        self.assertGreaterEqual(smile_iv(0.14, 20000, 40000, OptionType.CE), 0.14 * 0.85)


class BacktestMechanicsTests(unittest.TestCase):
    def setUp(self):
        self.bars = _synthetic_series()
        # Flat 16% IV against ~16% realized -> some entries pass the VRP gate.
        self.vix = {b.day: 18.0 for b in self.bars}

    def test_runs_and_produces_an_equity_curve(self):
        res = ShortVolBacktester(underlying="NIFTY").run(self.bars, self.vix)
        self.assertEqual(len(res.equity_curve), len(self.bars))
        self.assertTrue(all(v > 0 for _, v in res.equity_curve))

    def test_position_is_marked_to_market_daily(self):
        """Regression: equity used to change ONLY on trade close, leaving ~90%
        of daily returns at exactly zero. That collapses the return series'
        std and inflates Sharpe/DSR via stale pricing."""
        res = ShortVolBacktester(underlying="NIFTY").run(self.bars, self.vix)
        if not res.trades:
            self.skipTest("no trades generated for this synthetic path")
        nonzero = [r for r in res.daily_returns if abs(r) > 1e-12]
        self.assertGreater(len(nonzero), len(res.trades),
                           "more non-zero days than trades means intra-hold marking happened")

    def test_loss_is_capped_by_the_wing(self):
        """Defined-risk: no trade may lose more than (wing - credit) per lot,
        whatever the underlying does. Uses a crash path to force stops."""
        crash = _synthetic_series(n=400, drift=-0.004, vol=0.03, seed=3)
        vix = {b.day: 30.0 for b in crash}
        res = ShortVolBacktester(underlying="NIFTY").run(crash, vix)
        for t in res.trades:
            floor = -t.max_loss_points * t.lots * 50 - t.charges - 1e-6
            self.assertGreaterEqual(t.pnl, floor,
                                    f"trade on {t.entry_day} lost more than its defined max")

    def test_charges_are_always_positive_on_a_closed_trade(self):
        res = ShortVolBacktester(underlying="NIFTY").run(self.bars, self.vix)
        for t in res.trades:
            self.assertGreater(t.charges, 0.0)

    def test_spread_is_charged_on_entry_and_exit(self):
        """A wider assumed spread must reduce net P&L — proves the cost is
        actually applied rather than silently dropped."""
        cheap = ShortVolBacktester(underlying="NIFTY", spread_points=0.5).run(self.bars, self.vix)
        dear = ShortVolBacktester(underlying="NIFTY", spread_points=5.0).run(self.bars, self.vix)
        if not cheap.trades or not dear.trades:
            self.skipTest("no trades generated for this synthetic path")
        self.assertGreater(cheap.final_equity, dear.final_equity)

    def test_no_entry_when_vix_missing(self):
        res = ShortVolBacktester(underlying="NIFTY").run(self.bars, {})
        self.assertEqual(res.trades, [])

    def test_only_one_position_open_at_a_time(self):
        res = ShortVolBacktester(underlying="NIFTY").run(self.bars, self.vix)
        for a, b in zip(res.trades, res.trades[1:]):
            self.assertIsNotNone(a.exit_day)
            self.assertLessEqual(a.exit_day, b.entry_day)

    def test_entries_only_on_the_configured_weekday(self):
        res = ShortVolBacktester(underlying="NIFTY", entry_weekday=0).run(self.bars, self.vix)
        for t in res.trades:
            self.assertEqual(t.entry_day.weekday(), 0)


class NoFabricatedEdgeTests(unittest.TestCase):
    def test_zero_vrp_regime_produces_no_trades(self):
        """If implied == realized there is no premium to harvest, so the VRP
        gate must refuse every entry. A backtest that still trades here would
        be manufacturing edge.

        `decide()` compares implied against the TRAILING 20-day realized vol,
        so "zero VRP" has to be constructed per-day against that same trailing
        window — pinning VIX to a whole-series average instead leaves VRP
        positive on every day whose trailing vol happens to sit below it.
        """
        bars = _synthetic_series(n=300, vol=0.01, seed=11)
        strategy = ShortVolBacktester(underlying="NIFTY").strategy
        closes = [b.close for b in bars]
        vix = {
            b.day: strategy.realized_vol(closes[: i + 1])
            for i, b in enumerate(bars)
        }
        res = ShortVolBacktester(underlying="NIFTY").run(bars, vix)
        self.assertEqual(res.trades, [], "traded despite zero volatility risk premium")


class LoaderTests(unittest.TestCase):
    def test_skips_malformed_rows(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.csv"
            p.write_text(
                "timestamp,open,high,low,close,volume\n"
                "2026-01-01T00:00:00+05:30,1,1,1,100.0,0\n"
                "not-a-date,1,1,1,101.0,0\n"
                "2026-01-03T00:00:00+05:30,1,1,1,notanumber,0\n"
                "2026-01-04T00:00:00+05:30,1,1,1,-5,0\n"
                "2026-01-05T00:00:00+05:30,1,1,1,102.0,0\n",
                encoding="utf-8",
            )
            bars = load_daily_closes(p)
        self.assertEqual([b.close for b in bars], [100.0, 102.0])


class StructureVariantTests(unittest.TestCase):
    """REDESIGN §4.2 structures. These assert the variants are REAL (different
    legs, different risk), not a parameter relabel — a backtest that silently
    ran condors for every label would look like structure diversification
    while providing none."""

    def setUp(self):
        self.bars = _synthetic_series(n=500, seed=31)
        self.vix = {b.day: 22.0 for b in self.bars}

    def test_condor_has_four_legs_spreads_have_two(self):
        from trading_platform.strategies.short_vol import ShortVolStrategy
        st = ShortVolStrategy(min_vrp=0.5)
        closes = [b.close for b in self.bars]
        # Capital large enough that fractional-Kelly sizes >=1 lot, otherwise
        # decide() declines and returns no legs at all (nothing to assert on).
        common = dict(spot=closes[-1], vix=30.0, closes=closes,
                      capital=50_000_000.0, lot_size=50, strike_step=50,
                      wing_width=300, hold_days=5)
        for structure, n_legs in (("condor", 4), ("put_spread", 2), ("call_spread", 2)):
            d = st.decide(structure=structure, **common)
            self.assertTrue(d.enter, f"{structure} declined: {d.reason}")
            self.assertEqual(len(d.legs), n_legs, structure)

    def test_put_spread_uses_only_puts(self):
        from trading_platform.domain.enums import OptionType
        from trading_platform.strategies.short_vol import ShortVolStrategy
        closes = [b.close for b in self.bars]
        d = ShortVolStrategy(min_vrp=0.5).decide(
            spot=closes[-1], vix=30.0, closes=closes, capital=50_000_000.0,
            lot_size=50, strike_step=50, wing_width=300, hold_days=5,
            structure="put_spread")
        self.assertTrue(d.enter, d.reason)
        self.assertTrue(all(leg.option_type == OptionType.PE for leg in d.legs))

    def test_structure_is_recorded_on_every_trade(self):
        for st in ("condor", "put_spread", "call_spread"):
            res = ShortVolBacktester(underlying="NIFTY", structure=st).run(self.bars, self.vix)
            for t in res.trades:
                self.assertEqual(t.structure, st)

    def test_structures_produce_different_results(self):
        """If two structures give identical equity, the structure argument is
        not actually reaching decide()."""
        a = ShortVolBacktester(underlying="NIFTY", structure="condor").run(self.bars, self.vix)
        b = ShortVolBacktester(underlying="NIFTY", structure="put_spread").run(self.bars, self.vix)
        if not a.trades or not b.trades:
            self.skipTest("no trades on this synthetic path")
        self.assertNotAlmostEqual(a.final_equity, b.final_equity, places=2)

    def test_every_structure_stays_defined_risk(self):
        """No naked legs, ever — each structure must cap its loss at the wing.
        This mirrors the live RiskEngine's naked-option ban."""
        crash = _synthetic_series(n=400, drift=-0.004, vol=0.03, seed=5)
        vix = {b.day: 32.0 for b in crash}
        for st in ("condor", "put_spread", "call_spread"):
            res = ShortVolBacktester(underlying="NIFTY", structure=st).run(crash, vix)
            for t in res.trades:
                floor = -t.max_loss_points * t.lots * 50 - t.charges - 1e-6
                self.assertGreaterEqual(t.pnl, floor, f"{st} exceeded its defined max loss")


class SweepGateTests(unittest.TestCase):
    def test_sweep_gates_run_and_report_every_slot(self):
        bars = _synthetic_series(n=500, seed=21)
        vix = {b.day: 20.0 for b in bars}
        grid = [{"sd": sd, "min_vrp": 1.0, "kelly_fraction": 0.3} for sd in (1.0, 1.25, 1.5)]
        sweep = run_sweep(bars, vix, underlying="NIFTY", grid=grid)
        res = evaluate_short_vol_gates(sweep, strategy_id="short_vol")
        self.assertEqual(res.strategy_id, "short_vol")
        # Monte-Carlo and cost model always evaluate; DSR/PBO need >=2 variants.
        self.assertIsNotNone(res.monte_carlo)
        self.assertIsNotNone(res.cost_model)

    def test_empty_sweep_does_not_crash(self):
        res = evaluate_short_vol_gates([], strategy_id="short_vol")
        self.assertEqual(res.strategy_id, "short_vol")


if __name__ == "__main__":
    unittest.main()
