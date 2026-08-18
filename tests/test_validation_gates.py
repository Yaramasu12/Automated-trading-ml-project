"""Tests for the REDESIGN_PROMPT.md §5 validation gates — real CPCV fold
generation, Deflated Sharpe Ratio, Probability of Backtest Overfitting (CSCV),
Monte-Carlo drawdown, gate aggregation, and the strategy promotion ladder.

Several of these are explicit regression tests for bugs that made the original
(never-executed) versions of these files unusable or wrong:
  - cpcv.py fold generation raised ValueError on any real run (missing parens
    around the embargo mask's two comparisons)
  - get_combinatorial_paths() returned the 2**n power set instead of C(n, k)
  - the PBO/max-drawdown gates used a generic `value >= threshold`, which is
    INVERTED for lower-is-better metrics (PBO=0.5 vs limit 0.4 "passed")
  - deflated_sharpe_ratio() permuted the same return series to build its "null"
    distribution — shuffling doesn't change mean/std, so the deflation was a
    mathematical no-op
  - monte_carlo_dd_estimate() divided by a running peak of cumulative PnL
    starting near zero, producing drawdowns like 138.0 instead of a fraction
  - evaluate_promotion_ladder() did float("backtest") -> ValueError
"""
from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.stats import norm

from trading_platform.api.strategy_promotion_service import StrategyPromotionService
from trading_platform.data.persistence import TradingDatabase
from trading_platform.validation.cpcv import (
    CombinatorialPurgedCrossValidator,
    PathVariantResult,
    ProbabilityOfOverfittingProcessor,
    ValidationGateKeeper,
)
from trading_platform.validation.gates import (
    GateEvaluator,
    GateResult,
    deflated_sharpe_ratio,
    monte_carlo_dd_estimate,
    probability_of_backtest_overfitting,
)


class CPCVFoldGenerationTests(unittest.TestCase):
    def setUp(self):
        self.cv = CombinatorialPurgedCrossValidator(
            n_folds=6, purge_depth=5, embargo_size=5, min_train_size=50, min_test_size=20,
        )

    def test_generate_folds_runs_without_raising(self):
        """Regression: the embargo mask's missing parens made this raise
        `ValueError: truth value of an array is ambiguous` on every call."""
        folds = self.cv.generate_folds(600)
        self.assertTrue(folds, "expected at least one valid fold")

    def test_no_train_test_index_leakage(self):
        for fold in self.cv.generate_folds(600):
            overlap = set(fold.train_indices.tolist()) & set(fold.test_indices.tolist())
            self.assertEqual(overlap, set(), f"fold {fold.fold_id} leaks indices into train")

    def test_purge_and_embargo_zones_excluded_from_train(self):
        n = 600
        for fold in self.cv.generate_folds(n):
            test_start = int(fold.test_indices.min())
            test_end = int(fold.test_indices.max()) + 1
            purge_lo = max(0, test_start - self.cv.purge_depth)
            purge_hi = min(n, test_end + self.cv.purge_depth)
            in_purge = [i for i in fold.train_indices.tolist() if purge_lo <= i < purge_hi]
            self.assertEqual(in_purge, [], f"fold {fold.fold_id} kept purged indices in train")

    def test_combinatorial_paths_count_is_n_choose_k(self):
        """Regression: previously returned 2**n_folds - 1 (the power set)."""
        cv = CombinatorialPurgedCrossValidator(n_folds=10)
        paths = cv.get_combinatorial_paths(n_test_groups=2)
        self.assertEqual(len(paths), math.comb(10, 2))  # 45, not 1023
        self.assertTrue(all(len(p) == 2 for p in paths))

    def test_build_path_combines_test_windows_without_leakage(self):
        cv = CombinatorialPurgedCrossValidator(
            n_folds=6, purge_depth=5, embargo_size=5, min_train_size=10, min_test_size=10,
        )
        cv.generate_folds(600)
        fold = cv.build_path((0, 3), 600)
        overlap = set(fold.train_indices.tolist()) & set(fold.test_indices.tolist())
        self.assertEqual(overlap, set())
        # Two base folds combined -> roughly 2 * fold_size test observations.
        self.assertGreater(len(fold.test_indices), 100)


class DeflatedSharpeRatioTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(11)
        self.returns = rng.normal(0.0005, 0.01, 400)

    def test_insufficient_trials_reports_zero(self):
        """n_trials<2 cannot estimate the selection variance — must report 0.0
        (not silently 'pass'), and callers must treat 0.0 as not-passed."""
        result = deflated_sharpe_ratio(1.0, [1.0], self.returns)
        self.assertEqual(result.dsr, 0.0)
        self.assertEqual(result.n_trials, 1)

    def test_short_sample_reports_zero(self):
        result = deflated_sharpe_ratio(1.0, [1.0, 0.5], np.array([0.01] * 10))
        self.assertEqual(result.dsr, 0.0)

    def test_many_noisy_trials_deflate_a_lucky_winner(self):
        """The whole point of DSR: the best of MANY noisy trials must be
        deflated far below its naive Phi(SR) significance."""
        rng = np.random.default_rng(5)
        noisy = list(rng.normal(0.0, 1.0, 200)) + [3.0]
        deflated = deflated_sharpe_ratio(3.0, noisy, self.returns)
        naive = float(norm.cdf(3.0))
        self.assertLess(deflated.dsr, naive)
        self.assertGreater(deflated.expected_max_sharpe, 1.0, "SR_0 should be well above 0")

    def test_more_trials_deflate_harder_than_fewer(self):
        """Monotonicity: holding the winning Sharpe fixed, searching over more
        trials with the same spread must not INCREASE its DSR."""
        rng = np.random.default_rng(3)
        few = list(rng.normal(0.0, 1.0, 5)) + [2.5]
        many = list(rng.normal(0.0, 1.0, 500)) + [2.5]
        dsr_few = deflated_sharpe_ratio(2.5, few, self.returns).dsr
        dsr_many = deflated_sharpe_ratio(2.5, many, self.returns).dsr
        self.assertLessEqual(dsr_many, dsr_few)

    def test_result_is_a_probability(self):
        rng = np.random.default_rng(9)
        trials = list(rng.normal(0.1, 0.4, 30))
        result = deflated_sharpe_ratio(max(trials), trials, self.returns)
        self.assertGreaterEqual(result.dsr, 0.0)
        self.assertLessEqual(result.dsr, 1.0)


class ProbabilityOfBacktestOverfittingTests(unittest.TestCase):
    def test_pbo_near_zero_when_is_and_oos_agree(self):
        """A genuinely-best variant that wins consistently across the whole
        period is not overfit -> PBO ~ 0."""
        rng = np.random.default_rng(2)
        t_obs, n_variants = 480, 5
        matrix = np.column_stack([
            rng.normal(edge, 0.01, t_obs)
            for edge in np.linspace(0.001, 0.02, n_variants)
        ])
        result = probability_of_backtest_overfitting(matrix, n_groups=8)
        self.assertLess(result.pbo, 0.4)
        self.assertEqual(result.n_splits, math.comb(8, 4))

    def test_pbo_high_for_engineered_overfit_signature(self):
        """Classic overfit construction: each variant's 'edge' exists only in
        its own time block. Whichever looks best in-sample is mediocre
        out-of-sample -> PBO must exceed the 0.4 reject threshold."""
        rng = np.random.default_rng(7)
        t_obs, n_variants, n_groups = 800, 8, 8
        block = t_obs // n_groups
        matrix = rng.normal(0.0, 0.01, (t_obs, n_variants))
        for j in range(n_variants):
            matrix[j * block:(j + 1) * block, j] += 0.05
        result = probability_of_backtest_overfitting(matrix, n_groups=n_groups)
        self.assertGreater(result.pbo, 0.4)

    def test_single_variant_cannot_be_ranked(self):
        matrix = np.random.default_rng(1).normal(0, 0.01, (200, 1))
        result = probability_of_backtest_overfitting(matrix, n_groups=8)
        self.assertEqual(result.n_variants, 1)
        self.assertEqual(result.n_splits, 0)

    def test_path_variant_pbo_requires_two_variants(self):
        results = [
            PathVariantResult(path=(0, 1), params={}, is_metric=1.0, oos_metric=1.0,
                              oos_auc=0.6, returns=np.zeros(5)),
        ]
        out = ProbabilityOfOverfittingProcessor.calculate_from_path_variants(results)
        self.assertTrue(out.insufficient_trials)

    def test_path_variant_pbo_flags_is_winner_that_loses_oos(self):
        """Across every path the IS-best variant is the OOS-worst -> PBO = 1."""
        results = []
        for path in [(0, 1), (0, 2), (1, 2)]:
            results.append(PathVariantResult(path=path, params={"p": 1}, is_metric=9.0,
                                             oos_metric=-1.0, oos_auc=0.5, returns=np.zeros(5)))
            results.append(PathVariantResult(path=path, params={"p": 2}, is_metric=1.0,
                                             oos_metric=5.0, oos_auc=0.5, returns=np.zeros(5)))
        out = ProbabilityOfOverfittingProcessor.calculate_from_path_variants(results)
        self.assertFalse(out.insufficient_trials)
        self.assertEqual(out.pbo, 1.0)


