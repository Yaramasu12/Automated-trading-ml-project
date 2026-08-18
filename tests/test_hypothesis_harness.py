"""Tests for the hypothesis harness and the TCA fixes.

The harness's whole value is that it can be trusted to reject bad ideas
cheaply. So the tests that matter most are the ones proving it CANNOT be
fooled — no lookahead, costs actually charged, and a known-good synthetic
edge detected while pure noise is not.
"""
from __future__ import annotations

import math
import unittest
from datetime import date, datetime, timedelta, timezone

from trading_platform.backtesting.short_vol_backtest import DailyBar
from trading_platform.execution.tca import FillRecord, TransactionCostAnalyzer
from trading_platform.research.hypothesis_harness import (
    HypothesisHarness,
    HypothesisSpec,
    buy_and_hold_cagr,
)


def _bars(closes: list[float], start: date = date(2020, 1, 1)) -> list[DailyBar]:
    out, day = [], start
    for c in closes:
        out.append(DailyBar(day, c))
        day += timedelta(days=1)
    return out


def _random_walk(n: int = 600, seed: int = 5, vol: float = 0.01,
                 drift: float = 0.0) -> list[DailyBar]:
    import numpy as np
    rng = np.random.default_rng(seed)
    px, closes = 20000.0, []
    for _ in range(n):
        px *= math.exp(drift + vol * float(rng.normal()))
        closes.append(px)
    return _bars(closes)


class NoLookaheadTests(unittest.TestCase):
    """The single most important property. A harness with lookahead makes
    every hypothesis look brilliant and is worse than having none."""

    def test_exposure_earns_the_NEXT_bar_return_not_todays(self):
        # Price jumps once, on the final bar. A hypothesis that "knows" the
        # jump only at the close of the jump bar must earn NOTHING from it.
        bars = _bars([100.0, 100.0, 100.0, 200.0])

        def only_on_jump_day(bars, _params):
            return [0.0, 0.0, 0.0, 1.0]      # long only on the bar that jumped

        # cost_per_turn=0 so this isolates the lookahead property; with costs
        # on, equity dips slightly purely from the turn, which would muddy the
        # assertion without saying anything about lookahead.
        v = HypothesisHarness(starting_capital=1000.0).evaluate(
            HypothesisSpec("late", only_on_jump_day, cost_per_turn=0.0), bars)
        self.assertAlmostEqual(v.runs[0].final_equity, 1000.0, places=6)

    def test_exposure_set_before_the_move_does_earn_it(self):
        bars = _bars([100.0, 100.0, 100.0, 200.0])

        def before_jump(bars, _params):
            return [0.0, 0.0, 1.0, 1.0]      # long from the bar BEFORE the jump

        v = HypothesisHarness(starting_capital=1000.0).evaluate(
            HypothesisSpec("early", before_jump, cost_per_turn=0.0), bars)
        self.assertGreater(v.runs[0].final_equity, 1900.0)


class CostTests(unittest.TestCase):
    def test_costs_are_charged_on_every_turn(self):
        bars = _random_walk(200)

        def flip(bars, _p):
            return [1.0 if i % 2 == 0 else 0.0 for i in range(len(bars))]

        v = HypothesisHarness().evaluate(
            HypothesisSpec("flip", flip, cost_per_turn=0.001), bars)
        self.assertGreater(v.runs[0].total_costs, 0.0)
        self.assertGreater(v.runs[0].n_turns, 50)

    def test_higher_cost_reduces_equity(self):
        bars = _random_walk(200, drift=0.001)

        def flip(bars, _p):
            return [1.0 if i % 3 else 0.0 for i in range(len(bars))]

        cheap = HypothesisHarness().evaluate(
            HypothesisSpec("c", flip, cost_per_turn=0.0), bars).runs[0].final_equity
        dear = HypothesisHarness().evaluate(
            HypothesisSpec("d", flip, cost_per_turn=0.01), bars).runs[0].final_equity
        self.assertGreater(cheap, dear)


class DetectionTests(unittest.TestCase):
    def test_detects_a_planted_edge(self):
        """Sanity: a hypothesis with genuine foresight must be profitable.
        If this fails the simulator is broken, not the market."""
        bars = _random_walk(600, seed=3)

        def oracle(bars, _p):
            # Long when the NEXT bar rises — deliberate lookahead, used ONLY
            # to prove the machinery can detect a real edge when one exists.
            out = [0.0] * len(bars)
            for i in range(len(bars) - 1):
                out[i] = 1.0 if bars[i + 1].close > bars[i].close else 0.0
            return out

        v = HypothesisHarness().evaluate(
            HypothesisSpec("oracle", oracle, cost_per_turn=0.0), bars)
        self.assertGreater(v.best_sharpe, 3.0, "planted edge should be obvious")

    def test_pure_noise_does_not_pass(self):
        """A coin-flip strategy must not clear the gates."""
        import numpy as np
        rng = np.random.default_rng(17)
        bars = _random_walk(600, seed=8)

        def coinflip(bars, p):
            r = np.random.default_rng(int(p["seed"]))
            return [float(r.integers(0, 2)) for _ in bars]

        v = HypothesisHarness().evaluate(
            HypothesisSpec("noise", coinflip, [{"seed": s} for s in range(4)]), bars)
        self.assertFalse(v.passed, "random noise cleared the gates")


