from __future__ import annotations

"""QUBO builder for portfolio optimization.

Objective: maximize expected_edge
           - risk_aversion * covariance_risk
           - transaction_cost
           - concentration_penalty
           - liquidity_penalty

Constraints encoded as soft penalties in the QUBO matrix.
"""

from trading_platform.quantum.schemas import PortfolioOptimizationRequest, QuboProblem


def build_qubo(req: PortfolioOptimizationRequest) -> QuboProblem:
    """Build QUBO matrix from optimization request.

    Returns a QuboProblem with the n×n Q matrix.
    """
    candidates = req.candidates
    n = len(candidates)
    if n == 0:
        return QuboProblem(n=0, Q=[], candidate_symbols=[])

    # Diagonal: -edge + transaction_cost + liquidity_penalty
    Q = [[0.0] * n for _ in range(n)]

    for i, c in enumerate(candidates):
        edge_term = -c.expected_edge
        tx_term = c.transaction_cost
        liq_penalty = max(0.0, req.min_liquidity_score - c.liquidity_score) * 2.0
        Q[i][i] = edge_term + tx_term + liq_penalty

    # Off-diagonal: risk correlation penalty + concentration penalty
    for i in range(n):
        for j in range(i + 1, n):
            ci = candidates[i]
            cj = candidates[j]
            # Same symbol (different sides) — concentration penalty
            if ci.symbol == cj.symbol:
                penalty = 1.0
            else:
                # Covariance proxy: both high risk → penalty
                risk_cov = req.risk_aversion * ci.risk_estimate * cj.risk_estimate
                penalty = risk_cov * 0.5
            Q[i][j] = penalty
            Q[j][i] = penalty

    # Cardinality constraint: penalize selecting more than cardinality_limit
    # Encoded as: P * (sum_i x_i - K)^2 expanded
    k = req.cardinality_limit
    P = 2.0  # cardinality penalty weight
    for i in range(n):
        Q[i][i] += P * (1 - 2 * k)
        for j in range(i + 1, n):
            Q[i][j] += P * 2
            Q[j][i] += P * 2

    constraints = [
        f"cardinality <= {k}",
        f"min_liquidity >= {req.min_liquidity_score}",
        f"max_single_symbol_pct <= {req.max_single_symbol_pct}",
        f"max_drawdown_heat <= {req.max_drawdown_heat}",
    ]

    return QuboProblem(
        n=n,
        Q=Q,
        candidate_symbols=[c.symbol for c in candidates],
        constraint_descriptions=constraints,
    )


def evaluate_solution(
    binary_solution: list[int],
    req: PortfolioOptimizationRequest,
) -> float:
    """Compute the objective value for a binary selection vector."""
    candidates = req.candidates
    n = len(binary_solution)
    if n == 0:
        return 0.0

    obj = 0.0
    selected = [candidates[i] for i in range(min(n, len(candidates))) if binary_solution[i] == 1]

    for c in selected:
        obj += c.expected_edge
        obj -= c.transaction_cost
        obj -= max(0.0, req.min_liquidity_score - c.liquidity_score) * 2.0

    # Pairwise risk penalty
    for i in range(len(selected)):
        for j in range(i + 1, len(selected)):
            obj -= req.risk_aversion * selected[i].risk_estimate * selected[j].risk_estimate * 0.5

    # Cardinality penalty
    k = sum(binary_solution[:len(candidates)])
    if k > req.cardinality_limit:
        obj -= 2.0 * (k - req.cardinality_limit) ** 2

    return obj


def check_constraints(
    binary_solution: list[int],
    req: PortfolioOptimizationRequest,
) -> tuple[bool, list[str]]:
    """Return (all_satisfied, list_of_violations)."""
    violations: list[str] = []
    candidates = req.candidates
    n = min(len(binary_solution), len(candidates))
    selected_idx = [i for i in range(n) if binary_solution[i] == 1]
    selected = [candidates[i] for i in selected_idx]

    if len(selected) > req.cardinality_limit:
        violations.append(f"cardinality {len(selected)} > {req.cardinality_limit}")

    for c in selected:
        if c.liquidity_score < req.min_liquidity_score:
            violations.append(f"{c.symbol} liquidity {c.liquidity_score:.2f} < {req.min_liquidity_score}")

    # Concentration check: same symbol more than once
    sym_counts: dict[str, int] = {}
    for c in selected:
        sym_counts[c.symbol] = sym_counts.get(c.symbol, 0) + 1
    for sym, cnt in sym_counts.items():
        if cnt > 1:
            violations.append(f"duplicate symbol {sym} selected {cnt}x")

    return len(violations) == 0, violations
