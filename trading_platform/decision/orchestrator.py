from __future__ import annotations

"""Canonical decision-cycle orchestration.

This module is the single runtime path for a scan cycle:

baseline DecisionPipeline scan
  -> trace
  -> optional advisory systems
  -> blackboard
  -> ensemble

It deliberately does not submit orders. Execution remains behind the runtime's
explicit enqueue path and final risk controls.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from trading_platform.agent.trading_agent import SCAN_UNDERLYINGS
from trading_platform.agents.schemas import AgentInputContext
from trading_platform.decision_fusion.schemas import DecisionBlackboard
from trading_platform.domain.enums import ExecutionMode
from trading_platform.quantum.schemas import PortfolioOptimizationRequest, QuantumCandidate
from trading_platform.streaming.topics import (
    publish_agent_vote,
    publish_model_prediction,
    publish_quantum_result,
    publish_risk_veto,
)
from trading_platform.trace.ids import new_trace_id
from trading_platform.trace.models import DecisionTrace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecisionCycleResult:
    """Result bundle for one canonical decision cycle."""

    trace_id: str
    baseline_payload: dict
    high_end_payload: dict
    blackboard: DecisionBlackboard | None = None


class DecisionCycleOrchestrator:
    """Coordinates baseline and advisory decision layers.

    The runtime is injected as an object to avoid duplicating the many existing
    component dependencies while the project migrates onto this single path.
    """

    def __init__(self, runtime: Any) -> None:
        self._rt = runtime

    def run_signal_scan(self, payload: dict | None = None) -> DecisionCycleResult:
        """Run the canonical cycle for the existing compact signal endpoint."""
        return self._run(payload or {}, default_symbols=SCAN_UNDERLYINGS, trace_prefix="scan")

    def run_high_end_scan(self, payload: dict | None = None) -> DecisionCycleResult:
        """Run the canonical cycle for the high-end debug/advisory endpoint."""
        return self._run(payload or {}, default_symbols=SCAN_UNDERLYINGS[:5], trace_prefix="hescan")

    def _run(
        self,
        payload: dict,
        *,
        default_symbols: list[str],
        trace_prefix: str,
    ) -> DecisionCycleResult:
        symbols = self._symbols_from_payload(payload, default_symbols)
        trace_id = str(payload.get("trace_id") or new_trace_id(trace_prefix))
        trace = DecisionTrace(
            trace_id=trace_id,
            created_at=datetime.now(timezone.utc),
            execution_mode=self._rt.execution_mode.value,
            symbol_universe=symbols,
        )
        trace.add_event(
            "decision_cycle_started",
            "DecisionCycleOrchestrator",
            {"symbols": symbols, "trace_prefix": trace_prefix},
        )
        self._rt.trace_store.save(trace)

        baseline_payload = self._run_baseline(payload, symbols, trace_id)
        trace = self._rt.trace_store.get(trace_id) or trace
        self._stamp_baseline_trace(trace, baseline_payload, trace_id)
        trace.metadata["baseline_summary"] = {
            "approved_candidates": baseline_payload["approved_candidates"],
            "rejected_candidates": baseline_payload["rejected_candidates"],
            "submitted_orders": baseline_payload["submitted_orders"],
        }
        trace.add_event(
            "baseline_scan_completed",
            "DecisionPipeline",
            trace.metadata["baseline_summary"],
        )
        self._rt.trace_store.save(trace)

        high_end_payload, blackboard = self._run_advisory_layers(
            payload=payload,
            symbols=symbols,
            trace_id=trace_id,
            baseline_payload=baseline_payload,
        )
        return DecisionCycleResult(
            trace_id=trace_id,
            baseline_payload=baseline_payload,
            high_end_payload=high_end_payload,
            blackboard=blackboard,
        )

    def _symbols_from_payload(self, payload: dict, default_symbols: list[str]) -> list[str]:
        raw = payload.get("symbols", payload.get("underlyings", default_symbols))
        if isinstance(raw, str):
            raw = [item.strip() for item in raw.split(",")]
        return [str(item).upper() for item in raw if str(item).strip()]

    def _start_from_payload(self, payload: dict) -> date:
        default_start = (date.today() - timedelta(days=60)).isoformat()
        return date.fromisoformat(str(payload.get("start", default_start)))

    def _scan_mode_from_payload(self, payload: dict) -> ExecutionMode:
        mode_override = payload.get("execution_mode")
        try:
            return ExecutionMode(mode_override) if mode_override else self._rt.execution_mode
        except ValueError:
            return self._rt.execution_mode

    def _run_baseline(self, payload: dict, symbols: list[str], trace_id: str) -> dict:
        start = self._start_from_payload(payload)
        days = int(payload.get("days", 60))
        strategy_names = (
            [str(item) for item in payload["strategy_names"]]
            if payload.get("strategy_names")
            else None
        )
        scan_mode = self._scan_mode_from_payload(payload)

        scans = [
            self._stamp_scan_payload(
                self._rt.decision_pipeline.scan(
                    underlying=symbol,
                    start=start,
                    days=days,
                    execution_mode=scan_mode,
                    live_armed=self._rt.live_armed,
                    kill_switch_active=self._rt.kill_switch_active,
                    strategy_names=strategy_names,
                ).to_dict(),
                trace_id,
            )
            for symbol in symbols
        ]

        approved = 0
        rejected = 0
        now_ts = datetime.now(timezone.utc).isoformat()
        for scan in scans:
            for candidate in scan["candidates"]:
                rd = candidate.get("risk_decision")
                if not rd:
                    continue
                if rd["approved"]:
                    approved += 1
                else:
                    rejected += 1
                    rejected_symbol = (candidate.get("instrument") or {}).get("symbol", "")
                    rejected_reason = rd.get("reason", "")
                    self._rt._risk_rejection_log.append({
                        "ts": now_ts,
                        "underlying": scan.get("underlying", ""),
                        "symbol": rejected_symbol,
                        "strategy": candidate.get("strategy_name", ""),
                        "reason": rejected_reason,
                        "risk_score": rd.get("risk_score", 0),
                        "regime": scan.get("regime", ""),
                    })
                    try:
                        publish_risk_veto(
                            self._rt.typed_bus,
                            symbol=rejected_symbol,
                            reason=rejected_reason,
                            component="risk_engine",
                        )
                    except Exception:
                        pass

        if len(self._rt._risk_rejection_log) > 500:
            self._rt._risk_rejection_log = self._rt._risk_rejection_log[-500:]

        return {
            "mode": self._rt.execution_mode.value,
            "submitted_orders": 0,
            "approved_candidates": approved,
            "rejected_candidates": rejected,
            "scans": scans,
        }

    @staticmethod
    def _stamp_scan_payload(scan: dict, trace_id: str) -> dict:
        for index, candidate in enumerate(scan.get("candidates", [])):
            candidate_id = f"{trace_id}:{scan.get('underlying', 'UNKNOWN')}:{index}"
            candidate["candidate_id"] = candidate_id
            signal = candidate.get("signal")
            if signal is not None:
                metadata = dict(signal.get("metadata") or {})
                metadata.setdefault("trace_id", trace_id)
                metadata.setdefault("candidate_id", candidate_id)
                metadata.setdefault("opens_position", True)
                signal["metadata"] = metadata
        return scan

    @staticmethod
    def _stamp_baseline_trace(trace: DecisionTrace, baseline_payload: dict, trace_id: str) -> None:
        candidate_count = 0
        for scan in baseline_payload.get("scans", []):
            for candidate in scan.get("candidates", []):
                candidate_count += 1
                rd = candidate.get("risk_decision") or {}
                instrument = candidate.get("instrument") or {}
                event_data = {
                    "candidate_id": candidate.get("candidate_id"),
                    "underlying": scan.get("underlying", ""),
                    "symbol": instrument.get("symbol", ""),
                    "strategy_name": candidate.get("strategy_name", ""),
                    "reason": candidate.get("reason", ""),
                    "approved": rd.get("approved") if rd else None,
                    "risk_reason": rd.get("reason") if rd else "",
                    "risk_score": rd.get("risk_score") if rd else None,
                }
                trace.add_event("candidate_generated", "DecisionPipeline", event_data)
                if rd:
                    trace.risk_decisions.append({
                        "component": "DecisionPipeline",
                        "trace_id": trace_id,
                        **event_data,
                    })
        trace.metadata["baseline_candidate_count"] = candidate_count

    def _run_advisory_layers(
        self,
        *,
        payload: dict,
        symbols: list[str],
        trace_id: str,
        baseline_payload: dict,
    ) -> tuple[dict, DecisionBlackboard | None]:
        result: dict = {
            "trace_id": trace_id,
            "execution_mode": self._rt.execution_mode.value,
            "symbols": list(symbols),
            "baseline": baseline_payload,
            "ai_council": None,
            "neural": None,
            "quantum": None,
            "marl": None,
            "ensemble": None,
            "goal_governor": None,
        }

        snapshot = self._rt.portfolio.mark_to_market(datetime.now(timezone.utc), {})
        portfolio_state = {
            "equity": snapshot.equity,
            "drawdown": snapshot.drawdown,
            "open_positions": snapshot.open_positions,
        }
        regime = self._regime_for(symbols)

        kernel_result = None
        if self._rt.settings.enable_quantum_lab:
            try:
                kernel_features = [
                    float(portfolio_state.get("drawdown", 0.0)),
                    float(portfolio_state.get("equity", self._rt.settings.initial_capital))
                    / max(float(self._rt.settings.initial_capital), 1.0),
                    float(portfolio_state.get("open_positions", 0)) / 10.0,
                ]
                kernel_result = self._rt._quantum_kernel_service.classify_regime(
                    trace_id=trace_id,
                    features=kernel_features,
                    classical_regime=regime,
                )
                result["quantum_kernel"] = kernel_result.to_dict()
            except Exception as exc:
                logger.warning("decision cycle: quantum kernel error: %s", exc)

        council_decision = None
        if self._rt.settings.enable_ai_council:
            try:
                ctx = AgentInputContext(
                    trace_id=trace_id,
                    symbols=list(symbols),
                    execution_mode=self._rt.execution_mode.value,
                    portfolio_state=portfolio_state,
                    market_regime=regime,
                )
                council_decision = self._rt._agent_council.run(ctx)
                result["ai_council"] = council_decision.to_dict()
                try:
                    publish_agent_vote(self._rt.typed_bus, council_decision.to_dict())
                except Exception:
                    pass
            except Exception as exc:
                logger.warning("decision cycle: agent council error: %s", exc)

        neural_bundle = None
        if self._rt.settings.enable_neural_lab:
            try:
                bars_map = self._bars_map_for_neural(symbols)
                neural_bundle = self._rt._neural_service.predict(trace_id, list(symbols), bars_map)
                result["neural"] = neural_bundle.to_dict()
                self._rt._latest_neural_probs = {
                    forecast.symbol: forecast.direction_probability
                    for forecast in neural_bundle.forecasts
                }
                result["neural_direction_probabilities"] = dict(self._rt._latest_neural_probs)
                try:
                    publish_model_prediction(self._rt.typed_bus, neural_bundle.to_dict())
                except Exception:
                    pass
            except Exception as exc:
                logger.warning("decision cycle: neural service error: %s", exc)

        quantum_result = None
        if self._rt.settings.enable_quantum_lab and council_decision:
            try:
                candidates = [
                    QuantumCandidate(
                        symbol=proposal.symbol,
                        side=proposal.side,
                        expected_edge=max(0.0, proposal.edge_estimate),
                        risk_estimate=max(0.01, proposal.risk_estimate),
                        liquidity_score=1.0,
                    )
                    for proposal in council_decision.strategy_proposals[
                        : self._rt.settings.quantum_max_candidates
                    ]
                ]
                if candidates:
                    req = PortfolioOptimizationRequest(
                        trace_id=trace_id,
                        candidates=candidates,
                        risk_aversion=self._rt.settings.quantum_risk_aversion,
                        cardinality_limit=self._rt.settings.quantum_cardinality_limit,
                    )
                    quantum_result = self._rt._quantum_service.optimize(req)
                    result["quantum"] = quantum_result.to_dict()
                    try:
                        publish_quantum_result(self._rt.typed_bus, quantum_result.to_dict())
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning("decision cycle: quantum service error: %s", exc)

        rl_advisory = self._run_marl_advisory(portfolio_state) if self._rt.settings.enable_marl_lab else {}
        if rl_advisory:
            result["marl"] = rl_advisory

        blackboard = DecisionBlackboard(
            trace_id=trace_id,
            execution_mode=self._rt.execution_mode.value,
            symbols=list(symbols),
            pipeline_candidates=self._pipeline_candidates(baseline_payload),
            agent_council_decision=council_decision,
            neural_bundle=neural_bundle,
            quantum_result=quantum_result,
            quantum_kernel_result=kernel_result,
            rl_advisory=rl_advisory,
            portfolio_state=portfolio_state,
            market_regime=regime,
            risk_precheck_ok=not self._rt.kill_switch_active,
            risk_precheck_reason="kill_switch" if self._rt.kill_switch_active else "",
        )
        result["blackboard"] = blackboard.to_dict()

        if self._rt.settings.enable_ai_council or self._rt.settings.enable_neural_lab:
            try:
                ensemble_output = self._rt._ensemble_engine.decide(blackboard)
                result["ensemble"] = ensemble_output.to_dict()
                trace = self._rt.trace_store.get(trace_id)
                if trace:
                    trace.metadata["ensemble"] = ensemble_output.to_dict()
                    trace.add_event(
                        "ensemble_decision",
                        "EnsembleDecisionEngine",
                        {"action": ensemble_output.action, "confidence": ensemble_output.confidence},
                    )
                    self._rt.trace_store.save(trace)
            except Exception as exc:
                logger.warning("decision cycle: ensemble engine error: %s", exc)

        if self._rt.settings.enable_goal_governor:
            try:
                result["goal_governor"] = self._rt._goal_governor.status()
            except Exception as exc:
                logger.warning("decision cycle: goal governor error: %s", exc)

        result["trace_metadata"] = {
            "trace_id": trace_id,
            "components_active": {
                "ai_council": self._rt.settings.enable_ai_council,
                "neural_lab": self._rt.settings.enable_neural_lab,
                "quantum_lab": self._rt.settings.enable_quantum_lab,
                "marl_lab": self._rt.settings.enable_marl_lab,
                "goal_governor": self._rt.settings.enable_goal_governor,
            },
        }
        return result, blackboard

    def _regime_for(self, symbols: list[str]) -> str:
        try:
            reg_result = self._rt.regime_classify({"symbol": symbols[0] if symbols else "NIFTY", "days": 30})
            return reg_result.get("regime", "unknown")
        except Exception:
            return "unknown"

    def _bars_map_for_neural(self, symbols: list[str]) -> dict[str, list[dict]]:
        bars_map: dict[str, list[dict]] = {}
        start = date.today() - timedelta(days=60)
        for sym in list(symbols)[:10]:
            try:
                bars = self._rt.synthetic_data.generate_daily_bars(sym, start, days=60)
                if bars:
                    bars_map[sym] = [
                        {
                            "close": bar.close,
                            "high": bar.high,
                            "low": bar.low,
                            "volume": bar.volume,
                            "open": bar.open,
                        }
                        for bar in bars
                    ]
            except Exception:
                pass
        return bars_map

    def _run_marl_advisory(self, portfolio_state: dict) -> dict:
        try:
            from collections import Counter

            from trading_platform.rl.env import EnvObservation

            obs = EnvObservation(
                features=[
                    portfolio_state.get("drawdown", 0.0),
                    portfolio_state.get("equity", 0.0)
                    / max(self._rt.settings.initial_capital, 1.0),
                ],
                portfolio_pnl=portfolio_state.get("equity", self._rt.settings.initial_capital)
                - self._rt.settings.initial_capital,
                open_positions=portfolio_state.get("open_positions", 0),
                volatility=0.0,
                liquidity=1.0,
                step=0,
            )
            labels = {
                0: "NOOP",
                1: "PROPOSE_ENTRY",
                2: "PROPOSE_EXIT",
                3: "PROPOSE_HEDGE",
                4: "SIZE_UP",
                5: "SIZE_DOWN",
            }
            policy_votes: list[dict] = []
            for rec in self._rt._policy_registry._records.values():
                if rec.status not in {"shadow", "paper", "live_canary", "live_approved"}:
                    continue
                policy = self._rt._policy_registry.get(rec.policy_id)
                if policy is None:
                    continue
                action = policy.act(obs)
                policy_votes.append({
                    "policy_id": rec.policy_id,
                    "role": rec.role,
                    "status": rec.status,
                    "action": action,
                    "action_label": labels.get(action, "UNKNOWN"),
                    "can_submit_live_orders": rec.can_submit_live_orders,
                })
            if not policy_votes:
                return {"enabled": True, "policy_count": 0, "advisory_only": True, "note": "no_active_policies"}
            majority_action, majority_count = Counter(v["action"] for v in policy_votes).most_common(1)[0]
            return {
                "enabled": True,
                "policy_count": len(policy_votes),
                "majority_action": majority_action,
                "majority_action_label": labels.get(majority_action, "UNKNOWN"),
                "majority_confidence": majority_count / len(policy_votes),
                "policy_votes": policy_votes,
                "advisory_only": True,
            }
        except Exception as exc:
            logger.warning("decision cycle: marl advisory error: %s", exc)
            return {"enabled": True, "error": str(exc), "advisory_only": True}

    @staticmethod
    def _pipeline_candidates(baseline_payload: dict) -> list[dict]:
        candidates: list[dict] = []
        for scan in baseline_payload.get("scans", []):
            for candidate in scan.get("candidates", []):
                rd = candidate.get("risk_decision") or {}
                candidates.append({
                    "underlying": scan.get("underlying", ""),
                    "symbol": (candidate.get("instrument") or {}).get("symbol", ""),
                    "strategy_name": candidate.get("strategy_name", ""),
                    "approved": bool(rd.get("approved", False)),
                    "reason": rd.get("reason", candidate.get("reason", "")),
                    "risk_score": rd.get("risk_score", 0.0),
                    "confidence": (candidate.get("signal") or {}).get("confidence", 0.0),
                })
        return candidates
