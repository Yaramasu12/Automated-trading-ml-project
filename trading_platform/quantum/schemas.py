from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class QuantumCandidate:
    """One trade candidate for portfolio optimization."""
    symbol: str
    side: str
    expected_edge: float
    risk_estimate: float
    transaction_cost: float = 0.001
    liquidity_score: float = 1.0
    lot_size: int = 1
    margin_estimate: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "expected_edge": self.expected_edge,
            "risk_estimate": self.risk_estimate,
            "transaction_cost": self.transaction_cost,
            "liquidity_score": self.liquidity_score,
        }


@dataclass
class PortfolioOptimizationRequest:
    """Input to the quantum/classical portfolio optimizer."""
    trace_id: str
    candidates: list[QuantumCandidate]
    risk_aversion: float = 1.0
    cardinality_limit: int = 4
    budget: float = 1.0
    max_single_symbol_pct: float = 0.30
    min_liquidity_score: float = 0.3
    max_drawdown_heat: float = 0.10
    max_strategy_concentration: float = 0.50
    target_run_rate_pressure: float = 0.0  # 0=no pressure, 1=max pressure

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "n_candidates": len(self.candidates),
            "risk_aversion": self.risk_aversion,
            "cardinality_limit": self.cardinality_limit,
        }


@dataclass
class QuboProblem:
    """QUBO matrix encoding of portfolio optimization."""
    n: int
    Q: list[list[float]]  # n×n QUBO matrix
    candidate_symbols: list[str]
    constraint_descriptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "candidate_symbols": self.candidate_symbols,
            "constraints": self.constraint_descriptions,
        }


@dataclass
class QuantumOptimizationResult:
    """Result from quantum or classical optimizer."""
    trace_id: str
    selected_symbols: list[str]
    expected_edge_sum: float
    risk_score: float
    objective_value: float
    backend_used: str           # "classical" | "qiskit" | "dwave"
    classical_baseline_objective: float = 0.0
    beats_baseline: bool = True
    constraints_satisfied: bool = True
    stable: bool = True
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "selected_symbols": self.selected_symbols,
            "expected_edge_sum": self.expected_edge_sum,
            "risk_score": self.risk_score,
            "objective_value": self.objective_value,
            "backend_used": self.backend_used,
            "classical_baseline_objective": self.classical_baseline_objective,
            "beats_baseline": self.beats_baseline,
            "constraints_satisfied": self.constraints_satisfied,
            "stable": self.stable,
            "ts": self.ts.isoformat(),
        }


@dataclass
class QuantumBackendStatus:
    name: str
    available: bool
    error: str | None = None
    latency_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "available": self.available,
            "error": self.error,
            "latency_ms": self.latency_ms,
        }


@dataclass
class QuantumKernelResult:
    """Result from quantum kernel classifier (shadow research only)."""
    trace_id: str
    regime_label: str
    confidence: float
    classical_agreement: bool
    backend: str = "qiskit_kernel"
    shadow_only: bool = True

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "regime_label": self.regime_label,
            "confidence": round(self.confidence, 4),
            "classical_agreement": self.classical_agreement,
            "backend": self.backend,
            "shadow_only": self.shadow_only,
        }
