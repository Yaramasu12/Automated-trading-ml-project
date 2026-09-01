from __future__ import annotations

"""AgentCouncilSupervisor — orchestrates specialist agents and produces
typed AgentCouncilDecision. Never creates OrderIntent.

Per-agent timeouts
------------------
Each strategy agent runs in a ThreadPoolExecutor.  After submitting all
futures we call concurrent.futures.wait(timeout=_PER_AGENT_TIMEOUT_S).
Futures that do not complete within the budget receive a stub HOLD vote
instead of blocking the council indefinitely.  The pool is then shut down
with cancel_futures=True so queued-but-not-started agents are discarded.

5-party high-conviction debate
-------------------------------
When the council's mean confidence >= _HIGH_CONVICTION_THRESHOLD and the
preliminary action is PROCEED, a structured 5-party debate runs:
  1. Bull  — TrendMomentumAgent argues for the trade.
  2. Bear  — MeanReversionAgent challenges under adversarial regime.
  3. Risk  — RiskCriticAgent re-evaluates with bull/bear context.
  4. PM    — PortfolioManagerAgent adjudicates allocation.
  5. Supervisor — deterministic scoring → CONFIRM | REDUCE | ABORT.
The supervisor verdict may override the main council action.

RAG grounding
-------------
Before launching agents the supervisor retrieves the top-k evidence
documents relevant to the current scan context (symbols + regime) from
the RAGRetriever attached to the LocalModelGateway.  The retrieved
doc_ids are injected into AgentInputContext.evidence_ids so every agent
can merge them with its own gateway-level retrieval.  The final
AgentCouncilDecision aggregates all returned evidence_ids.
"""

import concurrent.futures
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from trading_platform.agents.schemas import (
    AgentCouncilDecision,
    AgentInputContext,
    AgentVote,
    DebateRound,
    StrategyProposal,
)
from trading_platform.agents.specialists import (
    BATCHABLE_AGENT_CLASSES,
    DEFAULT_BATCH_SIZE,
    BreakoutAgent,
    ExecutionAnalystAgent,
    FuturesCarryAgent,
    GapEventAgent,
    HedgeBuilderAgent,
    MeanReversionAgent,
    NewsMacroAgent,
    OptionsVolatilityAgent,
    PairsStatArbAgent,
    PortfolioManagerAgent,
    QuantResearchAgent,
    RiskCriticAgent,
    TrendMomentumAgent,
    chunk_contexts,
    run_batch,
)
from trading_platform.agents.voting import aggregate_to_action, compute_consensus, has_veto

if TYPE_CHECKING:
    from trading_platform.agents.model_gateway import LocalModelGateway
    from trading_platform.trace.store import TraceStore

logger = logging.getLogger(__name__)

# Debate triggers when mean confidence clears this level and action is PROCEED.
_HIGH_CONVICTION_THRESHOLD = 0.80

# Each strategy agent has this many seconds before it receives a stub HOLD vote.
_PER_AGENT_TIMEOUT_S = 10

# consult()'s batch-collection window: how long to wait, after the first
# consult() call in a new batch, for other concurrently-scanning underlyings
# to join the same batched dispatch (see consult()'s docstring). Short enough
# that no single underlying's decision is meaningfully delayed; long enough
# that AGENT_SCAN_CONCURRENCY's normal overlap (many underlyings' specialist_
# crew nodes reaching admission within milliseconds of each other) usually
# lands in the same window.
_BATCH_WINDOW_S = float(os.getenv("COUNCIL_BATCH_WINDOW_SECONDS", "2.0"))


@dataclass
class _PendingConsult:
    ctx: AgentInputContext
    done: threading.Event = field(default_factory=threading.Event)
    decision: AgentCouncilDecision | None = None


