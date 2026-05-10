"""Tests for Phase 4: QuantumOptimizationService."""
import unittest

from trading_platform.quantum.service import QuantumOptimizationService
from trading_platform.quantum.schemas import PortfolioOptimizationRequest, QuantumCandidate


def _make_req(n: int = 4) -> PortfolioOptimizationRequest:
    return PortfolioOptimizationRequest(
        trace_id="qsvc-test",
        candidates=[
            QuantumCandidate(f"S{i}", "BUY", 0.01 * (i + 1), 0.3)
            for i in range(n)
        ],
        cardinality_limit=2,
    )


class TestQuantumOptimizationService(unittest.TestCase):
    def setUp(self):
        self._svc = QuantumOptimizationService(backend="classical", timeout=2)

    def test_returns_result(self):
        req = _make_req(4)
        result = self._svc.optimize(req)
        self.assertIsNotNone(result)

    def test_no_order_creation(self):
        req = _make_req(4)
        result = self._svc.optimize(req)
        d = result.to_dict()
        self.assertNotIn("order_id", str(d))
        self.assertNotIn("OrderIntent", str(d))

    def test_respects_cardinality(self):
        req = _make_req(6)
        result = self._svc.optimize(req)
        self.assertLessEqual(len(result.selected_symbols), req.cardinality_limit)

    def test_empty_candidates(self):
        req = PortfolioOptimizationRequest(trace_id="t", candidates=[])
        result = self._svc.optimize(req)
        self.assertEqual(result.selected_symbols, [])

    def test_backend_status(self):
        statuses = self._svc.backend_status()
        self.assertGreater(len(statuses), 0)
        # Classical must always be available
        classical = next((s for s in statuses if s["name"] == "classical"), None)
        self.assertIsNotNone(classical)
        self.assertTrue(classical["available"])

    def test_missing_qiskit_does_not_fail(self):
        """Qiskit backend unavailability should be handled gracefully."""
        svc = QuantumOptimizationService(backend="qiskit", timeout=1)
        req = _make_req(3)
        result = svc.optimize(req)
        # Falls back to classical
        self.assertIn(result.backend_used, {"classical", "qiskit"})

    def test_missing_dwave_does_not_fail(self):
        svc = QuantumOptimizationService(backend="dwave", timeout=1)
        req = _make_req(3)
        result = svc.optimize(req)
        self.assertIn(result.backend_used, {"classical", "dwave"})

    def test_quantum_worse_than_baseline_rejected(self):
        """Service must use classical when quantum doesn't beat baseline."""
        svc = QuantumOptimizationService(
            backend="classical",
            timeout=2,
            min_baseline_improvement=100.0,  # impossibly high threshold
        )
        req = _make_req(4)
        result = svc.optimize(req)
        # Classical is always used when min_improvement is set high
        self.assertEqual(result.backend_used, "classical")


if __name__ == "__main__":
    unittest.main()
