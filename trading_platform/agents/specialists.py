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


def _build_market_context(ctx: AgentInputContext) -> str:
    """Build a compact market context block from all available ctx fields.

    Included in every specialist prompt so agents reason from real data
    rather than returning generic HOLD responses.
    """
    lines: list[str] = [f"SYMBOLS: {', '.join(ctx.symbols)}"]
    lines.append(f"REGIME: {ctx.market_regime}")

    # Portfolio state
    ps = ctx.portfolio_state
    if ps:
        drawdown = ps.get("drawdown", ps.get("peak_drawdown", None))
        cum_pnl = ps.get("cum_pnl", ps.get("daily_pnl", None))
        open_pos = ps.get("open_positions", ps.get("positions", None))
        equity = ps.get("equity", ps.get("capital", None))
        parts = []
        if drawdown is not None:
            parts.append(f"drawdown={drawdown:.2%}" if isinstance(drawdown, float) else f"drawdown={drawdown}")
        if cum_pnl is not None:
            parts.append(f"cum_pnl={cum_pnl}")
        if open_pos is not None:
            parts.append(f"open_positions={open_pos}")
        if equity is not None:
            parts.append(f"equity={equity}")
        if parts:
            lines.append("PORTFOLIO: " + ", ".join(parts))

    # Feature signals (prices, indicators, candidate signals)
    feats = ctx.features
    if feats:
        feat_parts: list[str] = []
        for key in ("regime", "close", "momentum_5", "momentum_20", "realized_volatility",
                    "volume_ratio", "trend_strength", "rsi_14", "atr_14", "bb_width",
                    "momentum_alignment", "direction_probability", "expected_return",
                    "predicted_volatility", "tail_risk_score", "top_signals"):
            val = feats.get(key)
            if val is not None:
                if isinstance(val, float):
                    feat_parts.append(f"{key}={val:.4f}")
                else:
                    feat_parts.append(f"{key}={val}")
        # Include any other numeric features
        for k, v in feats.items():
            if k not in ("_debate",) and k not in {p.split("=")[0] for p in feat_parts}:
                if isinstance(v, (int, float)):
                    feat_parts.append(f"{k}={v}")
        if feat_parts:
            lines.append("INDICATORS: " + ", ".join(feat_parts[:15]))

    return "\n".join(lines)


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
        mkt = _build_market_context(ctx)
        prompt = (
            f"{mkt}\n"
            "Assess macro/news event risk for the symbols above. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class QuantResearchAgent:
    name = "QuantResearchAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        mkt = _build_market_context(ctx)
        prompt = (
            f"{mkt}\n"
            "Analyze feature importance, strategy decay signals, and alpha hypotheses "
            "given the indicators above. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-31b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-31b"), resp, ctx.evidence_ids)