class AgentCouncilSupervisor:
    """Runs specialist agents in parallel, aggregates votes, returns typed decision.

    Flow:
      1. RAG pre-enrichment of AgentInputContext.evidence_ids.
      2. Run 10 independent strategy agents in parallel (ThreadPoolExecutor).
         - Each agent has a hard per-agent wall-clock budget (_PER_AGENT_TIMEOUT_S).
         - Timed-out agents receive a stub HOLD vote, never block the council.
      3. Risk critic evaluates all strategy proposals.
      4. Portfolio manager builds optimal basket.
      5. Execution analyst advises on timing/slicing.
      6. Vote aggregation → consensus → preliminary action.
      7. If high-conviction PROCEED: run 5-party debate (bull/bear/risk/PM/supervisor).
         - Supervisor verdict may override the preliminary action.
      8. Return typed AgentCouncilDecision.
    """

    def __init__(
        self,
        gateway: LocalModelGateway,
        trace_store: TraceStore | None = None,
        consensus_threshold: float = 0.55,
        confidence_threshold: float = 0.45,
        max_workers: int = 10,
        per_agent_timeout_s: int = _PER_AGENT_TIMEOUT_S,
        batch_window_s: float = _BATCH_WINDOW_S,
        max_batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._gw = gateway
        self._trace_store = trace_store
        self._consensus_threshold = consensus_threshold
        self._confidence_threshold = confidence_threshold
        self._max_workers = max_workers
        self._per_agent_timeout_s = per_agent_timeout_s
        # See run()'s docstring: serializes whole consults against each other,
        # not just individual generate() calls.
        self._run_lock = threading.Lock()
        # See consult()'s docstring: collects concurrently-arriving consults
        # into one batched dispatch instead of N separate ones.
        self._batch_window_s = batch_window_s
        self._max_batch_size = max_batch_size
        self._batch_lock = threading.Lock()
        self._pending: list[_PendingConsult] = []
        self._batch_timer: threading.Timer | None = None

        self._news = NewsMacroAgent(gateway)
        self._quant = QuantResearchAgent(gateway)
        self._trend = TrendMomentumAgent(gateway)
        self._reversion = MeanReversionAgent(gateway)
        self._breakout = BreakoutAgent(gateway)
        self._gap = GapEventAgent(gateway)
        self._pairs = PairsStatArbAgent(gateway)
        self._opts_vol = OptionsVolatilityAgent(gateway)
        self._futures = FuturesCarryAgent(gateway)
        self._hedge = HedgeBuilderAgent(gateway)
        self._risk_critic = RiskCriticAgent(gateway)
        self._execution = ExecutionAnalystAgent(gateway)
        self._pm = PortfolioManagerAgent(gateway)

    # ── RAG pre-enrichment ────────────────────────────────────────────────────

    def _enrich_context_with_rag(self, ctx: AgentInputContext) -> AgentInputContext:
        """Retrieve relevant evidence and inject doc_ids into context.evidence_ids."""
        rag = self._gw.rag_retriever
        if rag is None:
            return ctx
        try:
            query = (
                f"symbols {' '.join(ctx.symbols)} regime {ctx.market_regime} "
                f"trading strategy risk"
            )
            ids = rag.retrieve_ids(query, top_k=6)
            if ids:
                merged = list(dict.fromkeys(list(ctx.evidence_ids) + ids))
                return AgentInputContext(
                    trace_id=ctx.trace_id,
                    symbols=ctx.symbols,
                    execution_mode=ctx.execution_mode,
                    features=ctx.features,
                    portfolio_state=ctx.portfolio_state,
                    market_regime=ctx.market_regime,
                    evidence_ids=merged,
                    ts=ctx.ts,
                )
        except Exception as exc:
            logger.debug("AgentCouncilSupervisor: RAG enrich error: %s", exc)
        return ctx

    # ── Main entry point ──────────────────────────────────────────────────────

    def run(self, ctx: AgentInputContext) -> AgentCouncilDecision:
        """Synchronous entrypoint for non-async callers.

        Serialized process-wide via self._run_lock. Council admission
        (master_orchestrator._council_admission) already bounds how many
        consults happen per cycle (6/300s default), but does nothing to
        bound how many run AT THE SAME INSTANT: AGENT_SCAN_CONCURRENCY lets
        many underlyings' scans overlap in wall-clock time, and each
        admitted consult fans out to 10 concurrent specialist calls on its
        own. Without this lock, several overlapping consults could fire 60+
        simultaneous generate() calls at LocalModelGateway's 4-slot
        semaphore at once -- confirmed live 2026-09-01: 87% of calls
        stubbed via concurrency_saturated, with per-agent "timed out after
        120s" log lines far exceeding what a healthy 4-slot/10-agent ratio
        should produce. Serializing consults bounds peak concurrent demand
        to one consult's fan-out (10) against the gateway's 4 slots --
        worse than ideal but a tractable ~3 waves instead of an unbounded
        pile-up. Safe to serialize: the council is advisory-only
        (CLAUDE.md) and never gates a trade on its own, so a queued consult
        delays how quickly its opinion blends into crew_confidence, not any
        hard risk gate.
        """
        with self._run_lock:
            return self._run_locked(ctx)

    def _run_locked(self, ctx: AgentInputContext) -> AgentCouncilDecision:
        ctx = self._enrich_context_with_rag(ctx)

        votes: list[AgentVote] = []

        # ── Step 1: parallel strategy agents with per-agent timeout ───────────
        strategy_tasks: list[tuple[str, Callable[[], AgentVote]]] = [
            ("TrendMomentumAgent",   lambda: self._trend.run(ctx)),
            ("MeanReversionAgent",   lambda: self._reversion.run(ctx)),
            ("BreakoutAgent",        lambda: self._breakout.run(ctx)),
            ("GapEventAgent",        lambda: self._gap.run(ctx)),
            ("PairsStatArbAgent",    lambda: self._pairs.run(ctx)),
            ("OptionsVolatilityAgent", lambda: self._opts_vol.run(ctx)),
            ("FuturesCarryAgent",    lambda: self._futures.run(ctx)),
            ("HedgeBuilderAgent",    lambda: self._hedge.run(ctx)),
            ("NewsMacroAgent",       lambda: self._news.run(ctx)),
            # Instantiated since this class's __init__ but never actually
            # called — found 2026-07-28 while wiring LM Studio, meaning
            # primary_model was configured but dead in the live council flow.
            # Joins the same pool/timeout/vote-collection path as every
            # other specialist above; no other code path needs to change.
            ("QuantResearchAgent",   lambda: self._quant.run(ctx)),
        ]

        future_to_name: dict[concurrent.futures.Future, str] = {}
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers)
        try:
            for name, fn in strategy_tasks:
                fut = pool.submit(fn)
                future_to_name[fut] = name

            done, not_done = concurrent.futures.wait(
                list(future_to_name),
                timeout=self._per_agent_timeout_s,
                return_when=concurrent.futures.ALL_COMPLETED,
            )
        finally:
            # cancel_futures drops queued-but-not-started work; running threads
            # finish on their own but we do not wait (wait=False).
            pool.shutdown(wait=False, cancel_futures=True)

        # Collect results from completed futures
        for fut in done:
            name = future_to_name[fut]
            try:
                result = fut.result()
                votes.append(result)
            except Exception as exc:
                logger.warning("AgentCouncilSupervisor: agent %s error: %s", name, exc)
                votes.append(AgentVote(
                    agent_name=name,
                    action="HOLD",
                    confidence=0.0,
                    reasoning="agent_error",
                    failure_mode=str(exc)[:100],
                ))

        # Stub votes for agents that timed out
        for fut in not_done:
            name = future_to_name[fut]
            logger.warning(
                "AgentCouncilSupervisor: agent %s timed out after %ss — stub fallback",
                name, self._per_agent_timeout_s,
            )
            votes.append(AgentVote(
                agent_name=name,
                action="HOLD",
                confidence=0.0,
                reasoning="agent_timeout",
                failure_mode=f"timeout_{self._per_agent_timeout_s}s",
            ))

        return self._assemble_decision(ctx, votes)

    def _assemble_decision(self, ctx: AgentInputContext, votes: list[AgentVote]) -> AgentCouncilDecision:
        """Steps 2-8: turn a completed vote set into a typed AgentCouncilDecision.

        Extracted from _run_locked() so _run_many_locked() (the batched path
        -- see consult()'s docstring) can reuse the exact same
        proposal/risk-critic/PM/execution-analyst/consensus/debate logic
        after collecting its votes via run_batch() instead of the parallel
        per-context ThreadPoolExecutor dispatch _run_locked() uses.
        """
        proposals: list[StrategyProposal] = []
        # ── Step 2: build strategy proposals from votes ───────────────────────
        for v in votes:
            if v.action in ("BUY", "SELL") and v.confidence >= 0.5:
                for sym in ctx.symbols[:1]:
                    proposals.append(StrategyProposal(
                        agent_name=v.agent_name,
                        symbol=sym,
                        side=v.action,
                        edge_estimate=v.confidence * 0.01,
                        risk_estimate=0.5,
                        confidence=v.confidence,
                        evidence_ids=v.evidence_ids,
                        reasoning=v.reasoning,
                        model_id=v.model_id,
                    ))

        # ── Step 3: risk critic ───────────────────────────────────────────────
        risk_critique = None
        try:
            risk_critique = self._risk_critic.run(ctx, proposals)
        except Exception as exc:
            logger.warning("AgentCouncilSupervisor: risk critic error: %s", exc)

        # Early veto on risk critic
        if risk_critique and risk_critique.veto:
            decision = AgentCouncilDecision(
                trace_id=ctx.trace_id,
                action="NO_TRADE",
                confidence=0.0,
                consensus_score=0.0,
                votes=votes,
                strategy_proposals=proposals,
                risk_critique=risk_critique,
                debate_summary="Risk critic veto — no trade.",
                model_ids_used=list({v.model_id for v in votes}),
            )
            self._write_trace(ctx.trace_id, decision)
            return decision

        # ── Step 4: portfolio manager ─────────────────────────────────────────
        portfolio_proposal = None
        try:
            portfolio_proposal = self._pm.run(ctx, proposals)
        except Exception as exc:
            logger.warning("AgentCouncilSupervisor: PM error: %s", exc)

        # ── Step 5: execution analyst ─────────────────────────────────────────
        execution_advice = None
        try:
            execution_advice = self._execution.run(ctx)
        except Exception as exc:
            logger.warning("AgentCouncilSupervisor: execution analyst error: %s", exc)

        # ── Step 6: vote aggregation ──────────────────────────────────────────
        plurality_action, confidence_mean, consensus_score = compute_consensus(votes)

        if has_veto(votes):
            final_action = "HALT"
        else:
            final_action = aggregate_to_action(
                plurality_action,
                consensus_score,
                confidence_mean,
                self._consensus_threshold,
                self._confidence_threshold,
            )

        # ── Step 7: high-conviction 5-party debate ────────────────────────────
        debate_summary = ""
        debate_round: DebateRound | None = None

        if confidence_mean >= _HIGH_CONVICTION_THRESHOLD and final_action == "PROCEED":
            debate_summary, action_override, debate_round = self._run_debate(
                ctx, proposals, votes
            )
            if action_override:
                final_action = action_override

        # ── Step 8: assemble decision ─────────────────────────────────────────
        model_ids = list({v.model_id for v in votes if v.model_id})
        evidence_ids = list(dict.fromkeys(
            ctx.evidence_ids
            + [eid for v in votes for eid in v.evidence_ids]
            + (risk_critique.evidence_ids if risk_critique else [])
            + (debate_round.bull_vote.evidence_ids if debate_round else [])
            + (debate_round.bear_vote.evidence_ids if debate_round else [])
            + (debate_round.risk_critique_updated.evidence_ids if debate_round else [])
        ))

        decision = AgentCouncilDecision(
            trace_id=ctx.trace_id,
            action=final_action,
            confidence=confidence_mean,
            consensus_score=consensus_score,
            votes=votes,
            strategy_proposals=proposals,
            risk_critique=risk_critique,
            portfolio_proposal=portfolio_proposal,
            execution_advice=execution_advice,
            debate_summary=debate_summary,
            debate_round=debate_round,
            model_ids_used=model_ids,
            evidence_ids=evidence_ids,
        )
        self._write_trace(ctx.trace_id, decision)
        return decision

    # ── Batched multi-underlying entry point ────────────────────────────────

    def consult(self, ctx: AgentInputContext) -> AgentCouncilDecision:
        """Thread-safe blocking entrypoint for master_orchestrator.py's
        specialist_crew node (one call per admitted underlying, from
        whatever worker thread that underlying's asyncio.to_thread landed on).

        Collects concurrently-arriving consults into ONE batched dispatch
        instead of firing each underlying's 9-agent LLM fan-out separately.
        This is the fix for the root problem run()'s serialization lock only
        contained, not solved: even a single, completely uncontended call to
        the "fast" model measured 62.9s live 2026-09-01 -- with only 4
        gateway concurrency slots, 9 LLM-calling agents per underlying
        cannot all finish inside a 120s budget regardless of how many
        underlyings share the queue. specialists.py's run_batch() (built and
        unit-tested, but never wired into the live path until now) judges up
        to _max_batch_size instruments in ONE call per specialist instead of
        one call per (specialist, underlying) pair -- measured 20 instruments
        in a single ~40.6s call, all 20 with real verdicts.

        A consult joins the batch currently being collected (or starts a new
        one) and blocks until that batch's specialist calls complete and its
        own slice of the result is assembled. Bounded by
        _per_agent_timeout_s plus a small margin, so a caller can never hang
        past what the old per-context run() path would have taken.
        """
        req = _PendingConsult(ctx=ctx)
        flush_now = False
        with self._batch_lock:
            self._pending.append(req)
            if len(self._pending) == 1:
                timer = threading.Timer(self._batch_window_s, self._flush_batch)
                timer.daemon = True
                self._batch_timer = timer
                timer.start()
            elif len(self._pending) >= self._max_batch_size:
                flush_now = True
        if flush_now:
            self._flush_batch()
        # Generous margin over the per-agent budget: a consult that joined
        # right before flush still needs the full batch dispatch to run.
        req.done.wait(timeout=self._per_agent_timeout_s + self._batch_window_s + 30.0)
        if req.decision is not None:
            return req.decision
        logger.warning(
            "AgentCouncilSupervisor: consult() for %s did not complete in time — stub decision",
            ctx.symbols[0] if ctx.symbols else "UNKNOWN",
        )
        return AgentCouncilDecision(
            trace_id=ctx.trace_id, action="NO_TRADE", confidence=0.0, consensus_score=0.0,
            votes=[], strategy_proposals=[], debate_summary="consult_timeout",
        )

    def _flush_batch(self) -> None:
        with self._batch_lock:
            if self._batch_timer is not None:
                self._batch_timer.cancel()
                self._batch_timer = None
            batch, self._pending = self._pending, []
        if not batch:
            return
        with self._run_lock:  # same serialization guarantee run() gives individual consults
            decisions = self._run_many_locked([r.ctx for r in batch])
        for req in batch:
            sym = req.ctx.symbols[0] if req.ctx.symbols else "UNKNOWN"
            req.decision = decisions.get(sym)
            req.done.set()

    def _run_many_locked(self, contexts: list[AgentInputContext]) -> dict[str, AgentCouncilDecision]:
        """Batched counterpart to _run_locked(): one run_batch() call per
        specialist across ALL contexts, instead of one call per (specialist,
        context). Returns {symbol: AgentCouncilDecision}, one per context.

        The 9 BATCHABLE_AGENT_CLASSES calls run CONCURRENTLY (same
        ThreadPoolExecutor + per_agent_timeout_s pattern _run_locked uses for
        its own per-agent dispatch), not sequentially -- confirmed live
        2026-09-01: a first version that looped over them one at a time left
        3 of LocalModelGateway's 4 concurrency slots idle and pushed total
        wall-clock past consult()'s own wait budget (a batch of 6 underlyings
        still hadn't finished at 152s). Concurrent dispatch lets these calls
        actually share the gateway's real capacity instead of only reducing
        call COUNT without reducing latency.

        chunk_contexts() further splits into groups of at most
        self._max_batch_size, so an unusually large flush (e.g. max_batch_size
        was hit exactly, plus a straggler) still respects the measured-safe
        batch size instead of growing unbounded.
        """
        contexts = [self._enrich_context_with_rag(c) for c in contexts]
        votes_by_symbol: dict[str, list[AgentVote]] = {
            (c.symbols[0] if c.symbols else "UNKNOWN"): [] for c in contexts
        }
        for group in chunk_contexts(contexts, self._max_batch_size):
            def _dispatch(agent_cls, group=group) -> list[AgentVote]:
                model = getattr(self._gw, agent_cls.MODEL_ATTR)
                return run_batch(self._gw, model, agent_cls.name, agent_cls.TASK, group)

            future_to_cls: dict[concurrent.futures.Future, type] = {}
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(BATCHABLE_AGENT_CLASSES))
            try:
                for agent_cls in BATCHABLE_AGENT_CLASSES:
                    future_to_cls[pool.submit(_dispatch, agent_cls)] = agent_cls
                done, not_done = concurrent.futures.wait(
                    list(future_to_cls), timeout=self._per_agent_timeout_s,
                    return_when=concurrent.futures.ALL_COMPLETED,
                )
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

            for fut in done:
                agent_cls = future_to_cls[fut]
                try:
                    votes = fut.result()
                except Exception as exc:
                    logger.warning("AgentCouncilSupervisor: batch %s error: %s", agent_cls.name, exc)
                    votes = [
                        AgentVote(agent_name=agent_cls.name, action="HOLD", confidence=0.0,
                                 reasoning="batch_error", failure_mode=str(exc)[:100])
                        for _ in group
                    ]
                for ctx, vote in zip(group, votes):
                    sym = ctx.symbols[0] if ctx.symbols else "UNKNOWN"
                    votes_by_symbol[sym].append(vote)
            for fut in not_done:
                agent_cls = future_to_cls[fut]
                logger.warning(
                    "AgentCouncilSupervisor: batch %s timed out after %ss — stub fallback",
                    agent_cls.name, self._per_agent_timeout_s,
                )
                for ctx in group:
                    sym = ctx.symbols[0] if ctx.symbols else "UNKNOWN"
                    votes_by_symbol[sym].append(AgentVote(
                        agent_name=agent_cls.name, action="HOLD", confidence=0.0,
                        reasoning="batch_timeout", failure_mode=f"timeout_{self._per_agent_timeout_s}s",
                    ))

            # OptionsVolatilityAgent is deterministic (no LLM call) — cheap
            # enough to run per-context even inside a batch.
            for ctx in group:
                sym = ctx.symbols[0] if ctx.symbols else "UNKNOWN"
                votes_by_symbol[sym].append(self._opts_vol.run(ctx))

        return {
            (ctx.symbols[0] if ctx.symbols else "UNKNOWN"):
                self._assemble_decision(ctx, votes_by_symbol[ctx.symbols[0] if ctx.symbols else "UNKNOWN"])
            for ctx in contexts
        }

    # ── 5-party debate ────────────────────────────────────────────────────────

    def _run_debate(
        self,
        ctx: AgentInputContext,
        proposals: list[StrategyProposal],
        votes: list[AgentVote],
    ) -> tuple[str, str | None, DebateRound | None]:
        """Full 5-party high-conviction debate.

        Returns (summary_str, action_override_or_None, DebateRound_or_None).

        Parties
        -------
        1. Bull  — TrendMomentumAgent argues for the proposed trades.
        2. Bear  — MeanReversionAgent challenges under adversarial regime.
        3. Risk  — RiskCriticAgent re-evaluates with both positions in context.
        4. PM    — PortfolioManagerAgent adjudicates portfolio-level allocation.
        5. Supervisor — deterministic scoring:
                        against_score >= 4 → ABORT  (action_override=NO_TRADE)
                        against_score >= 2 → REDUCE (action_override=REDUCE)
                        otherwise          → CONFIRM (no override)
        """
        try:
            # 1. Bull case ─────────────────────────────────────────────────────
            bull_vote = self._trend.run(ctx)

            # 2. Bear case ─────────────────────────────────────────────────────
            bear_ctx = AgentInputContext(
                trace_id=ctx.trace_id,
                symbols=ctx.symbols,
                execution_mode=ctx.execution_mode,
                features=ctx.features,
                portfolio_state=ctx.portfolio_state,
                market_regime="bearish_challenge",
                evidence_ids=ctx.evidence_ids,
            )
            bear_vote = self._reversion.run(bear_ctx)

            # 3. Risk critic re-evaluates with bull/bear debate in context ─────
            merged_ids = list(dict.fromkeys(
                ctx.evidence_ids + bull_vote.evidence_ids + bear_vote.evidence_ids
            ))
            debate_ctx = AgentInputContext(
                trace_id=ctx.trace_id,
                symbols=ctx.symbols,
                execution_mode=ctx.execution_mode,
                features={
                    **ctx.features,
                    "_debate": {
                        "bull_agent": bull_vote.agent_name,
                        "bull_action": bull_vote.action,
                        "bull_conf": round(bull_vote.confidence, 4),
                        "bear_agent": bear_vote.agent_name,
                        "bear_action": bear_vote.action,
                        "bear_conf": round(bear_vote.confidence, 4),
                    },
                },
                portfolio_state=ctx.portfolio_state,
                market_regime=ctx.market_regime,
                evidence_ids=merged_ids,
            )
            risk_critique_updated = self._risk_critic.run(debate_ctx, proposals)

            # 4. PM adjudicates allocation given the debate tension ─────────────
            pm_adjudication = self._pm.run(debate_ctx, proposals)

            # 5. Supervisor adjudicator — deterministic scoring ─────────────────
            against_score = 0

            # Bear beats bull on confidence by a clear margin → one strike
            if (
                bear_vote.action in ("SELL", "HOLD")
                and bear_vote.confidence > bull_vote.confidence + 0.10
            ):
                against_score += 1

            # Risk critic's recommended action
            if risk_critique_updated.recommended_action == "HALT":
                against_score += 2
            elif risk_critique_updated.recommended_action == "REDUCE":
                against_score += 1

            # Risk critic explicit veto is conclusive
            if risk_critique_updated.veto:
                against_score += 3

            # PM cannot run at target rate
            if not pm_adjudication.target_run_rate_ok:
                against_score += 1

            # High updated risk score
            if risk_critique_updated.risk_score >= 0.75:
                against_score += 1

            if against_score >= 4:
                verdict: str = "ABORT"
                action_override: str | None = "NO_TRADE"
            elif against_score >= 2:
                verdict = "REDUCE"
                action_override = "REDUCE"
            else:
                verdict = "CONFIRM"
                action_override = None

            summary = (
                f"5-Party Debate │ "
                f"Bull {bull_vote.agent_name}:{bull_vote.action}({bull_vote.confidence:.2f}) │ "
                f"Bear {bear_vote.agent_name}:{bear_vote.action}({bear_vote.confidence:.2f}) │ "
                f"Risk {risk_critique_updated.recommended_action}"
                f"(score={risk_critique_updated.risk_score:.2f}) │ "
                f"PM {'ok' if pm_adjudication.target_run_rate_ok else 'reduce'} │ "
                f"Supervisor→{verdict}(against={against_score})"
            )

            logger.info("AgentCouncilSupervisor: %s", summary)

            round_record = DebateRound(
                bull_vote=bull_vote,
                bear_vote=bear_vote,
                risk_critique_updated=risk_critique_updated,
                pm_adjudication=pm_adjudication,
                supervisor_verdict=verdict,
                supervisor_reasoning=summary,
                action_override=action_override,
            )

            return summary, action_override, round_record

        except Exception as exc:
            logger.warning("AgentCouncilSupervisor: debate error: %s", exc)
            return f"debate_error: {exc}", None, None

    # ── Trace persistence ─────────────────────────────────────────────────────

    def _write_trace(self, trace_id: str, decision: AgentCouncilDecision) -> None:
        if not self._trace_store:
            return
        try:
            trace = self._trace_store.get(trace_id)
            if trace:
                trace.agent_outputs.append(decision.to_dict())
                trace.add_event(
                    "agent_council_decision",
                    "AgentCouncilSupervisor",
                    {"action": decision.action, "confidence": decision.confidence},
                )
                self._trace_store.save(trace)
        except Exception as exc:
            logger.warning("AgentCouncilSupervisor: trace write error: %s", exc)
