from __future__ import annotations

"""Qiskit quantum backend — lazy import; graceful unavailability when package/token missing."""

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_platform.quantum.schemas import PortfolioOptimizationRequest, QuantumBackendStatus

logger = logging.getLogger(__name__)


class QiskitBackend:
    """IBM/Qiskit portfolio optimization backend.

    Uses QAOA-style QUBO solving. Shadow/research only until validated.
    Never blocks the trade scan when unavailable.
    """

    name = "qiskit"

    def is_available(self) -> QuantumBackendStatus:
        from trading_platform.quantum.schemas import QuantumBackendStatus
        try:
            import qiskit  # noqa: F401
            token = os.getenv("IBM_QUANTUM_TOKEN", "")
            if not token:
                return QuantumBackendStatus(name=self.name, available=False, error="IBM_QUANTUM_TOKEN not set")
            return QuantumBackendStatus(name=self.name, available=True)
        except ImportError:
            return QuantumBackendStatus(name=self.name, available=False, error="qiskit not installed")

    def optimize(self, req: PortfolioOptimizationRequest) -> tuple[list[int], float] | None:
        """Return None if unavailable."""
        status = self.is_available()
        if not status.available:
            logger.debug("QiskitBackend: unavailable — %s", status.error)
            return None
        try:
            return self._run_qaoa(req)
        except Exception as exc:
            logger.warning("QiskitBackend: optimization error: %s", exc)
            return None

    def _run_qaoa(self, req: PortfolioOptimizationRequest) -> tuple[list[int], float] | None:
        from trading_platform.quantum.qubo import build_qubo
        # Build QUBO then run QAOA sampler
        qubo = build_qubo(req)
        n = qubo.n
        if n == 0:
            return [], 0.0
        # Real QAOA execution not yet implemented — signal unavailable so
        # the service falls back to classical rather than accepting all-zeros.
        logger.warning("QiskitBackend._run_qaoa: real execution not implemented; returning None")
        return None