class MonteCarloDrawdownTests(unittest.TestCase):
    def test_drawdown_is_a_fraction_of_capital(self):
        """Regression: dividing by a near-zero running peak of cumulative PnL
        produced values like 138.0 instead of a 0-1 fraction."""
        pnls = [{"pnl": p} for p in [500, -300, 700, -1200, 400, -100, 900, -250]]
        dd = monte_carlo_dd_estimate(pnls, starting_capital=1_000_000.0)
        self.assertGreaterEqual(dd, 0.0)
        self.assertLessEqual(dd, 1.0)

    def test_smaller_capital_base_yields_larger_drawdown_fraction(self):
        pnls = [{"pnl": p} for p in [-5000, 2000, -8000, 3000, -2000, 1000]]
        big = monte_carlo_dd_estimate(pnls, starting_capital=1_000_000.0)
        small = monte_carlo_dd_estimate(pnls, starting_capital=50_000.0)
        self.assertGreater(small, big)

    def test_too_few_trades_returns_zero(self):
        self.assertEqual(monte_carlo_dd_estimate([{"pnl": 1.0}], starting_capital=1000.0), 0.0)


class GateDirectionTests(unittest.TestCase):
    """Regression tests for the inverted pass/fail direction on
    lower-is-better metrics."""

    def test_pbo_above_threshold_fails(self):
        keeper = ValidationGateKeeper()
        gate = keeper.evaluate_gate("pbo", 0.5, 0.4, "PBO <= 0.4", direction="lte")
        self.assertEqual(gate.result, GateResult.FAIL)

    def test_pbo_below_threshold_passes(self):
        keeper = ValidationGateKeeper()
        gate = keeper.evaluate_gate("pbo", 0.2, 0.4, "PBO <= 0.4", direction="lte")
        self.assertEqual(gate.result, GateResult.PASS)

    def test_max_drawdown_above_limit_fails(self):
        keeper = ValidationGateKeeper()
        gate = keeper.evaluate_gate("max_drawdown", 0.20, 0.15, "DD <= 0.15", direction="lte")
        self.assertEqual(gate.result, GateResult.FAIL)

    def test_higher_is_better_metric_keeps_gte_semantics(self):
        keeper = ValidationGateKeeper()
        self.assertEqual(keeper.evaluate_gate("auc", 0.60, 0.52, "AUC").result, GateResult.PASS)
        self.assertEqual(keeper.evaluate_gate("auc", 0.40, 0.52, "AUC").result, GateResult.FAIL)


