from __future__ import annotations

"""Classical optimizer backend — exact enumeration for small N, greedy for larger N.

Deterministic, always available, validates all constraints.
"""

import itertools
import logging
from typing import TYPE_CHECKING

from trading_platform.quantum.qubo import check_constraints, evaluate_solution

if TYPE_CHECKING:
    from trading_platform.quantum.schemas import PortfolioOptimizationRequest, QuantumBackendStatus

logger = logging.getLogger(__name__)

_EXACT_THRESHOLD = 20   # use exact enumeration for N <= this


class ClassicalOptimizerBackend:
    """Deterministic classical optimizer. Never fails; used as baseline."""

    name = "classical"

    def is_available(self) -> QuantumBackendStatus:
        from trading_platform.quantum.schemas import QuantumBackendStatus
        return QuantumBackendStatus(name=self.name, available=True)

    def optimize(self, req: PortfolioOptimizationRequest) -> tuple[list[int], float]:
        """Return (binary_solution, objective_value)."""
        n = len(req.candidates)
        if n == 0:
            return [], 0.0

        if n <= _EXACT_THRESHOLD:
            return self._exact(n, req)
        return self._greedy(n, req)

    def _exact(self, n: int, req: PortfolioOptimizationRequest) -> tuple[list[int], float]:
        best_sol: list[int] = [0] * n
        best_obj = -1e18

        for bits in itertools.product([0, 1], repeat=n):
            sol = list(bits)
            ok, _ = check_constraints(sol, req)
            if not ok:
                continue
            obj = evaluate_solution(sol, req)
            if obj > best_obj:
                best_obj = obj
                best_sol = sol

        return best_sol, best_obj

    def _greedy(self, n: int, req: PortfolioOptimizationRequest) -> tuple[list[int], float]:
        candidates = req.candidates
        # Sort by edge-to-risk ratio descending
        ranked = sorted(range(n), key=lambda i: (
            candidates[i].expected_edge / max(candidates[i].risk_estimate, 1e-6)
        ), reverse=True)

        sol = [0] * n
        selected_syms: set[str] = set()

        for idx in ranked:
            c = candidates[idx]
            if len(selected_syms) >= req.cardinality_limit:
                break
            if c.symbol in selected_syms:
                continue  # no duplicate underlying
            if c.liquidity_score < req.min_liquidity_score:
                continue
            sol[idx] = 1
            selected_syms.add(c.symbol)

        obj = evaluate_solution(sol, req)
        return sol, obj
