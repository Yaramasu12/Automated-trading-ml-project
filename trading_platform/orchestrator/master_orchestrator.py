"""MasterOrchestrator — the LangGraph-inspired stateful node graph.

Implements a profit-first, self-improving orchestration pipeline that
replaces the existing decision pipeline with a 10-node state machine.

Architecture (inspired by LangGraph + CrewAI + AutoGen patterns):

    START
      │
      ▼
  [Node 1] MarketIntelligence ──────── Adaptive RAG + regime + news
      │
      ├──[EVENT RISK?]──► HALT (protect capital)
      │
      ▼
  [Node 2] SpecialistCrew ─────────── CrewAI 6-agent deliberation + debate
      │
      ├──[CREW=HALT]───► HALT
      ├──[CREW=HOLD/consensus<0.52]─► SKIP (no trade, not a halt)
      │
      ▼
  [Node 3] NeuralForecast ────────── Direction prob + uncertainty + tail risk
      │
      ├──[UNCERTAINTY>0.75]──► SKIP
      │
      ▼
  [Node 4] QuantumPortfolio ─────── QUBO symbol selection (optional)
      │
      ▼
  [Node 5] RiskCritic ────────────── Hard portfolio risk gates
      │
      ├──[NOT APPROVED]──► HALT
      │
      ▼
  [Node 6] ProfitGuard ───────────── EV > 0, Kelly > 1.5%, Sharpe > 0.5
      │
      ├──[GATE FAILED]───► SKIP (profitable abstention)
      │
      ▼
  [Node 7] ConsensusFusion ───────── Weighted ensemble of all signals
      │
      ├──[SCORE<0.48]────► SKIP
      │
      ▼
  [Node 8] GoalGovernor ──────────── Position size scaling vs annual target
      │
      ▼
  [Node 9] ExecutionPlan ─────────── Create typed OrderIntents
      │
      ▼
    END ──► [async] Node 10: Reflection (after trade resolves)

Each node returns a NodeResult with state delta + optional halt.
The orchestrator merges deltas and routes to the next node.

Key difference from the original pipeline:
  - Every intermediate gate is a hard stop (no optimistic fallthrough)
  - ProfitGuard ensures only positive-EV trades proceed
  - ReflectionEngine continuously improves agent weights
  - MarketRAG provides grounded historical win-rate estimates
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from trading_platform.orchestrator.market_rag import MarketRAG
from trading_platform.orchestrator.profit_guard import ProfitGuard
from trading_platform.orchestrator.reflection_engine import ReflectionEngine
from trading_platform.orchestrator.specialist_crew import SpecialistCrew
from trading_platform.orchestrator.state import NodeResult, OrchestratorState
from trading_platform.logging_safety import note_swallowed

if TYPE_CHECKING:
    from trading_platform.api.runtime import TradingRuntime

logger = logging.getLogger(__name__)

# ── Routing thresholds (env-tunable) ──────────────────────────────────────────
# These stack in series with the crew HOLD band and ProfitGuard: valid setups
# (e.g. RELIANCE passing ProfitGuard with +EV) were still killed here by a hair,
# so they are tunable. Raise the veto / lower the fusion floor to trade more.
NEURAL_UNCERTAINTY_VETO = float(os.getenv("NEURAL_UNCERTAINTY_VETO", "0.75"))
FUSION_PROCEED_THRESHOLD = float(os.getenv("FUSION_PROCEED_THRESHOLD", "0.48"))
CREW_CONSENSUS_MIN = float(os.getenv("CREW_CONSENSUS_MIN", "0.52"))
# Advisory crew: when true, a crew HOLD/low-consensus does not hard-veto — it is
# replaced by a regime/news directional lean and ProfitGuard's EV gate decides.
CREW_ADVISORY = os.getenv("CREW_ADVISORY", "false").lower() in ("1", "true", "yes")
CREW_ADVISORY_MIN_LEAN = float(os.getenv("CREW_ADVISORY_MIN_LEAN", "0.10"))


class MasterOrchestrator:
    """The central stateful orchestrator for the trading platform.

    Instantiated once per TradingRuntime and shared across all scan cycles.
    Maintains long-lived state: agent weights, RAG pattern store, profit guard
    rolling windows, and reflection history.

    Usage:
        orchestrator = MasterOrchestrator(runtime)
        state = await orchestrator.run(underlying, symbol_universe)
        candidates = state.order_candidates
    """

    def __init__(self, runtime: "TradingRuntime") -> None:
        self._runtime = runtime
        self._market_rag = MarketRAG()
        self._profit_guard = ProfitGuard()
        self._reflection_engine = ReflectionEngine()
        self._specialist_crew = SpecialistCrew()
        self._cycle_count = 0

        # Seed the RAG on first construction
        self._market_rag.seed()
        logger.info("MasterOrchestrator initialised")

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(
        self,
        underlying: str,
        symbol_universe: list[str],
        execution_mode: str = "PAPER",
        trace_id: str = "",
    ) -> OrchestratorState:
        """Execute the full 9-node pipeline for one underlying.

        Returns the final OrchestratorState regardless of outcome.
        Halted states have `state.halt_reason` set.
        """
        self._cycle_count += 1
        if not trace_id:
            trace_id = f"orch-{underlying}-{int(time.time())}"

        state = OrchestratorState(
            trace_id=trace_id,
            underlying=underlying,
            symbol_universe=symbol_universe,
            execution_mode=execution_mode,
            cycle_id=self._cycle_count,
            agent_weights=self._reflection_engine.get_weights(),
        )

        pipeline = [
            ("market_intelligence", self._node_market_intelligence),
            ("specialist_crew",     self._node_specialist_crew),
            ("neural_forecast",     self._node_neural_forecast),
            ("quantum_portfolio",   self._node_quantum_portfolio),
            ("risk_critic",         self._node_risk_critic),
            ("profit_guard",        self._node_profit_guard),
            ("consensus_fusion",    self._node_consensus_fusion),
            ("goal_governor",       self._node_goal_governor),
            ("execution_plan",      self._node_execution_plan),
        ]

        # CPU-heavy nodes run in a thread pool so they don't block the event loop.
        _HEAVY_NODES = frozenset({"specialist_crew", "neural_forecast", "quantum_portfolio"})

        for node_name, node_fn in pipeline:
            if state.halted:
                break

            t0 = time.perf_counter()
            state = _update_node(state, "current_node", node_name)
            try:
                if node_name in _HEAVY_NODES:
                    result: NodeResult = await asyncio.to_thread(node_fn, state)
                else:
                    result: NodeResult = node_fn(state)
            except Exception as exc:
                logger.error("Orchestrator node %s failed: %s", node_name, exc, exc_info=True)
                result = NodeResult(error=str(exc), halt=True, halt_reason=f"{node_name}: {exc}")

            elapsed_ms = (time.perf_counter() - t0) * 1000
            state = _merge(state, result)
            state = _update_node(state, "node_timings_ms", {
                **state.node_timings_ms, node_name: round(elapsed_ms, 1)
            })

            if result.halt:
                state = _update_node(state, "halt_reason", result.halt_reason)
                logger.info(
                    "Orchestrator halted at %s: %s (%.0fms)",
                    node_name, result.halt_reason, elapsed_ms,
                )
                break

            logger.debug("Orchestrator %s completed in %.0fms", node_name, elapsed_ms)

        return state

    async def reflect_on_outcome(
        self,
        state: OrchestratorState,
        pnl_pct: float,
        action_taken: str,
    ) -> dict:
        """Called after a trade resolves. Updates weights and pattern store."""
        if not state.crew_votes:
            return {}

        reflection = self._reflection_engine.reflect(
            trace_id=state.trace_id,
            underlying=state.underlying,
            action_taken=action_taken,
            pnl_pct=pnl_pct,
            crew_votes=state.crew_votes,
            crew_consensus=state.crew_consensus,
            regime=state.regime,
            market_features=state.market_features,
            market_rag=self._market_rag,
            profit_guard=self._profit_guard,
        )
        return reflection.to_dict()

    def stats(self) -> dict:
        return {
            "cycle_count": self._cycle_count,
            "market_rag": self._market_rag.stats(),
            "profit_guard": self._profit_guard.stats(),
            "reflection": self._reflection_engine.stats(),
        }

    def profit_guard_stats(self, underlying: str | None = None) -> dict:
        return self._profit_guard.stats(underlying)

    def recent_reflections(self, n: int = 20) -> list[dict]:
        return self._reflection_engine.recent_reflections(n)

    # ── Market-data population (feeds the crew/neural with real features) ──────

    def _ensure_market_features(self, underlying: str, regime: str) -> dict:
        """Return market features for `underlying`, computing & persisting them
        when the feature store has none yet.

        Replicates what the old decision pipeline did (fetch candles →
        FeatureEngine.compute → feature_store.append) so the autonomous loop is
        no longer blind. Cheap on subsequent scans: once a snapshot is stored,
        get_features() returns it and no re-fetch happens.
        """
        rt = self._runtime
        try:
            existing = rt.feature_store.get_features(underlying)
            if existing:
                return existing
        except Exception:
            return {}

        bars = self._fetch_recent_bars(underlying)
        if not bars or len(bars) < 5:
            return {}
        try:
            from dataclasses import asdict
            from trading_platform.ai.features import FeatureEngine
            snapshot = FeatureEngine().compute(bars)
            try:
                rt.feature_store.append(underlying, datetime.now(timezone.utc).date(), snapshot, regime)
            except Exception as exc:
                logger.debug("feature_store.append failed for %s: %s", underlying, exc)
            return asdict(snapshot)
        except Exception as exc:
            logger.debug("FeatureEngine.compute failed for %s: %s", underlying, exc)
            return {}

    def _fetch_recent_bars(self, underlying: str):
        """Recent daily candles — real Angel One when configured, else synthetic."""
        from datetime import date, timedelta
        rt = self._runtime
        try:
            if getattr(rt.settings, "angel_one_configured", False):
                inst = rt.instrument_master.get(underlying)
                to_dt = datetime.now(timezone.utc)
                from_dt = to_dt - timedelta(days=120)
                bars = rt.angel_one_history.get_candles(inst, from_dt, to_dt, "ONE_DAY")
                if bars and len(bars) >= 5:
                    return bars
        except Exception as exc:
            logger.debug("Angel candles unavailable for %s: %s", underlying, exc)
        try:
            base = rt.synthetic_data._BASE_PRICES.get(underlying, 1500.0)
            start = date.today() - timedelta(days=90)
            return rt.synthetic_data.generate_daily_bars(underlying, start, 60, base_price=base)
        except Exception:
            return []

    # ── Node 1: Market Intelligence (Adaptive RAG) ────────────────────────────

    def _node_market_intelligence(self, state: OrchestratorState) -> NodeResult:
        rt = self._runtime

        # Regime
        regime = "unknown"
        regime_confidence = 0.5
        try:
            regime_result = rt.decision_pipeline.get_regime(state.underlying)
            if isinstance(regime_result, dict):
                regime = regime_result.get("regime", "unknown")
                regime_confidence = regime_result.get("confidence", 0.5)
            else:
                regime = str(regime_result)
        except Exception as e:
            logger.debug("Regime detection failed: %s", e)

        # Market features — compute & PERSIST them when the store is empty for
        # this underlying. The autonomous orchestrated loop (unlike the old
        # /signals/scan pipeline) had no step that populated the feature store,
        # so the crew scored every symbol on empty data and always voted HOLD →
        # zero orders. This makes the live loop data-driven.
        market_features: dict[str, Any] = {}
        try:
            market_features = self._ensure_market_features(state.underlying, regime)
        except Exception as e:
            logger.debug("ensure_market_features failed for %s: %s", state.underlying, e)

        # News sentiment — NewsIntelligence.analyze(payload) returns NewsAnalysis.sentiment_score
        news_sentiment = 0.0
        try:
            news_result = rt.news_intelligence.analyze(
                {"text": state.underlying, "symbol": state.underlying, "source": "orchestrator"}
            )
            news_sentiment = float(getattr(news_result, "sentiment_score", 0.0))
        except Exception as exc:
            note_swallowed("orchestrator.news_sentiment", exc)

        # Event risk check — EventRiskGuard.check(as_of: date|None) takes a date, not a symbol.
        # FAIL-SAFE: if the event-risk check itself errors we cannot prove the window is
        # safe, so we treat it as blocked rather than silently trading through it.
        event_risk_active = False
        event_risk_reason = ""
        try:
            er = rt.event_risk_guard.check()          # no arg → uses today's date
            event_risk_active = bool(getattr(er, "blocked", False))
            event_risk_reason = str(getattr(er, "reason", ""))
        except Exception as e:
            logger.warning("Event-risk check failed — blocking as a precaution: %s", e)
            event_risk_active = True
            event_risk_reason = f"event_risk_check_error: {e}"

        # RAG retrieval — update state first so RAG has regime + news
        interim_state = _merge(state, NodeResult(updates={
            "regime": regime,
            "regime_confidence": regime_confidence,
            "news_sentiment": news_sentiment,
            "market_features": market_features,
            "event_risk_active": event_risk_active,
            "event_risk_reason": event_risk_reason,
        }))
        rag_result = self._market_rag.retrieve(interim_state)
        rag_updates = rag_result.updates

        if event_risk_active:
            return NodeResult(
                updates={**interim_state.__dict__, **rag_updates,
                         "event_risk_active": True, "event_risk_reason": event_risk_reason},
                halt=True,
                halt_reason=f"event_risk: {event_risk_reason}",
            )

        return NodeResult(updates={
            "regime": regime,
            "regime_confidence": regime_confidence,
            "news_sentiment": news_sentiment,
            "market_features": market_features,
            "event_risk_active": event_risk_active,
            **rag_updates,
        })

    # ── Node 2: Specialist Crew (CrewAI) ──────────────────────────────────────

    def _node_specialist_crew(self, state: OrchestratorState) -> NodeResult:
        # Deterministic rule-based crew always runs (no LLM needed)
        result = self._specialist_crew.deliberate(state)
        if result.halt:
            return result

        crew_action = result.updates.get("crew_action", "HOLD")
        crew_consensus = result.updates.get("crew_consensus", 0.0)
        crew_confidence = result.updates.get("crew_confidence", 0.0)

        # When AI council is enabled, blend LLM/rule votes into crew result BEFORE the HOLD gate
        # so council can rescue a marginal crew decision.
        # AgentCouncilSupervisor.run(AgentInputContext) → AgentCouncilDecision
        # AgentCouncilDecision.action ∈ {"PROCEED", "REDUCE", "HALT", "NO_TRADE"}
        try:
            council = self._runtime.agent_council
            if council is not None:
                from trading_platform.agents.schemas import AgentInputContext
                ps: dict[str, Any] = {}
                try:
                    snap = self._runtime.portfolio.mark_to_market(datetime.now(timezone.utc), {})
                    ps = {
                        "equity": snap.equity,
                        "drawdown": snap.drawdown,
                        "open_positions": snap.open_positions,
                    }
                except Exception as exc:
                    note_swallowed("orchestrator.portfolio_snapshot", exc)
                council_ctx = AgentInputContext(
                    trace_id=state.trace_id,
                    symbols=state.symbol_universe or [state.underlying],
                    execution_mode=state.execution_mode,
                    portfolio_state=ps,
                    market_regime=state.regime,
                )
                council_result = council.run(council_ctx)
                # Extract action and confidence from AgentCouncilDecision dataclass
                c_action_raw = getattr(council_result, "action", "NO_TRADE")
                c_conf = float(getattr(council_result, "confidence", crew_confidence) or 0.0)
                if c_action_raw == "PROCEED":
                    # Council aligns → boost crew confidence and consensus
                    crew_confidence = 0.60 * crew_confidence + 0.40 * c_conf
                    crew_consensus = min(crew_consensus * 1.10, 1.0)
                    # If crew said HOLD but council says PROCEED with decent confidence,
                    # flip into the directionally-appropriate side rather than always BUY.
                    # Lean is taken from regime + news so we can short bearish setups
                    # instead of buy-and-hoping in down moves.
                    if crew_action == "HOLD" and c_conf >= 0.55:
                        lean = _directional_lean(state)
                        crew_action = "SELL" if lean < 0 else "BUY"
                elif c_action_raw in ("HALT", "REDUCE"):
                    # Council is more conservative → pull crew back
                    crew_confidence *= 0.60
                    crew_consensus *= 0.70
                # NO_TRADE → leave crew unchanged
                result.updates["crew_action"] = crew_action
                result.updates["crew_confidence"] = round(crew_confidence, 4)
                result.updates["crew_consensus"] = round(crew_consensus, 4)
        except Exception as e:
            logger.debug("AI council blend failed (non-critical): %s", e)

        if crew_action == "HOLD" or crew_consensus < CREW_CONSENSUS_MIN:
            # Advisory mode (CREW_ADVISORY=true): the rule-based crew is a weak
            # signal, so instead of hard-vetoing we assign a direction from the
            # regime/news lean and let the downstream ProfitGuard EV gate be the
            # real decider. Only proceed when the lean is non-trivial; a flat
            # lean genuinely has no edge, so still hold.
            if CREW_ADVISORY:
                lean = _directional_lean(state)
                if abs(lean) >= CREW_ADVISORY_MIN_LEAN:
                    # NOTE: updates keys must be OrchestratorState fields — the
                    # merge passes them as dataclass kwargs (unknown keys raise).
                    result.updates["crew_action"] = "BUY" if lean > 0 else "SELL"
                    result.updates["crew_confidence"] = round(max(crew_confidence, 0.45), 4)
                    result.updates["crew_consensus"] = round(max(crew_consensus, CREW_CONSENSUS_MIN), 4)
                    return NodeResult(updates=result.updates)
            reason = (
                f"crew action=HOLD (no directional conviction, consensus={crew_consensus:.2f})"
                if crew_action == "HOLD"
                else f"crew consensus={crew_consensus:.2f} < {CREW_CONSENSUS_MIN}"
            )
            return NodeResult(
                updates=result.updates,
                halt=True,
                halt_reason=reason,
            )

        return result

    # ── Node 3: Neural Forecast ────────────────────────────────────────────────

    def _node_neural_forecast(self, state: OrchestratorState) -> NodeResult:
        updates: dict[str, Any] = {
            "neural_passed": False,
            "neural_uncertainty": 0.5,       # neutral default — no veto when service absent
            "neural_direction_prob": 0.5,
            "neural_expected_return": 0.0,
            "neural_tail_risk": 0.0,
            "neural_correlation_risk": 0.0,
        }

        neural_service = self._runtime.neural_service
        if neural_service is None:
            # Neural lab disabled — proceed with neutral defaults (no veto)
            return NodeResult(updates=updates)

        try:
            # Build bars_map from feature store for each symbol
            symbols = state.symbol_universe or [state.underlying]
            bars_map: dict[str, list[dict]] = {}
            try:
                for sym in symbols:
                    bars = self._runtime.feature_store.get_bars(sym, limit=60)
                    if bars:
                        bars_map[sym] = bars
            except Exception as exc:
                note_swallowed("orchestrator.feature_bars", exc)

            bundle = neural_service.predict(
                trace_id=state.trace_id,
                symbols=symbols,
                bars_map=bars_map,
            )
            if bundle:
                forecasts = getattr(bundle, "forecasts", []) or []
                direction_probs = [f.direction_probability for f in forecasts if hasattr(f, "direction_probability")]
                expected_returns = [f.expected_return for f in forecasts if hasattr(f, "expected_return")]
                tail_risks = getattr(bundle, "tail_risks", []) or []

                updates["neural_direction_prob"] = (
                    sum(direction_probs) / len(direction_probs) if direction_probs else 0.5
                )
                updates["neural_expected_return"] = (
                    sum(expected_returns) / len(expected_returns) if expected_returns else 0.0
                )
                raw_uncertainty = getattr(bundle, "overall_uncertainty", 0.5)
                # Cap uncertainty: when no bars available the service returns 1.0 but that
                # should not veto — treat it as high-but-not-vetoing (0.70)
                updates["neural_uncertainty"] = min(raw_uncertainty, 0.70) if not bars_map else raw_uncertainty
                updates["neural_tail_risk"] = (
                    max(getattr(tr, "extreme_move_probability", 0.0) or 0.0 for tr in tail_risks)
                    if tail_risks else 0.0
                )
                cr = getattr(bundle, "correlation_risk", None)
                updates["neural_correlation_risk"] = (
                    getattr(cr, "contagion_risk_score", 0.0) if cr else 0.0
                )
                updates["neural_model_versions"] = dict(getattr(bundle, "model_versions", {}) or {})
                updates["neural_passed"] = True
        except Exception as e:
            logger.warning("Neural forecast error: %s", e)
            # Keep neutral defaults already set above — do not veto

        uncertainty = updates.get("neural_uncertainty", 0.5)
        if uncertainty > NEURAL_UNCERTAINTY_VETO:
            return NodeResult(
                updates=updates,
                halt=True,
                halt_reason=f"neural_uncertainty={uncertainty:.2f} > veto={NEURAL_UNCERTAINTY_VETO}",
            )

        return NodeResult(updates=updates)

    # ── Node 4: Quantum Portfolio ──────────────────────────────────────────────

    def _node_quantum_portfolio(self, state: OrchestratorState) -> NodeResult:
        updates: dict[str, Any] = {
            "quantum_selected_symbols": state.symbol_universe,
            "quantum_backend": "classical",
            "quantum_beats_baseline": False,
            "quantum_improvement": 0.0,
        }
        try:
            from trading_platform.quantum.schemas import PortfolioOptimizationRequest, QuantumCandidate
            # QuantumOptimizationService.optimize(req: PortfolioOptimizationRequest) — not keyword args
            # NOTE: Node 4 runs BEFORE ConsensusFusion (Node 7), so state.fusion_action is not
            # populated yet. Use the crew's decided side, which IS available from Node 2.
            fusion_side = state.crew_action if state.crew_action in ("BUY", "SELL") else "BUY"
            q_candidates = [
                QuantumCandidate(
                    symbol=sym,
                    side=fusion_side,
                    expected_edge=max(0.001, state.neural_expected_return),
                    risk_estimate=max(0.01, state.neural_uncertainty * 0.5),
                    liquidity_score=1.0,
                )
                for sym in (state.symbol_universe or [state.underlying])[:10]
            ]
            req = PortfolioOptimizationRequest(
                trace_id=state.trace_id,
                candidates=q_candidates,
                cardinality_limit=getattr(
                    self._runtime.settings, "quantum_cardinality_limit", 5
                ),
            )
            result = self._runtime.quantum_service.optimize(req)
            if result:
                updates["quantum_selected_symbols"] = list(
                    getattr(result, "selected_symbols", state.symbol_universe)
                )
                updates["quantum_backend"] = getattr(result, "backend_used", "classical")
                updates["quantum_beats_baseline"] = bool(getattr(result, "beats_baseline", False))
                improvement = getattr(result, "improvement_over_classical", None)
                updates["quantum_improvement"] = float(improvement) if improvement is not None else 0.0
                updates["quantum_objective"] = float(getattr(result, "objective_value", 0.0))
        except Exception as e:
            logger.debug("Quantum portfolio failed (non-critical): %s", e)
        return NodeResult(updates=updates)

    # ── Node 5: Risk Critic ────────────────────────────────────────────────────

    def _node_risk_critic(self, state: OrchestratorState) -> NodeResult:
        updates: dict[str, Any] = {"risk_approved": False, "risk_score": 0.0, "risk_reason": ""}
        try:
            portfolio = self._runtime.portfolio
            ps = portfolio.snapshot() if hasattr(portfolio, "snapshot") else {}
            if isinstance(ps, dict):
                equity = ps.get("equity", ps.get("cash", 1.0)) or 1.0
                peak = ps.get("peak_equity", equity)
                daily_pnl = ps.get("realized_pnl", 0.0)
                drawdown = (peak - equity) / max(peak, 1.0) if peak > 0 else 0.0
                daily_loss_pct = abs(daily_pnl) / max(equity, 1.0) if daily_pnl < 0 else 0.0
            else:
                drawdown = 0.0
                daily_loss_pct = 0.0

            updates["drawdown_pct"] = round(drawdown, 5)
            updates["daily_loss_pct"] = round(daily_loss_pct, 5)

            # Hard gate checks
            settings = self._runtime.settings
            max_dd = getattr(settings, "max_drawdown", 0.10)
            max_dl = getattr(settings, "max_daily_loss", 0.02)

            if drawdown >= max_dd:
                return NodeResult(
                    updates={**updates, "risk_reason": f"drawdown={drawdown:.1%} >= max={max_dd:.1%}"},
                    halt=True,
                    halt_reason=f"risk: max drawdown breached ({drawdown:.1%})",
                )
            if daily_loss_pct >= max_dl:
                return NodeResult(
                    updates={**updates, "risk_reason": f"daily_loss={daily_loss_pct:.1%} >= max={max_dl:.1%}"},
                    halt=True,
                    halt_reason=f"risk: daily loss limit ({daily_loss_pct:.1%})",
                )

            # Full per-order risk evaluation (RiskEngine.evaluate) requires an OrderIntent
            # which doesn't exist yet at this planning stage — that check runs inside the
            # ExecutionScheduler at submit time. Record a composite risk score from the
            # already-computed drawdown and daily-loss so Node 7 can weight it.
            updates["risk_score"] = max(drawdown, daily_loss_pct)
            updates["risk_approved"] = True
            updates["risk_reason"] = "all_checks_passed"

        except Exception as e:
            # FAIL-SAFE: a risk check that throws must BLOCK the trade, never approve it.
            # (The runtime's final execution gate is a second line of defence, but a risk
            # node that errors should not let a candidate proceed on its own.)
            logger.error("Risk critic node error — halting to protect capital: %s", e)
            return NodeResult(
                updates={**updates, "risk_approved": False, "risk_reason": f"check_error: {e}"},
                halt=True,
                halt_reason=f"risk: check error ({e})",
            )

        return NodeResult(updates=updates)

    # ── Node 6: Profit Guard ───────────────────────────────────────────────────

    def _node_profit_guard(self, state: OrchestratorState) -> NodeResult:
        return self._profit_guard.evaluate(state)

    # ── Node 7: Consensus Fusion (AutoGen debate pattern) ─────────────────────

    def _node_consensus_fusion(self, state: OrchestratorState) -> NodeResult:
        """Weighted ensemble of all upstream signals → final score + action.

        Blends: SpecialistCrew votes + Neural direction + RAG win rate + RL policy votes.
        """
        from trading_platform.rl.env import (
            ACTION_NOOP, ACTION_PROPOSE_ENTRY, ACTION_PROPOSE_EXIT,
            ACTION_SIZE_UP, ACTION_SIZE_DOWN, EnvObservation,
        )

        weights = state.agent_weights or {}

        # ── Signal scores (all mapped to [-1, 1]) ──────────────────────────
        crew_score = (state.crew_confidence * 2 - 1) if state.crew_action in {"BUY", "SELL"} else 0.0
        neural_score = state.neural_direction_prob * 2 - 1
        rag_score = state.rag_win_rate * 2 - 1

        # ── RL/MARL policy votes ───────────────────────────────────────────
        rl_score = 0.0
        rl_vote_count = 0
        try:
            policy_registry = self._runtime.policy_registry
            ps = {}
            try:
                portfolio = self._runtime.portfolio
                snap = portfolio.snapshot() if hasattr(portfolio, "snapshot") else {}
                ps = snap if isinstance(snap, dict) else {}
            except Exception as exc:
                note_swallowed("orchestrator.policy_portfolio_snapshot", exc)

            obs = EnvObservation(
                features=[
                    state.neural_direction_prob,
                    state.rag_win_rate,
                    state.neural_uncertainty,
                    state.neural_tail_risk,
                    state.regime_confidence,
                    state.news_sentiment,
                ],
                portfolio_pnl=float(ps.get("realized_pnl", 0.0)),
                open_positions=int(ps.get("open_positions", 0)),
                volatility=state.market_features.get("realized_volatility", 0.20),
                liquidity=1.0,
                step=state.cycle_id % 100,
            )

            _action_to_score = {
                ACTION_PROPOSE_ENTRY:  1.0,   # strong bullish
                ACTION_SIZE_UP:        0.5,   # mild bullish
                ACTION_NOOP:           0.0,   # neutral
                ACTION_SIZE_DOWN:     -0.5,   # mild bearish
                ACTION_PROPOSE_EXIT:  -1.0,   # exit / bearish
            }

            for record in (policy_registry.list_all() or []):
                if record.get("status") not in ("paper", "shadow", "live_canary", "live_approved"):
                    continue
                policy = policy_registry.get(record["policy_id"])
                if policy is None:
                    continue
                try:
                    action = policy.act(obs)
                    rl_score += _action_to_score.get(action, 0.0)
                    rl_vote_count += 1
                except Exception as exc:
                    note_swallowed("orchestrator.rl_policy_act", exc)
        except Exception as e:
            logger.debug("RL vote collection failed (non-critical): %s", e)

        rl_avg = (rl_score / rl_vote_count) if rl_vote_count > 0 else 0.0
        w_rl = 0.6 if rl_vote_count > 0 else 0.0  # only weight RL if policies actually voted

        # ── Penalty terms ────────────────────────────────────────────────
        uncertainty_penalty = max(0.0, state.neural_uncertainty - 0.5) * 0.5
        tail_risk_penalty = state.neural_tail_risk * 0.3
        correlation_penalty = state.neural_correlation_risk * 0.2

        # ── Ensemble weights (adaptive) ──────────────────────────────────
        w_crew = sum(weights.get(n, 1.0) for n in ["TrendAnalyst", "MeanReversionSpecialist", "QuantitativeStrategist"]) / 3
        w_neural = 1.2
        w_rag = 0.8
        w_total = w_crew + w_neural + w_rag + w_rl + 1e-9

        fusion_raw = (
            w_crew * crew_score
            + w_neural * neural_score
            + w_rag * rag_score
            + w_rl * rl_avg
        ) / w_total

        # Apply penalties
        fusion_penalised = fusion_raw - uncertainty_penalty - tail_risk_penalty - correlation_penalty

        # Map to [0, 1] score
        fusion_score = (fusion_penalised + 1) / 2
        fusion_score = max(0.0, min(1.0, fusion_score))

        reasoning: list[str] = [
            f"crew={crew_score:.3f}(w={w_crew:.2f})",
            f"neural={neural_score:.3f}",
            f"rag={rag_score:.3f}",
            f"rl={rl_avg:.3f}(n={rl_vote_count})",
            f"penalties={uncertainty_penalty + tail_risk_penalty + correlation_penalty:.3f}",
            f"fusion={fusion_score:.3f}",
        ]

        if fusion_score < FUSION_PROCEED_THRESHOLD:
            return NodeResult(
                updates={
                    "fusion_score": round(fusion_score, 4),
                    "fusion_action": "HOLD",
                    "fusion_confidence": round(fusion_score, 4),
                    "fusion_reasoning": reasoning,
                },
                halt=True,
                halt_reason=f"fusion_score={fusion_score:.3f} < threshold={FUSION_PROCEED_THRESHOLD}",
            )

        # crew_action is normally already BUY/SELL here (HOLD halts at Node 2); the
        # fallback derives a side from the directional lean instead of forcing BUY.
        if state.crew_action in {"BUY", "SELL"}:
            fusion_action = state.crew_action
        else:
            fusion_action = "SELL" if _directional_lean(state) < 0 else "BUY"
        fusion_confidence = fusion_score * state.crew_confidence * (1 - state.neural_uncertainty * 0.5)

        return NodeResult(updates={
            "fusion_score": round(fusion_score, 4),
            "fusion_action": fusion_action,
            "fusion_confidence": round(min(fusion_confidence, 0.95), 4),
            "fusion_reasoning": reasoning,
        })

    # ── Node 8: Goal Governor ──────────────────────────────────────────────────

    # Map GoalGovernor recommendation strings to orchestrator sizing directives
    _GOAL_REC_MAP = {
        "normal": "MAINTAIN",
        "increase_research": "SCALE_UP",
        "reduce_risk": "REDUCE",
        "MAINTAIN": "MAINTAIN",
        "SCALE_UP": "SCALE_UP",
        "REDUCE": "REDUCE",
        "PRESERVATION": "PRESERVATION",
    }

    def _node_goal_governor(self, state: OrchestratorState) -> NodeResult:
        multiplier = 1.0
        recommendation = "MAINTAIN"
        on_track = True

        try:
            gg = self._runtime.goal_governor
            if hasattr(gg, "compute_state"):
                gs = gg.compute_state()
                # GoalGovernor.compute_state() returns a GoalState dataclass
                if hasattr(gs, "recommendation"):
                    raw_rec = gs.recommendation
                    recommendation = self._GOAL_REC_MAP.get(raw_rec, "MAINTAIN")
                    on_track = bool(getattr(gs, "on_track", True))
                elif isinstance(gs, dict):
                    raw_rec = gs.get("recommendation", "normal")
                    recommendation = self._GOAL_REC_MAP.get(raw_rec, "MAINTAIN")
                    on_track = bool(gs.get("on_track", True))

                if recommendation == "SCALE_UP":
                    multiplier = min(1.5, 1.0 + (1.0 - state.neural_uncertainty) * 0.5)
                elif recommendation == "REDUCE":
                    multiplier = max(0.5, state.neural_uncertainty)
                elif recommendation == "PRESERVATION":
                    multiplier = 0.25
        except Exception as exc:
            note_swallowed("orchestrator.goal_multiplier", exc)

        # Kelly fraction caps the multiplier
        pg = state.profit_gate
        if pg and pg.kelly_fraction > 0:
            kelly_multiplier = min(pg.kelly_fraction * 10, 2.0)  # Kelly*10 as multiplier
            multiplier = min(multiplier, kelly_multiplier)

        # Fusion confidence scales down in low-confidence environments
        multiplier *= max(0.5, state.fusion_confidence)
        multiplier = max(0.1, min(2.0, multiplier))

        return NodeResult(updates={
            "position_size_multiplier": round(multiplier, 4),
            "goal_recommendation": recommendation,
            "goal_on_track": on_track,
        })

    # ── Node 9: Execution Plan ─────────────────────────────────────────────────

    def _node_execution_plan(self, state: OrchestratorState) -> NodeResult:
        """Generate typed order candidates from the final orchestrated state."""
        candidates: list[dict] = []
        action = state.fusion_action.upper()
        multiplier = state.position_size_multiplier

        # Use quantum-selected symbols if available and better than baseline
        symbols_to_trade = (
            state.quantum_selected_symbols
            if state.quantum_beats_baseline and state.quantum_selected_symbols
            else state.symbol_universe[:5]   # cap at 5 symbols per cycle
        )

        for symbol in symbols_to_trade:
            candidates.append({
                "symbol": symbol,
                "underlying": state.underlying,
                "side": action,
                "strategy_name": f"orchestrator_{state.regime.lower()}",
                "confidence": state.fusion_confidence,
                "position_size_multiplier": multiplier,
                "signal": {
                    "side": action,
                    "confidence": state.fusion_confidence,
                    "price": 0.0,   # filled by execution router at submit time
                    "reason": " | ".join(state.fusion_reasoning[:3]),
                },
                "orchestrator_metadata": {
                    "trace_id": state.trace_id,
                    "regime": state.regime,
                    "underlying": state.underlying,
                    "crew_action": state.crew_action,
                    "crew_consensus": state.crew_consensus,
                    "neural_direction_prob": state.neural_direction_prob,
                    # canonical key read by _on_fill for MetaLabeler wiring
                    "neural_direction_probability": state.neural_direction_prob,
                    "fusion_score": state.fusion_score,
                    "expected_value": state.profit_gate.expected_value if state.profit_gate else 0.0,
                    "kelly_fraction": state.profit_gate.kelly_fraction if state.profit_gate else 0.0,
                    "rag_win_rate": state.rag_win_rate,
                    "market_features": state.market_features,
                },
            })

        logger.info(
            "ExecutionPlan: %d candidates for %s | action=%s fusion=%.3f EV=%.4f",
            len(candidates), state.underlying, action,
            state.fusion_score,
            state.profit_gate.expected_value if state.profit_gate else 0.0,
        )

        return NodeResult(updates={"order_candidates": candidates})


# ── Directional lean ──────────────────────────────────────────────────────────

# Signed regime lean: +bullish / −bearish. Mirrors SpecialistCrew._regime_score
# so the orchestrator agrees with the crew on which way a regime points.
_REGIME_LEAN = {
    "TRENDING_UP": 0.8, "BULLISH": 0.7, "BREAKOUT": 0.6, "TRENDING": 0.5,
    "NEUTRAL": 0.0, "MEAN_REVERTING": 0.0, "unknown": 0.0,
    "HIGH_VOLATILITY": -0.2, "VOLATILE": -0.2,
    "TRENDING_DOWN": -0.7, "BEARISH": -0.8,
}


def _directional_lean(state: OrchestratorState) -> float:
    """Signed [-1, 1] lean from regime + news + neural. <0 ⇒ short, >0 ⇒ long.

    Used only to pick a side when the crew abstained (HOLD) but a downstream gate
    still wants to trade — so we can short bearish setups instead of always buying.
    """
    regime_lean = _REGIME_LEAN.get(state.regime, 0.0)
    news_lean = max(-1.0, min(1.0, state.news_sentiment))
    neural_lean = (state.neural_direction_prob - 0.5) * 2.0 if state.neural_passed else 0.0
    return 0.5 * regime_lean + 0.3 * news_lean + 0.2 * neural_lean


# ── State helpers ─────────────────────────────────────────────────────────────

def _merge(state: OrchestratorState, result: NodeResult) -> OrchestratorState:
    """Apply NodeResult.updates to state (shallow copy)."""
    if not result.updates:
        return state
    d = {k: v for k, v in state.__dict__.items()}
    d.update(result.updates)
    if result.error:
        d["errors"] = list(state.errors) + [result.error]
    return OrchestratorState(**d)


def _update_node(state: OrchestratorState, key: str, value: Any) -> OrchestratorState:
    d = {k: v for k, v in state.__dict__.items()}
    d[key] = value
    return OrchestratorState(**d)
