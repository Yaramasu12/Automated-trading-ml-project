from __future__ import annotations

"""D-Wave hybrid backend — lazy import; graceful unavailability when package/token missing."""

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_platform.quantum.schemas import PortfolioOptimizationRequest, QuantumBackendStatus

logger = logging.getLogger(__name__)


class DWaveBackend:
    """D-Wave hybrid CQM/BQM solver backend.

    Constrained candidate selection and portfolio subset selection.
    Disabled by default; requires DWAVE_API_TOKEN.
    """

    name = "dwave"

    def is_available(self) -> QuantumBackendStatus:
        from trading_platform.quantum.schemas import QuantumBackendStatus
        try:
            import dimod  # noqa: F401
            token = os.getenv("DWAVE_API_TOKEN", "")
            if not token:
                return QuantumBackendStatus(name=self.name, available=False, error="DWAVE_API_TOKEN not set")
            return QuantumBackendStatus(name=self.name, available=True)
        except ImportError:
            return QuantumBackendStatus(name=self.name, available=False, error="dimod/dwave-ocean not installed")

    def optimize(self, req: PortfolioOptimizationRequest) -> tuple[list[int], float] | None:
        status = self.is_available()
        if not status.available:
            logger.debug("DWaveBackend: unavailable — %s", status.error)
            return None
        try:
            return self._run_hybrid(req)
        except Exception as exc:
            logger.warning("DWaveBackend: optimization error: %s", exc)
            return None

    def _run_hybrid(self, req: PortfolioOptimizationRequest) -> tuple[list[int], float] | None:
        from trading_platform.quantum.qubo import build_qubo
        qubo = build_qubo(req)
        n = qubo.n
        if n == 0:
            return [], 0.0
        # Real D-Wave execution not yet implemented — signal unavailable so
        # the service falls back to classical rather than accepting all-zeros.
        logger.warning("DWaveBackend._run_hybrid: real execution not implemented; returning None")
        return None