class GateEvaluatorTests(unittest.TestCase):
    def test_promotion_ladder_does_not_crash(self):
        """Regression: float(current_stage.value) on a string like 'backtest'
        raised ValueError, so the aggregate gate result could never finish."""
        evaluator = GateEvaluator()
        evaluator.evaluate_promotion_ladder(True)
        self.assertEqual(evaluator.results.promotion_ladder.result, GateResult.PASS)
        evaluator.evaluate_promotion_ladder(False)
        self.assertEqual(evaluator.results.promotion_ladder.result, GateResult.FAIL)

    def test_cost_model_uses_real_charges_not_recomputed(self):
        evaluator = GateEvaluator()
        # net 900 on gross 1000 -> ratio 0.9, above the 0.6 floor
        evaluator.evaluate_cost_model(total_pnl=900.0, total_charges=100.0, min_net_ratio=0.6)
        self.assertEqual(evaluator.results.cost_model.result, GateResult.PASS)
        self.assertAlmostEqual(evaluator.results.cost_model.metric, 0.9)

    def test_cost_model_fails_when_charges_eat_the_edge(self):
        evaluator = GateEvaluator()
        # net 100 on gross 1000 -> ratio 0.1, below the floor
        evaluator.evaluate_cost_model(total_pnl=100.0, total_charges=900.0, min_net_ratio=0.6)
        self.assertEqual(evaluator.results.cost_model.result, GateResult.FAIL)

    def test_cost_model_warns_on_non_positive_gross(self):
        evaluator = GateEvaluator()
        evaluator.evaluate_cost_model(total_pnl=-500.0, total_charges=100.0, min_net_ratio=0.6)
        self.assertEqual(evaluator.results.cost_model.result, GateResult.WARN)

    def test_paper_days_gate(self):
        evaluator = GateEvaluator()
        evaluator.evaluate_paper_days(45, 30)
        self.assertEqual(evaluator.results.paper_days.result, GateResult.PASS)
        evaluator.evaluate_paper_days(10, 30)
        self.assertEqual(evaluator.results.paper_days.result, GateResult.FAIL)

    def test_reads_thresholds_from_settings(self):
        class _Settings:
            PROMOTION_PAPER_DAYS = 60
            MIN_NET_COST_RATIO = 0.9

        evaluator = GateEvaluator(settings=_Settings())
        evaluator.evaluate_paper_days(45)  # 45 < 60 -> fails on the configured threshold
        self.assertEqual(evaluator.results.paper_days.result, GateResult.FAIL)
        self.assertEqual(evaluator.results.paper_days.threshold, 60.0)
        evaluator.evaluate_cost_model(total_pnl=800.0, total_charges=200.0)  # ratio 0.8 < 0.9
        self.assertEqual(evaluator.results.cost_model.result, GateResult.FAIL)

    def test_all_passed_ignores_unevaluated_gates(self):
        evaluator = GateEvaluator()
        evaluator.evaluate_paper_days(45, 30)
        results = evaluator.finalize("bt-1", "short_vol")
        self.assertTrue(results.all_passed)
        self.assertEqual(results.backtest_id, "bt-1")

    def test_dsr_with_thin_data_skips_rather_than_failing(self):
        """'Could not evaluate' must be distinguishable from 'scored badly' —
        otherwise a thin sample looks identical to genuine overfitting."""
        evaluator = GateEvaluator()
        evaluator.evaluate_dsr(1.0, [1.0], np.array([0.01] * 10))
        self.assertEqual(evaluator.results.dsr.result, GateResult.SKIP)

    def test_a_skipped_gate_does_not_count_as_passed(self):
        """Safety: an un-evaluated gate must BLOCK promotion, never satisfy it."""
        evaluator = GateEvaluator()
        evaluator.evaluate_dsr(1.0, [1.0], np.array([0.01] * 10))  # -> SKIP
        self.assertFalse(evaluator.results.dsr.passed)
        self.assertFalse(evaluator.finalize("bt-1", "short_vol").all_passed)

    def test_walk_forward_gate_wraps_real_result(self):
        class _WF:
            mean_test_sharpe = 0.9
            degradation_detected = False

            def to_dict(self):
                return {"mean_test_sharpe": 0.9}

        evaluator = GateEvaluator()
        evaluator.evaluate_walk_forward(_WF(), min_sharpe=0.3)
        self.assertEqual(evaluator.results.walk_forward.result, GateResult.PASS)

    def test_walk_forward_gate_fails_on_degradation(self):
        class _WF:
            mean_test_sharpe = 0.9
            degradation_detected = True

            def to_dict(self):
                return {}

        evaluator = GateEvaluator()
        evaluator.evaluate_walk_forward(_WF(), min_sharpe=0.3)
        self.assertEqual(evaluator.results.walk_forward.result, GateResult.FAIL)


class GateResultPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(Path(self._tmpdir.name) / "gates.db")

    def tearDown(self):
        try:
            self._tmpdir.cleanup()
        except (OSError, PermissionError):
            pass  # Windows keeps the SQLite handle briefly; harmless for a temp dir

    def _passing_results(self, backtest_id="bt-1", strategy_id="short_vol"):
        evaluator = GateEvaluator()
        evaluator.evaluate_paper_days(45, 30)
        evaluator.evaluate_cost_model(900.0, 100.0, 0.6)
        results = evaluator.finalize(backtest_id, strategy_id)
        evaluator.evaluate_promotion_ladder(results.all_passed)
        return results

    def test_batch_save_and_read_back(self):
        self.db.save_gate_results_batch("bt-1", "short_vol", self._passing_results())
        rows = self.db.recent_gate_results(strategy_id="short_vol")
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["gate_name"] for r in rows},
                         {"paper_days", "cost_model", "promotion_ladder"})

    def test_latest_gate_summary_reports_all_passed(self):
        self.db.save_gate_results_batch("bt-1", "short_vol", self._passing_results())
        summary = self.db.latest_gate_summary("short_vol")
        self.assertTrue(summary["all_passed"])
        self.assertEqual(summary["backtest_id"], "bt-1")

    def test_latest_gate_summary_is_none_when_never_gated(self):
        self.assertIsNone(self.db.latest_gate_summary("never_run"))

    def test_latest_gate_summary_reflects_only_the_newest_run(self):
        self.db.save_gate_results_batch("bt-old", "short_vol", self._passing_results("bt-old"))
        failing = GateEvaluator()
        failing.evaluate_paper_days(1, 30)  # fails
        self.db.save_gate_results_batch("bt-new", "short_vol", failing.finalize("bt-new", "short_vol"))
        summary = self.db.latest_gate_summary("short_vol")
        self.assertEqual(summary["backtest_id"], "bt-new")
        self.assertFalse(summary["all_passed"])

    def test_strategy_promotion_upsert_round_trip(self):
        self.db.upsert_strategy_promotion("short_vol", "paper", 2, {"note": "x"})
        row = self.db.get_strategy_promotion("short_vol")
        self.assertEqual(row["status"], "paper")
        self.db.upsert_strategy_promotion("short_vol", "live_canary", 3, {})
        self.assertEqual(self.db.get_strategy_promotion("short_vol")["status"], "live_canary")
        self.assertEqual(len(self.db.list_strategy_promotions()), 1)


class StrategyPromotionServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(Path(self._tmpdir.name) / "promo.db")
        self.svc = StrategyPromotionService(db=self.db, min_paper_days=30)

    def tearDown(self):
        try:
            self._tmpdir.cleanup()
        except (OSError, PermissionError):
            pass

    def _record_passing_gates(self, strategy_id="short_vol"):
        evaluator = GateEvaluator()
        evaluator.evaluate_paper_days(45, 30)
        results = evaluator.finalize("bt-1", strategy_id)
        evaluator.evaluate_promotion_ladder(results.all_passed)
        self.db.save_gate_results_batch("bt-1", strategy_id, results)

    def test_unknown_strategy_starts_at_research(self):
        self.assertEqual(self.svc.get_record("brand_new").status, "research")

    def test_paper_promotion_blocked_without_any_gate_run(self):
        self.svc.promote("short_vol", "shadow")
        gate = self.svc.promotion_gate(self.svc.get_record("short_vol"), "paper")
        self.assertFalse(gate["approved"])
        self.assertIn("no_backtest_gate_run_recorded", gate["blocking"])

    def test_paper_promotion_blocked_when_gates_failed(self):
        self.svc.promote("short_vol", "shadow")
        failing = GateEvaluator()
        failing.evaluate_paper_days(1, 30)  # FAIL
        self.db.save_gate_results_batch("bt-f", "short_vol", failing.finalize("bt-f", "short_vol"))
        gate = self.svc.promotion_gate(self.svc.get_record("short_vol"), "paper")
        self.assertFalse(gate["approved"])

    def test_paper_promotion_allowed_when_gates_passed(self):
        self.svc.promote("short_vol", "shadow")
        self._record_passing_gates()
        result = self.svc.promote("short_vol", "paper")
        self.assertTrue(result["promoted"])
        self.assertEqual(self.svc.get_record("short_vol").status, "paper")

    def test_live_canary_requires_paper_days(self):
        self.svc.promote("short_vol", "shadow")
        self._record_passing_gates()
        self.svc.promote("short_vol", "paper")
        blocked = self.svc.promotion_gate(self.svc.get_record("short_vol"), "live_canary")
        self.assertFalse(blocked["approved"])
        self.assertIn("paper_days", blocked["blocking"])

        self.db.upsert_strategy_promotion("short_vol", "paper", 3, {"paper_days": 45})
        allowed = self.svc.promotion_gate(self.svc.get_record("short_vol"), "live_canary")
        self.assertTrue(allowed["approved"])

    def test_live_approved_requires_manual_approval(self):
        self.db.upsert_strategy_promotion("short_vol", "live_canary", 1, {"paper_days": 45})
        self._record_passing_gates()
        without = self.svc.promotion_gate(self.svc.get_record("short_vol"), "live_approved")
        self.assertFalse(without["approved"])
        self.assertIn("manual_live_approval", without["blocking"])
        with_approval = self.svc.promotion_gate(
            self.svc.get_record("short_vol"), "live_approved", {"manual_approval": True},
        )
        self.assertTrue(with_approval["approved"])

    def test_cannot_skip_a_rung(self):
        self._record_passing_gates()
        gate = self.svc.promotion_gate(self.svc.get_record("short_vol"), "live_approved")
        self.assertFalse(gate["approved"])
        self.assertIn("single_step_forward", gate["blocking"])

    def test_rollback_is_never_gate_blocked(self):
        """De-risking must always be possible, even with failing gates."""
        self.svc.promote("short_vol", "shadow")
        result = self.svc.rollback("short_vol")
        self.assertTrue(result["rolled_back"])
        self.assertEqual(self.svc.get_record("short_vol").status, "research")

    def test_disabled_strategy_cannot_promote(self):
        self.db.upsert_strategy_promotion("short_vol", "disabled", 1, {})
        gate = self.svc.promotion_gate(self.svc.get_record("short_vol"), "shadow")
        self.assertFalse(gate["approved"])
        self.assertIn("strategy_not_disabled", gate["blocking"])