class TrendMomentumAgent:
    name = "TrendMomentumAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        mkt = _build_market_context(ctx)
        prompt = (
            f"{mkt}\n"
            "Assess trend and momentum strength using the momentum and trend_strength indicators above. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class MeanReversionAgent:
    name = "MeanReversionAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        mkt = _build_market_context(ctx)
        prompt = (
            f"{mkt}\n"
            "Assess mean reversion opportunity using rsi_14, bb_width, and realized_volatility above. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class BreakoutAgent:
    name = "BreakoutAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        mkt = _build_market_context(ctx)
        prompt = (
            f"{mkt}\n"
            "Detect breakout setups from consolidation ranges using volume_ratio and atr_14 above. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class GapEventAgent:
    name = "GapEventAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        mkt = _build_market_context(ctx)
        prompt = (
            f"{mkt}\n"
            "Assess gap fill or gap continuation potential based on overnight events and the indicators above. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class PairsStatArbAgent:
    name = "PairsStatArbAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        mkt = _build_market_context(ctx)
        prompt = (
            f"{mkt}\n"
            "Identify pairs trading and statistical arbitrage opportunities among the symbols above. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class OptionsVolatilityAgent:
    name = "OptionsVolatilityAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        """REAL volatility assessment from the GARCH forecast + tail risk. It has
        no *directional* edge (returns are unpredictable), so it honestly votes
        HOLD — but its confidence and reasoning reflect the actual vol regime,
        which is the signal that genuinely informs short-vol (condor) decisions."""
        f = ctx.features or {}
        pred_vol = float(f.get("predicted_volatility", 0.0) or 0.0)   # annualised fraction
        tail_risk = float(f.get("tail_risk_score", 0.0) or 0.0)
        pct = pred_vol * 100.0

        if tail_risk >= 0.6 or pct >= 30.0:
            regime, conf = "elevated", 0.65
            reason = f"forecast vol {pct:.0f}% / tail-risk {tail_risk:.2f} — vol-selling risky, favour caution"
        elif pct <= 12.0:
            regime, conf = "subdued", 0.6
            reason = f"forecast vol {pct:.0f}% subdued — premium likely thin, be selective on short-vol"
        else:
            regime, conf = "normal", 0.5
            reason = f"forecast vol {pct:.0f}% normal — no directional edge; short-vol depends on VRP"

        # No directional edge -> HOLD; the value is the vol-regime read, not a call.
        resp = {"action": "HOLD", "confidence": conf,
                "reasoning": f"vol regime={regime}: {reason}", "evidence_ids": []}
        return _safe_vote(self.name, "options_vol_garch_v1", resp, ctx.evidence_ids)


class FuturesCarryAgent:
    name = "FuturesCarryAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        mkt = _build_market_context(ctx)
        prompt = (
            f"{mkt}\n"
            "Evaluate futures carry, basis, and roll yield opportunities for the symbols above. "
            "Output JSON: action, confidence, reasoning, evidence_ids."
        )
        resp = self._gw.generate("gemma4-e4b", _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", "gemma4-e4b"), resp, ctx.evidence_ids)


class HedgeBuilderAgent:
    name = "HedgeBuilderAgent"

    def __init__(self, gateway: LocalModelGateway) -> None:
        self._gw = gateway

    def run(self, ctx: AgentInputContext) -> AgentVote:
        mkt = _build_market_context(ctx)
        prompt = (
            f"{mkt}\n"
            "Recommend hedges to reduce tail risk without excessive cost, "
            "considering the portfolio drawdown and tail_risk_score above. "
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
        """REAL risk critique computed from actual market/portfolio numbers — not
        an LLM guess. Combines tail-risk, drawdown, and forecast vol into a risk
        score and a PROCEED/REDUCE/HALT recommendation before the RiskEngine's
        hard gates. Deterministic and auditable."""
        f = ctx.features or {}
        p = ctx.portfolio_state or {}
        tail_risk = float(f.get("tail_risk_score", 0.0) or 0.0)
        pred_vol = float(f.get("predicted_volatility", 0.0) or 0.0)   # annualised fraction
        drawdown = float(p.get("drawdown", 0.0) or 0.0)

        concerns: list[str] = []
        if tail_risk >= 0.6:
            concerns.append(f"elevated tail-risk {tail_risk:.2f}")
        if drawdown >= 0.05:
            concerns.append(f"drawdown {drawdown:.1%}")
        if pred_vol >= 0.30:
            concerns.append(f"high forecast vol {pred_vol:.0%}")

        # Blended, bounded risk score from real inputs.
        risk_score = min(1.0, 0.55 * tail_risk + 4.0 * drawdown + 0.6 * min(pred_vol, 0.5))

        # Recommendation ladder tied to hard-ish thresholds.
        if drawdown >= 0.09 or risk_score >= 0.85:
            recommended, veto = "HALT", True
        elif risk_score >= 0.55:
            recommended, veto = "REDUCE", False
        else:
            recommended, veto = "PROCEED", False

        return RiskCritique(
            veto=veto,
            risk_score=round(risk_score, 3),
            concerns=concerns or ["no elevated risk signals"],
            recommended_action=recommended,
            evidence_ids=list(ctx.evidence_ids),
            model_id="risk_critic_rules_v1",
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
