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
import os
from typing import TYPE_CHECKING, Any, Sequence

from trading_platform.ai.guardrails import scan_for_directive_language
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


def _safe_float(value: Any, default: float, lo: float | None = None, hi: float | None = None) -> float:
    """Coerce a model-returned field to float, defaulting on any failure
    instead of raising. Confirmed live 2026-09-01: google/gemma-4-e4b
    returned confidence="Medium" -- a descriptive string, not a number --
    inside otherwise-valid JSON. The old bare float(response["confidence"])
    raised ValueError uncaught here, which the caller's try/except then
    turned into an "agent_error" stub -- discarding an otherwise-usable
    vote (a real action + substantive reasoning) purely because of one
    malformed field, indistinguishable from a genuine agent failure."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        result = max(lo, result)
    if hi is not None:
        result = min(hi, result)
    return result


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

    reasoning = str(response.get("reasoning", ""))[:500]
    # action is already a closed enum (validated above) so this can't hijack
    # the actual decision — but a prompt-injection attack that got past
    # wrap_untrusted_content() could still try to plant directive-sounding
    # text in the free-text reasoning shown to a human operator. Flag, don't
    # silently strip: an analyst mentioning "the model considered placing an
    # order" is legitimate content, so this is a signal to look at, not a
    # verdict to act on automatically.
    scan = scan_for_directive_language(reasoning)
    if scan.flagged:
        logger.warning(
            "specialist %s reasoning matched directive-language patterns %s — review for prompt injection: %r",
            agent_name, scan.matched_patterns, reasoning[:200],
        )

    return AgentVote(
        agent_name=agent_name,
        action=action,
        confidence=_safe_float(response.get("confidence"), 0.5, 0.0, 1.0),
        reasoning=reasoning,
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
        resp = self._gw.generate(self._gw.fast_model, _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", self._gw.fast_model), resp, ctx.evidence_ids)


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
        resp = self._gw.generate(self._gw.primary_model, _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", self._gw.primary_model), resp, ctx.evidence_ids)


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
        resp = self._gw.generate(self._gw.fast_model, _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", self._gw.fast_model), resp, ctx.evidence_ids)


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
        resp = self._gw.generate(self._gw.fast_model, _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", self._gw.fast_model), resp, ctx.evidence_ids)


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
        resp = self._gw.generate(self._gw.fast_model, _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", self._gw.fast_model), resp, ctx.evidence_ids)


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
        resp = self._gw.generate(self._gw.fast_model, _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", self._gw.fast_model), resp, ctx.evidence_ids)


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
        resp = self._gw.generate(self._gw.fast_model, _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", self._gw.fast_model), resp, ctx.evidence_ids)


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
        resp = self._gw.generate(self._gw.fast_model, _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", self._gw.fast_model), resp, ctx.evidence_ids)


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
        resp = self._gw.generate(self._gw.fast_model, _SYSTEM_BASE, prompt)
        return _safe_vote(self.name, resp.get("model_id", self._gw.fast_model), resp, ctx.evidence_ids)


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
        resp = self._gw.generate(self._gw.fast_model, system, prompt)
        pref = resp.get("preferred_order_type", "LIMIT")
        if pref not in ("MARKET", "LIMIT", "STOPLOSS"):
            pref = "LIMIT"
        return ExecutionAdvice(
            avoid_windows=list(resp.get("avoid_windows", [])),
            preferred_order_type=pref,
            max_slice_size_pct=_safe_float(resp.get("max_slice_size_pct"), 1.0, 0.0, 1.0),
            reasoning=str(resp.get("reasoning", ""))[:300],
            model_id=resp.get("model_id", self._gw.fast_model),
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
        resp = self._gw.generate(self._gw.coordinator_model, system, prompt)
        return PortfolioProposal(
            preferred_basket=list(resp.get("preferred_basket", [])),
            expected_return_estimate=_safe_float(resp.get("expected_return_estimate"), 0.0),
            max_heat=_safe_float(resp.get("max_heat"), 0.5, 0.0, 1.0),
            hedge_request=resp.get("hedge_request"),
            target_run_rate_ok=bool(resp.get("target_run_rate_ok", True)),
            reasoning=str(resp.get("reasoning", ""))[:300],
            model_id=resp.get("model_id", self._gw.coordinator_model),
        )


# ─── Batched multi-instrument analysis (cost control) ────────────────────────
#
# WHY: a specialist call costs ~6-25s, and the council runs once PER
# UNDERLYING across 58 underlyings per 300s cycle. One call per (agent,
# instrument) is 580 calls/cycle — measured at ~1.2h, ~15x over budget, which
# manifested not as a slow cycle but as SILENT DEGRADATION (per-agent timeouts
# fired and every vote fell back to a canned stub).
#
# Batching asks ONE call to judge N instruments, cutting calls by ~N. Measured
# projection for the full 58-underlying universe:
#     per-instrument calls : ~4,400s (1.2h)   <- infeasible
#     batches of 10        :   ~108s          <- fits inside a 300s cycle
#
# ROBUSTNESS: the model is asked to echo each instrument's symbol, and results
# are matched BY SYMBOL, never by array position. A model that reorders, drops,
# or hallucinates an extra entry then degrades only the instruments it actually
# missed — those callers get a stub, everything else keeps its real verdict.
# Position-matching would silently attribute one instrument's verdict to
# another, which is far worse than a stub: a wrong-but-confident vote.

# NOTE: must be a JSON OBJECT wrapping the array, not a bare array.
# LocalModelGateway._parse_json rejects anything that is not a dict
# ("Expected JSON object") and returns a stub, so a top-level array never
# reaches parse_batch_response — verified live: 10/10 instruments stubbed.
_BATCH_INSTRUCTION = (
    "You are judging MULTIPLE instruments in one response.\n"
    'Return ONLY a JSON object of the form {"results": [...]}, with one entry '
    "per instrument. Each entry MUST echo the instrument's exact symbol in a "
    '"symbol" field so results can be matched.\n'
    '{"results":[{"symbol":"<exact symbol>","action":"BUY|SELL|HOLD",'
    '"confidence":0.0-1.0,"reasoning":"<one short sentence>"}]}\n'
    "Do not merge instruments. Do not omit any. Keep each reasoning under 25 words."
)


# Instruments per batched call. 20 measured at 40.6s for 20/20 real verdicts
# (2.03s per instrument) vs 30.9s for 10 (3.09s) — bigger batches amortise the
# shared prompt better.
#
# MEASURED CEILING: batching and CONCURRENCY trade off against each other on
# this hardware. A single batch-20 call succeeds, but 4-8 CONCURRENT batch-20
# calls returned 100% stubs (80/80 and 160/160) while still taking 97-137s —
# i.e. the calls were made and failed, not queued. LM Studio is served with
# PARALLEL=4 and cannot hold that many large prompts at once. So raise batch
# size OR gateway concurrency, not both; verify with a real run after changing
# either.
DEFAULT_BATCH_SIZE = int(os.getenv("COUNCIL_BATCH_SIZE", "20"))
# Output-token budgeting for batched replies (measured: ~120 tokens per verdict
# including its reasoning sentence, plus JSON scaffolding for the wrapper).
_TOKENS_PER_VERDICT = int(os.getenv("COUNCIL_TOKENS_PER_VERDICT", "160"))
_BATCH_JSON_OVERHEAD = 256


def chunk_contexts(
    contexts: Sequence[AgentInputContext], size: int | None = None,
) -> list[list[AgentInputContext]]:
    """Split contexts into batches of at most `size` for run_batch()."""
    n = max(1, int(size or DEFAULT_BATCH_SIZE))
    return [list(contexts[i:i + n]) for i in range(0, len(contexts), n)]


def build_batch_prompt(contexts: Sequence[AgentInputContext], task: str) -> str:
    """One prompt covering every context, with a per-instrument block."""
    blocks = []
    for ctx in contexts:
        sym = ctx.symbols[0] if ctx.symbols else "UNKNOWN"
        blocks.append(f"--- INSTRUMENT: {sym} ---\n{_build_market_context(ctx)}")
    return f"{task}\n\n{_BATCH_INSTRUCTION}\n\n" + "\n\n".join(blocks)


def parse_batch_response(response: Any, contexts: Sequence[AgentInputContext]) -> dict[str, dict]:
    """Map a batched model reply to {symbol: verdict}.

    Accepts the array under a few shapes because local models are inconsistent
    about wrapping: a bare list, or a dict with a list under a common key.
    Unmatched/missing symbols are simply absent from the result — the caller
    decides what to do (stub just those), rather than this silently inventing
    a verdict.
    """
    items: Any = response
    if isinstance(response, dict):
        for key in ("results", "instruments", "verdicts", "data", "items"):
            if isinstance(response.get(key), list):
                items = response[key]
                break
        else:
            items = [response] if "symbol" in response else []
    if not isinstance(items, list):
        return {}

    wanted = {(c.symbols[0] if c.symbols else "UNKNOWN") for c in contexts}
    out: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or "").strip()
        if sym in wanted and sym not in out:      # first wins; ignore duplicates
            out[sym] = item
    return out


def run_batch(
    gateway: LocalModelGateway,
    model: str,
    agent_name: str,
    task: str,
    contexts: Sequence[AgentInputContext],
) -> list[AgentVote]:
    """Judge every context in ONE model call; one AgentVote per context, in order.

    Any instrument the model failed to return a usable verdict for falls back to
    a stub vote for THAT instrument only.
    """
    if not contexts:
        return []
    prompt = build_batch_prompt(contexts, task)
    try:
        # Batched replies are {"results": [...]}, a different shape from a
        # single verdict — pass the matching schema or structured output would
        # reject every batch.
        from trading_platform.agents.model_gateway import _BATCH_SCHEMA
        # Output budget must scale with the batch: one verdict is ~120 tokens,
        # so a batch-20 reply needs ~2400 — above the default 2048, which made
        # the truncation guard fire on half of all concurrent batch-20 calls
        # (measured: 40/80 real, failure_mode "ValueError: ...truncated").
        # Floor at the gateway default so a small batch is never given less.
        budget = max(gateway.max_tokens, _TOKENS_PER_VERDICT * len(contexts) + _BATCH_JSON_OVERHEAD)
        raw = gateway.generate(model, _SYSTEM_BASE, prompt,
                               response_schema=_BATCH_SCHEMA, max_tokens=budget)
    except Exception:                                   # noqa: BLE001
        raw = {}
    by_symbol = parse_batch_response(raw, contexts)
    model_id = raw.get("model_id", model) if isinstance(raw, dict) else model

    votes: list[AgentVote] = []
    for ctx in contexts:
        sym = ctx.symbols[0] if ctx.symbols else "UNKNOWN"
        verdict = by_symbol.get(sym)
        if verdict is None:
            verdict = {
                "action": "HOLD",
                "confidence": 0.45,
                "reasoning": f"Stub: no batched verdict returned for {sym}.",
            }
        votes.append(_safe_vote(agent_name, model_id, verdict, ctx.evidence_ids))
    return votes