class HarnessContractTests(unittest.TestCase):
    def test_wrong_length_exposure_is_rejected_loudly(self):
        bars = _random_walk(50)
        with self.assertRaises(ValueError):
            HypothesisHarness().evaluate(
                HypothesisSpec("short", lambda b, p: [0.0] * (len(b) - 1)), bars)

    def test_benchmark_comparison_is_reported(self):
        bars = _random_walk(300, drift=0.001)
        bh = buy_and_hold_cagr(bars)
        v = HypothesisHarness().evaluate(
            HypothesisSpec("flat", lambda b, p: [0.0] * len(b)), bars,
            benchmark_cagr=bh)
        self.assertIsNotNone(v.beats_benchmark())
        self.assertFalse(v.beats_benchmark(), "a flat book cannot beat a rising benchmark")

    def test_buy_and_hold_cagr_matches_manual(self):
        bars = _bars([100.0, 110.0])
        yrs = (bars[-1].day - bars[0].day).days / 365.25
        self.assertAlmostEqual(buy_and_hold_cagr(bars), (110 / 100) ** (1 / yrs) - 1, places=6)


class TCACorrectnessTests(unittest.TestCase):
    """Regression tests for two real bugs: the rupee cost was derived from an
    incoherent sum of differently-signed components, and the quality bands
    topped out so a catastrophic fill still rated GOOD."""

    def setUp(self):
        self.tca = TransactionCostAnalyzer()

    def _fill(self, side: str, fill_price: float, arrival: float, qty: int = 50) -> FillRecord:
        return FillRecord(
            correlation_id="t", symbol="X", exchange="NFO", side=side, quantity=qty,
            fill_price=fill_price, fill_time=datetime.now(timezone.utc),
            arrival_price=arrival, benchmark_price=arrival, order_type="MARKET",
            urgency="NORMAL", strategy="s", time_to_fill_ms=200.0)

    def test_rupee_cost_equals_shortfall_times_quantity(self):
        """Regression: a BUY of 50 filled 5 points above a 100 arrival is a
        Rs250 cost. This used to report Rs13."""
        r = self.tca.analyze_fill(self._fill("BUY", 105.0, 100.0))
        self.assertAlmostEqual(r.total_cost_inr, 250.0, places=2)

    def test_sell_below_arrival_is_also_a_cost(self):
        r = self.tca.analyze_fill(self._fill("SELL", 98.0, 100.0))
        self.assertAlmostEqual(r.implementation_shortfall_bps, 200.0, places=3)
        self.assertAlmostEqual(r.total_cost_inr, 100.0, places=2)

    def test_perfect_fill_costs_nothing(self):
        r = self.tca.analyze_fill(self._fill("BUY", 100.0, 100.0))
        self.assertAlmostEqual(r.implementation_shortfall_bps, 0.0, places=6)
        self.assertAlmostEqual(r.total_cost_inr, 0.0, places=6)

    def test_catastrophic_fill_is_not_rated_good(self):
        """Regression: 500bps used to rate GOOD, same as 21bps."""
        r = self.tca.analyze_fill(self._fill("BUY", 105.0, 100.0))
        self.assertEqual(r.quality_rating, "POOR")

    def test_small_slip_still_rates_well(self):
        r = self.tca.analyze_fill(self._fill("BUY", 100.1, 100.0))
        self.assertIn(r.quality_rating, {"EXCELLENT", "GOOD"})

    def test_quality_is_monotonic_in_cost(self):
        rank = {"EXCELLENT": 3, "GOOD": 2, "FAIR": 1, "POOR": 0}
        prev = 4
        for slip in (0.0, 0.1, 0.2, 0.5, 2.0, 10.0):
            r = self.tca.analyze_fill(self._fill("BUY", 100.0 + slip, 100.0))
            cur = rank[r.quality_rating]
            self.assertLessEqual(cur, prev, f"quality improved as cost rose at slip={slip}")
            prev = cur


if __name__ == "__main__":
    unittest.main()


class LivenessGuardTests(unittest.TestCase):
    """A do-nothing hypothesis must never be reported as a PASS.

    Regression for a real hole found 2026-08-09: an LLM-proposed hypothesis
    "passed" holdout with CAGR 0.00% / Sharpe 0.00 because it held no position
    for the entire period. No trades => no drawdown, no losses, nothing to
    overfit => every gate trivially satisfied.
    """

    def test_always_flat_hypothesis_does_not_pass(self):
        bars = _random_walk(600, seed=11, drift=0.0006)
        spec = HypothesisSpec(
            name="always_flat",
            exposure_fn=lambda b, p: [0.0] * len(b),
            param_grid=[{"a": 1}, {"a": 2}],
        )
        v = HypothesisHarness().evaluate(spec, bars)
        self.assertFalse(v.passed, "a strategy that never trades must not pass")

    def test_hypothesis_that_always_errors_does_not_pass(self):
        """Code that raises on every param combination is flat by default —
        it must be rejected, not silently treated as a clean run."""
        def boom(bars, params):
            raise ValueError("bad hypothesis")

        spec = HypothesisSpec(name="always_errors", exposure_fn=boom,
                              param_grid=[{"a": 1}, {"a": 2}])
        v = HypothesisHarness().evaluate(spec, _random_walk(600, seed=12))
        self.assertFalse(v.passed)

    def test_a_genuinely_trading_hypothesis_can_still_pass(self):
        """Guard against over-correcting: liveness must not block real results."""
        bars = _random_walk(800, seed=13, vol=0.006, drift=0.0010)
        spec = HypothesisSpec(
            name="always_long",
            exposure_fn=lambda b, p: [1.0] * len(b),
            param_grid=[{"a": 1}, {"a": 2}],
        )
        v = HypothesisHarness().evaluate(spec, bars)
        self.assertGreater(len(v.__dict__), 0)
        self.assertNotEqual(v.best_cagr, 0.0, "a fully-invested book must move")
