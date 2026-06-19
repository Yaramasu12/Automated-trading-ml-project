"""QuantumLabService — quantum status & preview endpoints, extracted from runtime.

Read-only/advisory diagnostics for the (classical-fallback) quantum optimizer.
Deps injected under their EXACT runtime attribute names so the bodies are a
verbatim move. Part of the ongoing runtime god-object decomposition.
"""
from __future__ import annotations

from typing import Any

from trading_platform.trace.ids import new_trace_id
from trading_platform.quantum.service import QuantumOptimizationService
from trading_platform.quantum.schemas import PortfolioOptimizationRequest, QuantumCandidate


class QuantumLabService:
    def __init__(self, *, quantum_service: Any, quantum_kernel_service: Any,
                 settings: Any, trace_store: Any) -> None:
        self._quantum_service = quantum_service
        self._quantum_kernel_service = quantum_kernel_service
        self.settings = settings
        self.trace_store = trace_store

    def quantum_status(self) -> dict:
        if self._quantum_service is None:
            preview_service = QuantumOptimizationService(
                backend="classical",
                timeout=self.settings.quantum_timeout_seconds,
                min_baseline_improvement=self.settings.quantum_min_baseline_improvement,
                trace_store=self.trace_store,
            )
            return {
                "enabled": self.settings.enable_quantum_lab,
                "backend": self.settings.quantum_backend,
                "timeout_seconds": self.settings.quantum_timeout_seconds,
                "backends": preview_service.backend_status(),
                "preview_fallback": "classical",
                "note": "Quantum Lab is disabled; manual previews use the safe classical optimizer.",
            }
        return {
            "enabled": self.settings.enable_quantum_lab,
            "backend": self.settings.quantum_backend,
            "timeout_seconds": self.settings.quantum_timeout_seconds,
            "backends": self._quantum_service.backend_status(),
        }

    def quantum_kernel_status(self) -> dict:
        """Return quantum kernel research service status and shadow-only flag."""
        if self._quantum_kernel_service is None:
            return {"available": False, "shadow_only": True, "note": "quantum_lab not enabled"}
        available = self._quantum_kernel_service.is_available()
        return {
            "available": available,
            "shadow_only": True,
            "note": (
                "Quantum kernel classifier runs in shadow mode — results are logged and "
                "returned in scan output but never influence live trade decisions."
            ),
        }

    def quantum_optimize_preview(self, payload: dict) -> dict:
        candidates_raw = payload.get("candidates", [])
        trace_id = new_trace_id("qpreview")
        candidates = [
            QuantumCandidate(
                symbol=c.get("symbol", "UNKNOWN"),
                side=c.get("side", "BUY"),
                expected_edge=float(c.get("expected_edge", 0.01)),
                risk_estimate=float(c.get("risk_estimate", 0.5)),
            )
            for c in candidates_raw
        ]
        req = PortfolioOptimizationRequest(
            trace_id=trace_id,
            candidates=candidates,
            risk_aversion=self.settings.quantum_risk_aversion,
            cardinality_limit=self.settings.quantum_cardinality_limit,
        )
        service = self._quantum_service
        if service is None:
            service = QuantumOptimizationService(
                backend="classical",
                timeout=self.settings.quantum_timeout_seconds,
                min_baseline_improvement=self.settings.quantum_min_baseline_improvement,
                trace_store=self.trace_store,
            )
        result = service.optimize(req)
        return result.to_dict()
