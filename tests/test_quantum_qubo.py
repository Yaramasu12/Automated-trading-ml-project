"""Tests for Phase 4: QUBO builder and classical optimizer."""
import unittest

from trading_platform.quantum.qubo import build_qubo, check_constraints, evaluate_solution
from trading_platform.quantum.schemas import PortfolioOptimizationRequest, QuantumCandidate
from trading_platform.quantum.backends.classical import ClassicalOptimizerBackend


def _make_req(n: int = 5) -> PortfolioOptimizationRequest:
    candidates = [
        QuantumCandidate(
            symbol=f"SYM{i}",
            side="BUY",
            expected_edge=0.01 * (i + 1),
            risk_estimate=0.3,
            liquidity_score=1.0,
        )
        for i in range(n)
    ]
    return PortfolioOptimizationRequest(
        trace_id="test-qubo",
        candidates=candidates,
        cardinality_limit=2,
    )


class TestQuboBuilder(unittest.TestCase):
    def test_builds_correct_size(self):
        req = _make_req(4)
        qubo = build_qubo(req)
        self.assertEqual(qubo.n, 4)
        self.assertEqual(len(qubo.Q), 4)
        self.assertEqual(len(qubo.Q[0]), 4)

    def test_symmetric_matrix(self):
        req = _make_req(4)
        qubo = build_qubo(req)
        for i in range(4):
            for j in range(4):
                self.assertAlmostEqual(qubo.Q[i][j], qubo.Q[j][i])

    def test_empty_candidates(self):
        req = PortfolioOptimizationRequest(trace_id="t", candidates=[])
        qubo = build_qubo(req)
        self.assertEqual(qubo.n, 0)

    def test_constraint_descriptions_present(self):
        req = _make_req(3)
        qubo = build_qubo(req)
        self.assertGreater(len(qubo.constraint_descriptions), 0)


class TestConstraintCheck(unittest.TestCase):
    def test_valid_solution(self):
        req = _make_req(5)
        sol = [1, 1, 0, 0, 0]  # 2 selected, within cardinality limit
        ok, violations = check_constraints(sol, req)
        self.assertTrue(ok)
        self.assertEqual(violations, [])

    def test_cardinality_violation(self):
        req = _make_req(5)
        sol = [1, 1, 1, 1, 1]  # 5 selected, limit is 2
        ok, violations = check_constraints(sol, req)
        self.assertFalse(ok)
        self.assertGreater(len(violations), 0)

    def test_liquidity_violation(self):
        req = PortfolioOptimizationRequest(
            trace_id="t",
            candidates=[
                QuantumCandidate("A", "BUY", 0.01, 0.3, liquidity_score=0.1),
            ],
            cardinality_limit=2,
            min_liquidity_score=0.5,
        )
        sol = [1]
        ok, violations = check_constraints(sol, req)
        self.assertFalse(ok)


class TestClassicalOptimizer(unittest.TestCase):
    def test_returns_solution(self):
        req = _make_req(5)
        backend = ClassicalOptimizerBackend()
        sol, obj = backend.optimize(req)
        self.assertEqual(len(sol), 5)
        self.assertIn(0, sol + [0])
        self.assertIn(1, sol + [0])

    def test_respects_cardinality(self):
        req = _make_req(6)
        backend = ClassicalOptimizerBackend()
        sol, _ = backend.optimize(req)
        selected = sum(sol)
        self.assertLessEqual(selected, req.cardinality_limit)

    def test_no_duplicate_symbols(self):
        req = _make_req(4)
        backend = ClassicalOptimizerBackend()
        sol, _ = backend.optimize(req)
        selected_syms = [req.candidates[i].symbol for i in range(len(sol)) if sol[i] == 1]
        self.assertEqual(len(selected_syms), len(set(selected_syms)))

    def test_empty_candidates(self):
        req = PortfolioOptimizationRequest(trace_id="t", candidates=[])
        backend = ClassicalOptimizerBackend()
        sol, obj = backend.optimize(req)
        self.assertEqual(sol, [])

    def test_always_available(self):
        backend = ClassicalOptimizerBackend()
        status = backend.is_available()
        self.assertTrue(status.available)


if __name__ == "__main__":
    unittest.main()