class GateWaiverTests(unittest.TestCase):
    """A waiver is a deliberate, audited override — it must work for paper,
    be impossible for live rungs, and never be silent."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(Path(self._tmpdir.name) / "w.db")
        self.svc = StrategyPromotionService(db=self.db, min_paper_days=30)

    def tearDown(self):
        try:
            self._tmpdir.cleanup()
        except (OSError, PermissionError):
            pass

    def test_waiver_unlocks_paper_without_gate_results(self):
        # The ladder is single-step, so sit on `shadow` (the rung before paper).
        self.db.upsert_strategy_promotion("sv", "shadow", 1, {})
        blocked = self.svc.promotion_gate(self.svc.get_record("sv"), "paper")
        self.assertFalse(blocked["approved"])
        self.svc.grant_gate_waiver("sv", "paper", "no index options history available")
        gate = self.svc.promotion_gate(self.svc.get_record("sv"), "paper")
        self.assertTrue(gate["approved"])

    def test_waiver_is_visible_in_the_checks_not_silent(self):
        self.svc.grant_gate_waiver("sv", "paper", "data blocker")
        gate = self.svc.promotion_gate(self.svc.get_record("sv"), "paper")
        names = {c["name"] for c in gate["checks"]}
        self.assertIn("backtest_gates_waived", names)
        waived = next(c for c in gate["checks"] if c["name"] == "backtest_gates_waived")
        self.assertEqual(waived["actual"]["reason"], "data blocker")
        self.assertFalse(waived["actual"]["gates_passed"])

    def test_waiver_cannot_be_granted_for_live_rungs(self):
        for rung in ("live_canary", "live_approved"):
            with self.assertRaises(ValueError):
                self.svc.grant_gate_waiver("sv", rung, "trust me")

    def test_paper_waiver_does_not_unlock_live_canary(self):
        self.svc.grant_gate_waiver("sv", "paper", "data blocker")
        self.db.upsert_strategy_promotion(
            "sv", "paper", 1, self.svc.get_record("sv").metadata
        )
        gate = self.svc.promotion_gate(self.svc.get_record("sv"), "live_canary")
        self.assertFalse(gate["approved"])
        names = {c["name"] for c in gate["checks"] if not c["passed"]}
        self.assertIn("backtest_gates_passed", names)

    def test_waiver_requires_a_reason(self):
        with self.assertRaises(ValueError):
            self.svc.grant_gate_waiver("sv", "paper", "   ")

    def test_revoke_restores_the_block(self):
        self.db.upsert_strategy_promotion("sv", "shadow", 1, {})
        self.svc.grant_gate_waiver("sv", "paper", "temporary")
        self.assertTrue(self.svc.promotion_gate(self.svc.get_record("sv"), "paper")["approved"])
        self.svc.revoke_gate_waiver("sv")
        self.assertFalse(self.svc.promotion_gate(self.svc.get_record("sv"), "paper")["approved"])


class ShortVolPromotionGateTests(unittest.TestCase):
    """The §5 ladder must actually gate the ONE live, enqueue-capable strategy —
    otherwise the gates are decorative for the money path that exists today."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = TradingDatabase(Path(self._tmpdir.name) / "sv.db")
        self.svc = StrategyPromotionService(db=self.db, min_paper_days=30)

        from trading_platform.strategies.short_vol_executor import ShortVolExecutor

        class _Runtime:
            pass

        rt = _Runtime()
        rt._strategy_promotion_service = self.svc
        self.executor = ShortVolExecutor.__new__(ShortVolExecutor)  # no broker/network setup
        self.executor._rt = rt

    def tearDown(self):
        try:
            self._tmpdir.cleanup()
        except (OSError, PermissionError):
            pass

    def test_blocked_at_default_research_status(self):
        reason = self.executor.promotion_block_reason()
        self.assertIsNotNone(reason)
        self.assertIn("research", reason)

    def test_allowed_once_promoted_to_paper(self):
        self.db.upsert_strategy_promotion("short_vol", "paper", 1, {})
        self.assertIsNone(self.executor.promotion_block_reason())

    def test_blocked_when_disabled(self):
        self.db.upsert_strategy_promotion("short_vol", "disabled", 1, {})
        self.assertIsNotNone(self.executor.promotion_block_reason())

    def test_escape_hatch_env_flag_bypasses(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"SHORTVOL_REQUIRE_PROMOTION": "false"}):
            self.assertIsNone(self.executor.promotion_block_reason())

    def test_fails_open_when_promotion_service_errors(self):
        """A promotion-service outage must not silently halt a running
        strategy — RiskEngine/kill-switch still gate every actual order."""
        class _Boom:
            def get_record(self, _):
                raise RuntimeError("db down")

        self.executor._rt._strategy_promotion_service = _Boom()
        self.assertIsNone(self.executor.promotion_block_reason())


