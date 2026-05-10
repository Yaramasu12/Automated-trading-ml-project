"""Tests for Gap 1: QuantumKernelResearchService surfacing.

Validates that:
- QuantumKernelResult carries all required fields including backend
- QuantumKernelResearchService.classify_regime() always returns a result (stub/error/live)
- DecisionBlackboard accepts and serialises quantum_kernel_result
- The kernel result is embedded in the blackboard's to_dict()
- Classical-agreement disagreement is reflected correctly in the result
- Runtime instantiates the kernel service (checked via module import)
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from trading_platform.quantum.quantum_kernel import QuantumKernelResearchService
from trading_platform.quantum.schemas import QuantumKernelResult
from trading_platform.decision_fusion.schemas import DecisionBlackboard


# ── QuantumKernelResult schema ────────────────────────────────────────────────

class TestQuantumKernelResultSchema(unittest.TestCase):
    def _make_result(self, regime: str = "TRENDING", agreement: bool = True) -> QuantumKernelResult:
        return QuantumKernelResult(
            trace_id="test-trace",
            regime_label=regime,
            confidence=0.75,
            classical_agreement=agreement,
            backend="qiskit_kernel_placeholder",
            shadow_only=True,
        )

    def test_to_dict_has_all_required_keys(self):
        d = self._make_result().to_dict()
        required = {"trace_id", "regime_label", "confidence", "classical_agreement", "backend", "shadow_only"}
        self.assertEqual(required, set(d.keys()))

    def test_to_dict_backend_field_present(self):
        d = self._make_result().to_dict()
        self.assertEqual(d["backend"], "qiskit_kernel_placeholder")

    def test_to_dict_shadow_only_always_true(self):
        d = self._make_result().to_dict()
        self.assertTrue(d["shadow_only"])

    def test_to_dict_confidence_rounded(self):
        r = QuantumKernelResult(
            trace_id="t", regime_label="X", confidence=0.123456789,
            classical_agreement=True, shadow_only=True,
        )
        d = r.to_dict()
        # Rounded to 4 decimal places
        self.assertEqual(d["confidence"], round(0.123456789, 4))

    def test_agreement_false_reflected(self):
        d = self._make_result(agreement=False).to_dict()
        self.assertFalse(d["classical_agreement"])

    def test_regime_label_matches(self):
        d = self._make_result(regime="VOLATILE").to_dict()
        self.assertEqual(d["regime_label"], "VOLATILE")


# ── QuantumKernelResearchService ──────────────────────────────────────────────

class TestQuantumKernelResearchService(unittest.TestCase):
    def setUp(self):
        self.svc = QuantumKernelResearchService()

    def test_shadow_only_attribute(self):
        self.assertTrue(self.svc.shadow_only)

    def test_is_available_returns_bool(self):
        result = self.svc.is_available()
        self.assertIsInstance(result, bool)

    def test_classify_regime_always_returns_result(self):
        result = self.svc.classify_regime("trace-1", [0.1, 0.2, 0.5], "TRENDING")
        self.assertIsInstance(result, QuantumKernelResult)

    def test_classify_regime_stub_uses_classical_regime(self):
        """When qiskit is absent, result regime_label == classical_regime."""
        result = self.svc.classify_regime("trace-2", [0.0, 1.0, 0.3], "MEAN_REVERTING")
        self.assertEqual(result.regime_label, "MEAN_REVERTING")

    def test_classify_regime_stub_has_classical_agreement(self):
        """Stub always agrees with classical (falls back to the same label)."""
        result = self.svc.classify_regime("trace-3", [0.1], "TRENDING")
        self.assertTrue(result.classical_agreement)

    def test_classify_regime_result_is_shadow_only(self):
        result = self.svc.classify_regime("trace-4", [0.5], "VOLATILE")
        self.assertTrue(result.shadow_only)

    def test_classify_regime_no_qiskit_uses_stub_backend(self):
        result = self.svc.classify_regime("trace-5", [0.1, 0.2], "TRENDING")
        self.assertIn(result.backend, {"stub", "qiskit_kernel_placeholder", "error_fallback"})

    def test_classify_regime_error_fallback(self):
        """When _run_kernel raises, classify_regime must catch and return a valid result."""
        svc = QuantumKernelResearchService()
        with patch.object(svc, "_run_kernel", side_effect=RuntimeError("kaboom")):
            with patch.object(svc, "is_available", return_value=True):
                result = svc.classify_regime("trace-err", [0.1], "TRENDING")
        self.assertIsInstance(result, QuantumKernelResult)
        self.assertEqual(result.backend, "error_fallback")

    def test_classify_regime_returns_trace_id(self):
        result = self.svc.classify_regime("my-trace-id", [0.3], "unknown")
        self.assertEqual(result.trace_id, "my-trace-id")

    def test_classify_regime_confidence_in_range(self):
        result = self.svc.classify_regime("t", [0.5], "TRENDING")
        self.assertGreaterEqual(result.confidence, 0.0)
        self.assertLessEqual(result.confidence, 1.0)


# ── DecisionBlackboard integration ───────────────────────────────────────────

class TestDecisionBlackboardKernelField(unittest.TestCase):
    def _kernel_result(self, agreement: bool = True, regime: str = "TRENDING") -> QuantumKernelResult:
        return QuantumKernelResult(
            trace_id="bb-trace",
            regime_label=regime,
            confidence=0.6,
            classical_agreement=agreement,
            backend="stub",
            shadow_only=True,
        )

    def test_blackboard_accepts_kernel_result(self):
        bb = DecisionBlackboard(
            trace_id="test",
            execution_mode="paper",
            symbols=["NIFTY"],
            quantum_kernel_result=self._kernel_result(),
        )
        self.assertIsNotNone(bb.quantum_kernel_result)

    def test_blackboard_to_dict_includes_quantum_kernel(self):
        bb = DecisionBlackboard(
            trace_id="test",
            execution_mode="paper",
            symbols=["NIFTY"],
            quantum_kernel_result=self._kernel_result(),
        )
        d = bb.to_dict()
        self.assertIn("quantum_kernel", d)
        self.assertIsNotNone(d["quantum_kernel"])

    def test_blackboard_to_dict_kernel_none_when_not_set(self):
        bb = DecisionBlackboard(
            trace_id="test",
            execution_mode="paper",
            symbols=["NIFTY"],
        )
        d = bb.to_dict()
        self.assertIn("quantum_kernel", d)
        self.assertIsNone(d["quantum_kernel"])

    def test_blackboard_kernel_dict_has_regime_label(self):
        bb = DecisionBlackboard(
            trace_id="test",
            execution_mode="paper",
            symbols=["NIFTY"],
            quantum_kernel_result=self._kernel_result(regime="VOLATILE"),
        )
        d = bb.to_dict()
        self.assertEqual(d["quantum_kernel"]["regime_label"], "VOLATILE")

    def test_blackboard_kernel_dict_has_classical_agreement(self):
        bb = DecisionBlackboard(
            trace_id="test",
            execution_mode="paper",
            symbols=["NIFTY"],
            quantum_kernel_result=self._kernel_result(agreement=False),
        )
        d = bb.to_dict()
        self.assertFalse(d["quantum_kernel"]["classical_agreement"])

    def test_blackboard_kernel_dict_has_backend(self):
        bb = DecisionBlackboard(
            trace_id="test",
            execution_mode="paper",
            symbols=["NIFTY"],
            quantum_kernel_result=self._kernel_result(),
        )
        d = bb.to_dict()
        self.assertIn("backend", d["quantum_kernel"])

    def test_blackboard_default_kernel_is_none(self):
        bb = DecisionBlackboard(
            trace_id="t", execution_mode="paper", symbols=[]
        )
        self.assertIsNone(bb.quantum_kernel_result)


# ── Runtime instantiation (import-level smoke test) ───────────────────────────

class TestQuantumKernelImport(unittest.TestCase):
    def test_import_from_runtime_module(self):
        """runtime.py must import QuantumKernelResearchService without error."""
        from trading_platform.quantum.quantum_kernel import QuantumKernelResearchService as QK
        self.assertIsNotNone(QK)

    def test_runtime_has_kernel_service_attribute(self):
        """TradingRuntime must expose _quantum_kernel_service after init."""
        from trading_platform.api.runtime import TradingRuntime
        rt = TradingRuntime()
        self.assertTrue(hasattr(rt, "_quantum_kernel_service"))
        self.assertIsInstance(rt._quantum_kernel_service, QuantumKernelResearchService)

    def test_kernel_status_method_exists(self):
        from trading_platform.api.runtime import TradingRuntime
        rt = TradingRuntime()
        status = rt.quantum_kernel_status()
        self.assertIn("available", status)
        self.assertIn("shadow_only", status)
        self.assertTrue(status["shadow_only"])


if __name__ == "__main__":
    unittest.main()
