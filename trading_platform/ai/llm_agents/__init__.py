"""
trading_platform/ai/llm_agents/__init__.py — LangGraph LLM council agents

*** RETIRED 2026-08-29 — see README.md in this directory. ***
Zero callers anywhere in the codebase (confirmed 2026-08-28). The AI council
that actually runs is trading_platform/agents/ (specialists.py, supervisor.py,
voting.py) — a separate framework. Do not import from this package or build
new work on it; port any wanted idea into agents/specialists.py's existing
AgentVote/_safe_vote() contract instead. Kept in the tree for reference, not
deleted, per an explicit decision to retire rather than migrate to it.

Per §8 (REDESIGN_PROMPT): Local LLM agents with veto-only power.
All agents use LM Studio's local OpenAI-compatible API.
No agent initiates trades or upsizes positions — veto and downsize only.

Named `llm_agents` (not `agents`) because `trading_platform.ai.agents` is
already a pre-existing, load-bearing module (ModelPerformance, RetrainingAgent,
RiskSupervisorAgent, MarketRegimeAgent, ...) imported by `api/runtime.py`.
A same-named package here would shadow that module entirely.

Agents:
- regime.py: Regime Analyst (daily+intraday regime detection)
- veto.py: Signal Veto Agent (reviews entries against RAG context)
- journalist.py: Trade Journalist (structured postmortem per trade)
- copilot.py: Copilot chat agent (explains decisions, NL→backtest config)
- compliance.py: Compliance Watcher (flags SEBI/exchange circulars)
"""

from .regime import RegimeAnalyst, RegimeClassification, RegimeDisagreementReport
from .veto import SignalVetoAgent, VetoDecision, VetoAction
from .journalist import TradeJournalist, TradePostmortem
from .copilot import CopilotAgent, CopilotResponse
from .compliance import ComplianceWatcherAgent, ComplianceFlag

__all__ = [
    "RegimeAnalyst",
    "RegimeClassification",
    "RegimeDisagreementReport",
    "SignalVetoAgent",
    "VetoDecision",
    "VetoAction",
    "TradeJournalist",
    "TradePostmortem",
    "CopilotAgent",
    "CopilotResponse",
    "ComplianceWatcherAgent",
    "ComplianceFlag",
]