class SweepGateIntegrationTests(unittest.TestCase):
    """End-to-end: a real backtest leaderboard through evaluate_sweep_gates()."""

    def test_sweep_gates_run_to_completion_on_real_backtest(self):
        from datetime import date

        from trading_platform.backtesting.engine import BacktestEngine
        from trading_platform.backtesting.evaluator import StrategyEvaluator, evaluate_sweep_gates

        evaluation = StrategyEvaluator(BacktestEngine()).evaluate(
            start=date(2026, 1, 1),
            days=60,
            underlyings=("NIFTY",),
            starting_capital=1_000_000.0,
            max_drawdown=0.10,
        )
        results = evaluate_sweep_gates(evaluation)
        summary = results.summary()
        self.assertIn("gates", summary)
        # Every evaluated gate must carry a real verdict — never crash, and
        # never silently omit itself.
        for name, gate in summary["gates"].items():
            self.assertIn(gate["result"], {"pass", "fail", "warn", "skip"}, name)
        self.assertIsNotNone(results.promotion_ladder)

    def test_sweep_gates_skip_dsr_pbo_with_a_single_variant(self):
        from datetime import date

        from trading_platform.backtesting.engine import BacktestEngine
        from trading_platform.backtesting.evaluator import StrategyEvaluator, evaluate_sweep_gates

        evaluation = StrategyEvaluator(BacktestEngine()).evaluate(
            start=date(2026, 1, 1),
            days=45,
            underlyings=("NIFTY",),
            starting_capital=1_000_000.0,
            max_drawdown=0.10,
            strategy_names=("equity_momentum",),
        )
        results = evaluate_sweep_gates(evaluation)
        self.assertEqual(results.dsr.result, GateResult.SKIP)
        self.assertEqual(results.pbo.result, GateResult.SKIP)


if __name__ == "__main__":
    unittest.main()
