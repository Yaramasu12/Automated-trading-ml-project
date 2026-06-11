"""ReflectionEngine — post-trade outcome scoring and adaptive weight learning.

Implements the Reflection Agent pattern from the 500-AI-Agents repo:
  - Observe outcome (win/loss, P&L %)
  - Score the decision quality (did the agent signal align with outcome?)
  - Update agent weights (online Exponential Moving Average)
  - Feed outcome back to MarketRAG pattern store
  - Feed outcome back to ProfitGuard rolling window
  - Emit learning journal entries for the trace store

The key insight: agents that predicted correctly GAIN weight for future cycles;
agents that were wrong LOSE weight. Over time this self-calibrates the crew
so that the most accurate specialist dominates decisions.

Pattern reference:
  - AutoGen: Reflection Agent
  - LangGraph: Self-RAG (re-scoring and correcting)
  - CrewAI: Agent with Memory and self-improvement
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trading_platform.orchestrator.market_rag import MarketRAG
    from trading_platform.orchestrator.profit_guard import ProfitGuard

logger = logging.getLogger(__name__)

# ── Learning hyperparameters ──────────────────────────────────────────────────
EMA_ALPHA = 0.15          # weight update speed (0 = never update, 1 = fully replace)
WEIGHT_MIN = 0.30         # floor: no agent drops below this weight
WEIGHT_MAX = 2.50         # cap: no agent grows beyond this weight
INITIAL_WEIGHT = 1.0      # all agents start equal
REWARD_CORRECT = 0.12     # weight gain for a correct prediction
PENALTY_WRONG = 0.08      # weight loss for an incorrect prediction
PENALTY_HALT_WRONG = 0.15 # extra penalty for halting when trade would have won
REWARD_HALT_RIGHT = 0.20  # extra reward for halting when trade would have lost

# Quality score thresholds
GREAT_TRADE_THRESHOLD = 0.015   # >= 1.5% return → great
GOOD_TRADE_THRESHOLD = 0.005    # >= 0.5% return → good
BAD_TRADE_THRESHOLD = -0.005    # <= -0.5% return → bad


@dataclass
class TradeReflection:
    """The reflection record for a single completed trade."""
    trace_id: str
    underlying: str
    action_taken: str         # BUY / SELL / HOLD (no trade)
    pnl_pct: float
    won: bool
    quality_score: float      # 0 = worst, 1 = best
    agent_predictions: dict[str, str]   # agent_name → predicted_action
    agent_correctness: dict[str, bool]  # agent_name → was_correct
    weight_deltas: dict[str, float]     # agent_name → Δweight applied
    new_weights: dict[str, float]
    crew_consensus: float
    regime: str
    market_features: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "underlying": self.underlying,
            "action_taken": self.action_taken,
            "pnl_pct": round(self.pnl_pct, 5),
            "won": self.won,
            "quality_score": round(self.quality_score, 4),
            "agent_correctness": self.agent_correctness,
            "weight_deltas": {k: round(v, 4) for k, v in self.weight_deltas.items()},
            "new_weights": {k: round(v, 4) for k, v in self.new_weights.items()},
            "crew_consensus": round(self.crew_consensus, 4),
            "regime": self.regime,
            "ts": self.ts,
        }


class ReflectionEngine:
    """Online learning engine: observes trade outcomes and updates agent weights.

    Maintains per-agent EMA weights. Called after each trade resolves
    (typically at position close or EOD mark-to-market).
    """

    def __init__(
        self,
        ema_alpha: float = EMA_ALPHA,
        weight_min: float = WEIGHT_MIN,
        weight_max: float = WEIGHT_MAX,
    ) -> None:
        self._alpha = ema_alpha
        self._w_min = weight_min
        self._w_max = weight_max

        # Current agent weights (shared with OrchestratorState)
        self._weights: dict[str, float] = {}

        # Performance history
        self._reflections: list[TradeReflection] = []
        self._agent_accuracy: dict[str, list[bool]] = {}   # rolling accuracy tracking

        self._db = None   # wired via set_db() in TradingRuntime

    # ── DB wiring ─────────────────────────────────────────────────────────────

    def set_db(self, db) -> None:
        """Wire a TradingDatabase so agent weights and reflections persist."""
        self._db = db

    def load_weights_from_db(self) -> int:
        """Restore agent weights from the database on startup.

        Returns the number of agents restored.
        """
        if self._db is None:
            return 0
        try:
            weights = self._db.load_agent_weights()
        except Exception:
            return 0
        if weights:
            self._weights.update(weights)
            logger.info("ReflectionEngine: restored weights for %d agents", len(weights))
        return len(weights)

    # ── Main reflection entry point ───────────────────────────────────────────

    def reflect(
        self,
        *,
        trace_id: str,
        underlying: str,
        action_taken: str,
        pnl_pct: float,
        crew_votes: list[Any],        # list[AgentVoteSummary]
        crew_consensus: float,
        regime: str,
        market_features: dict[str, Any],
        market_rag: "MarketRAG | None" = None,
        profit_guard: "ProfitGuard | None" = None,
    ) -> TradeReflection:
        """Process a resolved trade and update the system's learning state."""

        won = pnl_pct > 0
        quality = self._quality_score(pnl_pct)

        # ── Determine correctness per agent ───────────────────────────────
        agent_predictions: dict[str, str] = {v.agent_name: v.action for v in crew_votes}
        agent_correctness: dict[str, bool] = {}
        weight_deltas: dict[str, float] = {}

        for vote in crew_votes:
            name = vote.agent_name
            predicted = vote.action.upper()
            current_weight = self._weights.get(name, INITIAL_WEIGHT)

            correct = self._was_correct(predicted, action_taken, won)
            agent_correctness[name] = correct

            # Track rolling accuracy
            self._agent_accuracy.setdefault(name, []).append(correct)
            if len(self._agent_accuracy[name]) > 50:
                self._agent_accuracy[name] = self._agent_accuracy[name][-50:]

            # Compute weight delta
            delta = self._compute_delta(predicted, action_taken, won, pnl_pct)
            weight_deltas[name] = delta

            # EMA update
            new_weight = current_weight * (1 - self._alpha) + (current_weight + delta) * self._alpha
            new_weight = max(self._w_min, min(self._w_max, new_weight))
            self._weights[name] = new_weight

        new_weights = dict(self._weights)

        # ── Update MarketRAG pattern store ────────────────────────────────
        if market_rag is not None:
            feats = market_features
            market_rag.upsert_pattern(
                regime=regime,
                news_sentiment=feats.get("news_sentiment", 0.0),
                volatility=feats.get("realized_volatility", feats.get("predicted_volatility", 0.20)),
                momentum=feats.get("momentum_20", feats.get("momentum_5", 0.0)),
                rsi=feats.get("rsi_14", 50.0),
                won=won,
                pnl_pct=pnl_pct,
            )

        # ── Update ProfitGuard rolling window ─────────────────────────────
        if profit_guard is not None:
            profit_guard.record_outcome(
                underlying=underlying,
                won=won,
                pnl_pct=pnl_pct,
                ts=datetime.now(timezone.utc).isoformat(),
            )

        reflection = TradeReflection(
            trace_id=trace_id,
            underlying=underlying,
            action_taken=action_taken,
            pnl_pct=pnl_pct,
            won=won,
            quality_score=quality,
            agent_predictions=agent_predictions,
            agent_correctness=agent_correctness,
            weight_deltas=weight_deltas,
            new_weights=new_weights,
            crew_consensus=crew_consensus,
            regime=regime,
            market_features=market_features,
        )
        self._reflections.append(reflection)

        # Persist to database so learning survives restarts
        if self._db is not None:
            try:
                self._db.save_reflection(
                    trace_id=trace_id,
                    underlying=underlying,
                    won=won,
                    pnl_pct=pnl_pct,
                    quality=quality,
                    regime=regime,
                    payload=reflection.to_dict(),
                    ts=reflection.ts,
                )
                if new_weights:
                    self._db.save_agent_weights(new_weights)
            except Exception as _e:
                logger.debug("ReflectionEngine: DB persist error: %s", _e)

        log_level = logging.INFO if won else logging.WARNING
        logger.log(
            log_level,
            "Reflection %s: %s pnl=%.2f%% quality=%.2f weights_updated=%d",
            underlying, "WIN" if won else "LOSS",
            pnl_pct * 100, quality, len(weight_deltas),
        )

        return reflection

    # ── Weight accessors ──────────────────────────────────────────────────────

    def get_weights(self) -> dict[str, float]:
        """Return current agent weights (copy)."""
        return dict(self._weights)

    def get_weight(self, agent_name: str) -> float:
        return self._weights.get(agent_name, INITIAL_WEIGHT)

    def reset_weights(self) -> None:
        self._weights.clear()

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        total = len(self._reflections)
        wins = sum(1 for r in self._reflections if r.won)
        agent_acc = {
            name: {
                "accuracy": sum(outcomes) / len(outcomes) if outcomes else None,
                "weight": round(self._weights.get(name, INITIAL_WEIGHT), 4),
                "sample_size": len(outcomes),
            }
            for name, outcomes in self._agent_accuracy.items()
        }
        recent = [r.to_dict() for r in self._reflections[-10:]]
        return {
            "total_reflections": total,
            "overall_win_rate": wins / total if total else None,
            "agent_weights": {k: round(v, 4) for k, v in self._weights.items()},
            "agent_accuracy": agent_acc,
            "recent_reflections": recent,
        }

    def recent_reflections(self, n: int = 20) -> list[dict]:
        return [r.to_dict() for r in self._reflections[-n:]]

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _quality_score(pnl_pct: float) -> float:
        """Map P&L to a [0, 1] quality score using sigmoid-like mapping."""
        if pnl_pct >= GREAT_TRADE_THRESHOLD:
            return 0.85 + min(0.15, (pnl_pct - GREAT_TRADE_THRESHOLD) * 5)
        if pnl_pct >= GOOD_TRADE_THRESHOLD:
            return 0.60 + (pnl_pct - GOOD_TRADE_THRESHOLD) / (GREAT_TRADE_THRESHOLD - GOOD_TRADE_THRESHOLD) * 0.25
        if pnl_pct >= 0:
            return 0.50 + pnl_pct / GOOD_TRADE_THRESHOLD * 0.10
        if pnl_pct >= BAD_TRADE_THRESHOLD:
            return 0.40 + pnl_pct / BAD_TRADE_THRESHOLD * 0.10
        # significant loss
        return max(0.0, 0.30 + pnl_pct * 10)

    @staticmethod
    def _was_correct(predicted: str, action_taken: str, won: bool) -> bool:
        """Judge whether an agent's prediction was correct."""
        # Agent voted HOLD/HALT and no trade → correct if there was nothing to win
        if predicted in {"HOLD", "REDUCE"} and action_taken in {"HOLD", "SKIP"}:
            return True   # cautious abstention is always defensible

        # Agent voted HALT and trade happened anyway → depends on outcome
        if predicted == "HALT":
            return not won   # correct to halt if we would have lost

        if predicted in {"BUY", "CALL"} and action_taken in {"BUY", "CALL"}:
            return won
        if predicted in {"SELL", "SHORT", "PUT"} and action_taken in {"SELL", "SHORT"}:
            return won
        if predicted in {"BUY", "CALL"} and action_taken in {"SELL", "SHORT"}:
            return False  # opposite direction
        if predicted in {"SELL", "SHORT"} and action_taken in {"BUY", "CALL"}:
            return False

        # Default: correct if won
        return won

    @staticmethod
    def _compute_delta(predicted: str, action_taken: str, won: bool, pnl_pct: float) -> float:
        """Compute the weight delta for one agent vote."""
        # Scale reward/penalty by magnitude of outcome
        magnitude = min(abs(pnl_pct) / 0.02, 2.0)  # normalise at 2% swing

        if predicted == "HALT" and not won:
            # Agent correctly prevented a loss → big reward
            return REWARD_HALT_RIGHT * magnitude

        if predicted == "HALT" and won:
            # Agent blocked a winning trade → penalty
            return -PENALTY_HALT_WRONG * magnitude

        correct = ReflectionEngine._was_correct(predicted, action_taken, won)
        if correct:
            return REWARD_CORRECT * magnitude
        return -PENALTY_WRONG * magnitude
