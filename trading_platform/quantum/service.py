from __future__ import annotations

"""QuantumOptimizationService — runs classical baseline + optional quantum backend.

Safety rules:
- Always runs classical baseline first.
- Quantum result must beat classical baseline by min_improvement threshold.
- Returns classical result if quantum times out, fails, or underperforms.
- Never creates OrderIntent.
"""

import logging
import threading
from typing import TYPE_CHECKING

from trading_platform.quantum.backends.classical import ClassicalOptimizerBackend
from trading_platform.quantum.qubo import check_constraints
from trading_platform.quantum.schemas import QuantumOptimizationResult

if TYPE_CHECKING:
    from trading_platform.quantum.schemas import PortfolioOptimizationRequest
    from trading_platform.trace.store import TraceStore

logger = logging.getLogger(__name__)


class QuantumOptimizationService:
    """Orchestrates classical and optional quantum optimization.

    Returns QuantumOptimizationResult — advisory, never live orders.
    """

    def __init__(
        self,
        backend: str = "classical",
        timeout: int = 3,
        min_baseline_improvement: float = 0.0,
        trace_store: TraceStore | None = None,
    ) -> None:
        self._backend_name = backend
        self._timeout = timeout
        self._min_improvement = min_baseline_improvement
        self._trace_store = trace_store
        self._classical = ClassicalOptimizerBackend()
        self._quantum_backend = self._load_quantum_backend(backend)

    def _load_quantum_backend(self, name: str):
        if name == "qiskit":
            try:
                from trading_platform.quantum.backends.qiskit_backend import QiskitBackend
                return QiskitBackend()
            except Exception:
                return None
        if name == "dwave":
            try:
                from trading_platform.quantum.backends.dwave_backend import DWaveBackend
                return DWaveBackend()
            except Exception:
                return None
        return None

    def optimize(self, req: PortfolioOptimizationRequest) -> QuantumOptimizationResult:
        """Run optimization and return the best safe result."""
        if not req.candidates:
            return self._empty_result(req.trace_id)

        # Always run classical baseline
        classical_sol, classical_obj = self._classical.optimize(req)
        classical_symbols = self._sol_to_symbols(classical_sol, req)

        result = QuantumOptimizationResult(
            trace_id=req.trace_id,
            selected_symbols=classical_symbols,
            expected_edge_sum=sum(req.candidates[i].expected_edge for i in range(len(classical_sol)) if classical_sol[i] == 1),
            risk_score=0.5,
            objective_value=classical_obj,
            backend_used="classical",
            classical_baseline_objective=classical_obj,
            beats_baseline=True,
        )

        # Optionally run quantum backend with timeout
        if self._quantum_backend is not None:
            quantum_result = self._run_quantum_with_timeout(req)
            if quantum_result is not None:
                q_sol, q_obj = quantum_result
                # Primary check: validate binary solution vector
                ok, violations = check_constraints(q_sol, req)
                improvement = q_obj - classical_obj
                if ok and improvement >= self._min_improvement:
                    q_symbols = self._sol_to_symbols(q_sol, req)
                    # Secondary check: independently validate the derived symbol basket.
                    # This catches any divergence between the binary solution and the
                    # symbol list (e.g. index mismatches, duplicate injection, or future
                    # backend bugs that mutate the solution after constraint checking).
                    sym_ok, sym_violations = self._validate_selected_symbols(q_symbols, req)
                    if sym_ok:
                        result = QuantumOptimizationResult(
                            trace_id=req.trace_id,
                            selected_symbols=q_symbols,
                            expected_edge_sum=sum(
                                req.candidates[i].expected_edge
                                for i in range(len(q_sol))
                                if q_sol[i] == 1
                            ),
                            risk_score=0.5,
                            objective_value=q_obj,
                            backend_used=self._backend_name,
                            classical_baseline_objective=classical_obj,
                            beats_baseline=True,
                            constraints_satisfied=True,
                        )
                    else:
                        logger.warning(
                            "QuantumOptimizationService: symbol basket failed secondary "
                            "constraint validation %s — falling back to classical",
                            sym_violations,
                        )
                        result.constraints_satisfied = False
                else:
                    logger.info(
                        "QuantumOptimizationService: quantum result rejected "
                        "(constraints_ok=%s, improvement=%.4f < %.4f); using classical",
                        ok, improvement, self._min_improvement,
                    )

        self._write_trace(req.trace_id, result)
        return result

    def _run_quantum_with_timeout(
        self, req: PortfolioOptimizationRequest
    ) -> tuple[list[int], float] | None:
        result_holder: list = [None]
        exc_holder: list = [None]

        def _run():
            try:
                result_holder[0] = self._quantum_backend.optimize(req)
            except Exception as exc:
                exc_holder[0] = exc

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=self._timeout)
        if t.is_alive():
            logger.warning("QuantumOptimizationService: backend timed out after %ds", self._timeout)
            return None
        if exc_holder[0]:
            logger.warning("QuantumOptimizationService: backend error: %s", exc_holder[0])
            return None
        return result_holder[0]

    @staticmethod
    def _sol_to_symbols(sol: list[int], req: PortfolioOptimizationRequest) -> list[str]:
        return [
            req.candidates[i].symbol
            for i in range(min(len(sol), len(req.candidates)))
            if sol[i] == 1
        ]

    @staticmethod
    def _validate_selected_symbols(
        selected_symbols: list[str],
        req: PortfolioOptimizationRequest,
    ) -> tuple[bool, list[str]]:
        """Secondary constraint gate applied to the derived symbol basket.

        Validates cardinality, per-symbol liquidity, and duplicate symbols
        independently of the binary solution vector.  Returns (ok, violations).
        """
        violations: list[str] = []

        # Cardinality
        if len(selected_symbols) > req.cardinality_limit:
            violations.append(
                f"cardinality {len(selected_symbols)} > limit {req.cardinality_limit}"
            )

        # Build a lookup from the request candidates for liquidity checking
        sym_to_liquidity: dict[str, float] = {c.symbol: c.liquidity_score for c in req.candidates}

        # Duplicate symbols and per-symbol liquidity
        seen: dict[str, int] = {}
        for sym in selected_symbols:
            seen[sym] = seen.get(sym, 0) + 1
            liq = sym_to_liquidity.get(sym, 1.0)
            if liq < req.min_liquidity_score:
                violations.append(
                    f"{sym} liquidity {liq:.3f} < min {req.min_liquidity_score:.3f}"
                )

        for sym, cnt in seen.items():
            if cnt > 1:
                violations.append(f"duplicate symbol {sym} appears {cnt}x in basket")

        return len(violations) == 0, violations

    @staticmethod
    def _empty_result(trace_id: str) -> QuantumOptimizationResult:
        return QuantumOptimizationResult(
            trace_id=trace_id,
            selected_symbols=[],
            expected_edge_sum=0.0,
            risk_score=0.0,
            objective_value=0.0,
            backend_used="classical",
            beats_baseline=True,
        )

    def _write_trace(self, trace_id: str, result: QuantumOptimizationResult) -> None:
        if not self._trace_store:
            return
        try:
            trace = self._trace_store.get(trace_id)
            if trace:
                trace.quantum_result_id = f"qopt-{trace_id}"
                trace.add_event("quantum_optimization", "QuantumOptimizationService",
                                {"backend": result.backend_used, "n_selected": len(result.selected_symbols)})
                self._trace_store.save(trace)
        except Exception as exc:
            logger.warning("QuantumOptimizationService: trace write error: %s", exc)

    def backend_status(self) -> list[dict]:
        statuses = [self._classical.is_available().to_dict()]
        if self._quantum_backend:
            statuses.append(self._quantum_backend.is_available().to_dict())
        return statuses
