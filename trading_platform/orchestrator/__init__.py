"""Master Orchestrator package.

Implements a LangGraph-inspired stateful node graph for profit-focused
trading decisions, integrating patterns from:
  - CrewAI (specialist crew with role-based agents)
  - AutoGen (multi-agent debate and consensus)
  - LangGraph (typed state machine with conditional edges)
  - Adaptive RAG (self-correcting market pattern retrieval)
  - Reflection agents (post-trade outcome learning)
"""
from trading_platform.orchestrator.master_orchestrator import MasterOrchestrator
from trading_platform.orchestrator.state import OrchestratorState, NodeResult

__all__ = ["MasterOrchestrator", "OrchestratorState", "NodeResult"]
