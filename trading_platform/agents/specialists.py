from __future__ import annotations

"""Specialist agents for the AI Council.

Each agent receives AgentInputContext, calls the local model gateway,
and returns a typed AgentVote. No agent creates OrderIntent.

RAG evidence propagation
------------------------
Every _safe_vote() call merges ctx.evidence_ids (pre-retrieved by the
AgentCouncilSupervisor before agents run) with the evidence_ids returned
by the gateway's own per-call RAG retrieval.  This ensures the full
evidence chain is preserved in each AgentVote for traceability.
"""

import logging
from typing import TYPE_CHECKING

from trading_platform.agents.schemas import (
    AgentVote,
    ExecutionAdvice,
    PortfolioProposal,
    RiskCritique,
    StrategyProposal,
)

if TYPE_CHECKING:
    from trading_platform.agents.model_gateway import LocalModelGateway
    from trading_platform.agents.schemas import AgentInputContext

logger = logging.getLogger(__name__)

_SYSTEM_BASE = (
    "You are a specialist trading analyst. "
    "Respond in strict JSON with keys: action (BUY|SELL|HOLD|REDUCE|HALT|HEDGE), "
    "confidence (0.0-1.0), reasoning (string), evidence_ids (list[str]). "
    "Never include broker credentials, API keys, or passwords."
)

_VALID_ACTIONS = frozenset({"BUY", "SELL", "HOLD", "REDUCE", "HALT", "HEDGE"})


def _safe_vote(
    agent_name: str,
    model_id: str,
    response: dict,
    ctx_evidence_ids: list[str] | None = None,
) -> AgentVote:
    """Build a validated AgentVote, merging context + gateway evidence_ids."""
    action = response.get("action", "HOLD")
    if action not in _VALID_ACTIONS:
        action = "HOLD"

    # Merge supervisor-level pre-retrieved IDs with gateway-retrieved IDs.
    # dict.fromkeys preserves order and deduplicates.
    gateway_ids: list[str] = list(response.get("evidence_ids") or [])
    merged_ids = list(dict.fromkeys((ctx_evidence_ids or []) + gateway_ids))

    return AgentVote(
        agent_name=agent_name,
        action=action,
        confidence=float(response.get("confidence", 0.5)),
        reasoning=str(response.get("reasoning", ""))[:500],
        evidence_ids=merged_ids,
        model_id=model_id,
        failure_mode=response.get("failure_mode"),
    )


# ── Strategy agents ───────────────────────────────────────────────────────────

class NewsMacroAgent:
    name = "NewsMacroAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        prompt = (
            f"Symbols: {ctx.symbols}. Regime: {ctx.market_regime}. "
            "Assess macro/news event risk. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class QuantResearchAgent:
    name = "QuantResearchAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        prompt = (
            f"Symbols: {ctx.symbols}. "
            "Analyze feature importance, strategy decay signals, and alpha hypotheses. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-31b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-31b"), resp, ctx.evidence_ids)


class TrendMomentumAgent:
    name = "TrendMomentumAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        prompt = (
            f"Symbols: {ctx.symbols}. Regime: {ctx.market_regime}. "
            "Assess trend and momentum strength. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class MeanReversionAgent:
    name = "MeanReversionAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        prompt = (
            f"Symbols: {ctx.symbols}. Regime: {ctx.market_regime}. "
            "Assess mean reversion opportunity using z-score and bollinger bands. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class BreakoutAgent:
    name = "BreakoutAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        prompt = (
            f"Symbols: {ctx.symbols}. "
            "Detect breakout setups from consolidation ranges. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class GapEventAgent:
    name = "GapEventAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        prompt = (
            f"Symbols: {ctx.symbols}. "
            "Assess gap fill or gap continuation potential based on overnight events. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class PairsStatArbAgent:
    name = "PairsStatArbAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        prompt = (
            f"Symbols: {ctx.symbols}. "
            "Identify pairs trading and statistical arbitrage opportunities. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class OptionsVolatilityAgent:
    name = "OptionsVolatilityAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        prompt = (
            f"Symbols: {ctx.symbols}. "
            "Assess implied volatility surface, term structure skew, and vol-of-vol signals. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-26b-moe", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-26b-moe"), resp, ctx.evidence_ids)


