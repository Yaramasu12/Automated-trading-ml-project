from __future__ import annotations

"""Quantum kernel research service — shadow-only regime classifier.

No live influence until walk-forward improvement is proven.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_platform.quantum.schemas import QuantumKernelResult

logger = logging.getLogger(__name__)


class QuantumKernelResearchService:
    """Shadow-only quantum kernel classifier for regime detection.

    Compares quantum-kernel prediction against classical regime classifier.
    Results are written to trace but do NOT influence live decisions.
    """

    shadow_only = True

    def is_available(self) -> bool:
        try:
            import qiskit_machine_learning  # noqa: F401
            import qiskit  # noqa: F401
            return True
        except ImportError:
            return False

    def classify_regime(
        self,
        trace_id: str,
        features: list[float],
        classical_regime: str = "unknown",
    ) -> QuantumKernelResult:
        from trading_platform.quantum.schemas import QuantumKernelResult
        if not self.is_available():
            return QuantumKernelResult(
                trace_id=trace_id,
                regime_label=classical_regime,
                confidence=0.5,
                classical_agreement=True,
                backend="stub",
                shadow_only=True,
            )
        try:
            return self._run_kernel(trace_id, features, classical_regime)
        except Exception as exc:
            logger.warning("QuantumKernelResearchService: error: %s", exc)
            return QuantumKernelResult(
                trace_id=trace_id,
                regime_label=classical_regime,
                confidence=0.5,
                classical_agreement=True,
                backend="error_fallback",
                shadow_only=True,
            )

    def _run_kernel(
        self, trace_id: str, features: list[float], classical_regime: str
    ) -> QuantumKernelResult:
        from trading_platform.quantum.schemas import QuantumKernelResult
        # Placeholder: returns classical regime until real kernel is trained
        return QuantumKernelResult(
            trace_id=trace_id,
            regime_label=classical_regime,
            confidence=0.5,
            classical_agreement=True,
            backend="qiskit_kernel_placeholder",
            shadow_only=True,
        )