class FuturesCarryAgent:
    name = "FuturesCarryAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        prompt = (
            f"Symbols: {ctx.symbols}. "
            "Evaluate futures carry, basis, and roll yield opportunities. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class HedgeBuilderAgent:
    name = "HedgeBuilderAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        prompt = (
            f"Portfolio state: {ctx.portfolio_state}. Symbols: {ctx.symbols}. "
            "Recommend hedges to reduce tail risk without excessive cost. "
            "Output JSON: action (HEDGE/HOLD), confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


# ── Evaluation agents (non-vote return types) ─────────────────────────────────

class RiskCriticAgent:
    name = "RiskCriticAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext, proposals: list[StrategyProposal]) -> RiskCritique:
        system = (
            "You are a risk critic. Try to identify weaknesses before the risk engine. "
            "Respond in JSON with keys: veto (bool), risk_score (0-1), concerns (list), "
            "recommended_action (PROCEED|REDUCE|HALT), evidence_ids (list)."
        )
        prompt = (
            f"Proposals: {[p.to_dict() for p in proposals]}. "
            f"Portfolio: {ctx.portfolio_state}. Regime: {ctx.market_regime}. "
            "Identify crowding, stale data, event risk, high slippage, or model disagreement."
        )
        resp = self._gw.generate("gemma4-31b", system, prompt)
        veto = bool(resp.get("veto", False))
        risk_score = float(resp.get("risk_score", 0.3))
        recommended = resp.get("recommended_action", "PROCEED")
        if recommended not in ("PROCEED", "REDUCE", "HALT"):
            recommended = "PROCEED"

        # Merge supervisor-level pre-retrieved ids with gateway-retrieved ids.
        gateway_ids: list[str] = list(resp.get("evidence_ids") or [])
        merged_ids = list(dict.fromkeys(list(ctx.evidence_ids) + gateway_ids))

        return RiskCritique(
            veto=veto,
            risk_score=risk_score,
            concerns=list(resp.get("concerns", [])),
            recommended_action=recommended,
            evidence_ids=merged_ids,
            model_id=resp.get("model_id", "gemma4-31b"),
        )


class ExecutionAnalystAgent:
    name = "ExecutionAnalystAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> ExecutionAdvice:
        system = (
            "You are an execution analyst. "
            "Respond in JSON with keys: avoid_windows (list), preferred_order_type (string), "
            "max_slice_size_pct (float 0-1), reasoning (string)."
        )
        prompt = (
            f"Symbols: {ctx.symbols}. Portfolio: {ctx.portfolio_state}. "
            "Analyze fill quality risk, spread, and volume. Advise on order slicing."
        )
        resp = self._gw.generate("gemma4-e4b", system, prompt)
        pref = resp.get("preferred_order_type", "LIMIT")
        if pref not in ("MARKET", "LIMIT", "STOPLOSS"):
            pref = "LIMIT"
        return ExecutionAdvice(
            avoid_windows=list(resp.get("avoid_windows", [])),
            preferred_order_type=pref,
            max_slice_size_pct=float(resp.get("max_slice_size_pct", 1.0)),
            reasoning=str(resp.get("reasoning", ""))[:300],
            model_id=resp.get("model_id", "gemma4-e4b"),
        )


class PortfolioManagerAgent:
    name = "PortfolioManagerAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext, proposals: list[StrategyProposal]) -> PortfolioProposal:
        system = (
            "You are a portfolio manager. "
            "Respond in JSON with keys: preferred_basket (list[str]), "
            "expected_return_estimate (float), max_heat (float 0-1), "
            "hedge_request (str|null), target_run_rate_ok (bool), reasoning (str)."
        )
        prompt = (
            f"Proposals: {[p.to_dict() for p in proposals]}. "
            f"Portfolio: {ctx.portfolio_state}. "
            "Build optimal basket respecting capital and risk limits."
        )
        resp = self._gw.generate("gemma4-26b-moe", system, prompt)
        return PortfolioProposal(
            preferred_basket=list(resp.get("preferred_basket", [])),
            expected_return_estimate=float(resp.get("expected_return_estimate", 0.0)),
            max_heat=float(resp.get("max_heat", 0.5)),
            hedge_request=resp.get("hedge_request"),
            target_run_rate_ok=bool(resp.get("target_run_rate_ok", True)),
            reasoning=str(resp.get("reasoning", ""))[:300],
            model_id=resp.get("model_id", "gemma4-26b-moe"),
        )
