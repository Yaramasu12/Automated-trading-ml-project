"""ProfitGuard — the mathematical gatekeeper that only allows trades when the
expected value is strictly positive.

Implements three complementary filters inspired by quantitative finance:

  0. Honest win probability
     P(win) is the probability of hitting the REAL target before the REAL stop.
     For a driftless walk that is the barrier probability stop/(stop+target)
     (~0.29 at 2.5:1), tilted by directional edge from neural/RAG/crew.  A signal
     with no edge is therefore correctly modelled as a post-cost loser — the old
     model floored avg_profit at 2× an understated stop and waved coin-flips
     through.  stop/target mirror what the ExitManager actually applies.

  1. Expected Value (EV) Gate — NET of costs
     EV = P(win) × target - P(loss) × stop - round_trip_cost
     Only proceed if EV > EV_THRESHOLD (default 0.0015 = 0.15% after costs).

  2. Kelly Criterion Gate
     f* = (P(win) × RR - P(loss)) / RR   where RR = target/stop
     Only proceed if half-Kelly fraction > KELLY_MIN (default 0.015 = 1.5%).
     Kelly fraction also caps position_size_multiplier to prevent over-betting.

  3. Sharpe Estimate Gate
     Sharpe ≈ directional_edge / model_uncertainty   (signal-to-noise)
     Only proceed if Sharpe > SHARPE_MIN (default 0.15), and only when the
     neural service actually produced a forecast.

  4. Consecutive Loss Circuit Breaker
     If last N trades on this underlying all lost → skip this cycle.

  5. Win-Rate Momentum Filter
     Rolling 20-trade win rate must be > 40% to keep trading.

Pattern reference: AutoGen Optimizer + Reflection Agent (post-trade feedback loop).
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from trading_platform.orchestrator.state import OrchestratorState, NodeResult, ProfitGateResult
from trading_platform.logging_safety import note_swallowed

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
EV_THRESHOLD = 0.0015          # minimum expected value per trade, NET of costs (0.15%)
KELLY_MIN = 0.015              # minimum Kelly fraction (1.5% of capital)
KELLY_MAX = 0.25               # Kelly cap — never bet more than 25% Kelly
SHARPE_MIN = 0.15              # minimum signal-to-noise ratio (edge / uncertainty)
WIN_RATE_MIN = 0.38            # rolling win rate must stay above 38%
CONSECUTIVE_LOSS_LIMIT = 4     # halt after 4 consecutive losses per underlying
ROLLING_WINDOW = 20            # rolling window for win-rate calc

# ── Real trade structure ──────────────────────────────────────────────────────
# These MUST mirror what the ExitManager actually applies (see exit_plan.py /
# runtime._on_fill).  The EV/Kelly math is only meaningful if it models the same
# stop and target the trade will really exit at — otherwise the gate "approves"
# trades whose true expected value is negative.
STOP_PCT = 0.015               # real default hard stop (matches ExitPlan.from_trade)
TARGET_PCT = 0.038             # real default target (matches ExitPlan.from_trade)
ATR_STOP_MULTIPLIER = 2.0      # ATR-based stop distance (matches runtime _on_fill)
RR_TARGET = 2.5                # exit manager enforces a 2.5:1 target on the stop distance
EDGE_SENSITIVITY = 1.2         # how strongly directional edge shifts win prob off the no-edge barrier
ROUND_TRIP_COST = 0.0015       # brokerage+STT+slippage round-trip drag subtracted from EV (~0.15%)


@dataclass
class TradeOutcomeRecord:
    underlying: str
    won: bool
    pnl_pct: float
    ts: str


class ProfitGuard:
    """Stateful profit guard — tracks rolling performance per underlying.

    Instantiated once per TradingRuntime and persists across cycles.
    """

    def __init__(
        self,
        ev_threshold: float = EV_THRESHOLD,
        kelly_min: float = KELLY_MIN,
        kelly_max: float = KELLY_MAX,
        sharpe_min: float = SHARPE_MIN,
        win_rate_min: float = WIN_RATE_MIN,
        consecutive_loss_limit: int = CONSECUTIVE_LOSS_LIMIT,
        stop_pct: float = STOP_PCT,
        target_pct: float = TARGET_PCT,
        atr_stop_multiplier: float = ATR_STOP_MULTIPLIER,
        rr_target: float = RR_TARGET,
        edge_sensitivity: float = EDGE_SENSITIVITY,
        round_trip_cost: float = ROUND_TRIP_COST,
    ) -> None:
        self._ev_threshold = ev_threshold
        self._kelly_min = kelly_min
        self._kelly_max = kelly_max
        self._sharpe_min = sharpe_min
        self._win_rate_min = win_rate_min
        self._consecutive_loss_limit = consecutive_loss_limit
        self._stop_pct = stop_pct
        self._target_pct = target_pct
        self._atr_stop_multiplier = atr_stop_multiplier
        self._rr_target = rr_target
        self._edge_sensitivity = edge_sensitivity
        self._round_trip_cost = round_trip_cost

        # Per-underlying rolling performance
        self._rolling: dict[str, deque[bool]] = {}           # underlying → deque of bool
        self._consecutive_losses: dict[str, int] = {}
        self._all_outcomes: list[TradeOutcomeRecord] = []

        self._db = None   # wired via set_db() in TradingRuntime

    # ─────────────────────────────────────────────── DB wiring

    def set_db(self, db) -> None:
        """Wire a TradingDatabase so rolling outcomes survive restarts."""
        self._db = db

    def load_from_db(self) -> int:
        """Restore rolling win-rate state from persisted outcomes.

        Replays the last ROLLING_WINDOW outcomes per underlying so the guard
        has accurate per-asset history after a restart.  Returns the number of
        underlyings restored.
        """
        if self._db is None:
            return 0
        loaded = 0
        try:
            underlyings = self._db.load_all_outcome_underlyings()
        except Exception:
            return 0
        for underlying in underlyings:
            try:
                rows = self._db.load_recent_outcomes(underlying, limit=ROLLING_WINDOW)
            except Exception:
                continue
            if not rows:
                continue
            dq: deque[bool] = deque(maxlen=ROLLING_WINDOW)
            consec = 0
            for row in rows:
                won = bool(row["won"])
                dq.append(won)
                if won:
                    consec = 0
                else:
                    consec += 1
            self._rolling[underlying] = dq
            self._consecutive_losses[underlying] = consec
            loaded += 1
        if loaded:
            logger.info("ProfitGuard: restored rolling state for %d underlyings", loaded)
        return loaded

    # ─────────────────────────────────────────────── public API

    def evaluate(self, state: OrchestratorState) -> NodeResult:
        """Node function: compute EV/Kelly/Sharpe and decide if trade is allowed."""
        underlying = state.underlying

        # ── Real trade structure (must match the ExitManager, not a fiction) ──
        # Prefer the actual ATR-based stop the exit manager will use when ATR is
        # available in the feature snapshot; otherwise fall back to the flat
        # percentage stop/target the ExitPlan applies by default.
        stop_pct = self._stop_pct
        target_pct = self._target_pct
        try:
            mf = state.market_features or {}
            atr = float(mf.get("atr_14", 0.0) or 0.0)
            mark = float(mf.get("close", mf.get("price", 0.0)) or 0.0)
            if atr > 0 and mark > 0:
                stop_pct = (atr * self._atr_stop_multiplier) / mark
                target_pct = stop_pct * self._rr_target
        except Exception as exc:
            note_swallowed("profit_guard.atr_stop", exc)
        stop_pct = max(stop_pct, 0.002)
        target_pct = max(target_pct, stop_pct * 1.5)
        rr = target_pct / stop_pct

        # ── Honest win probability ────────────────────────────────────────────
        # Start from the no-edge barrier probability of hitting the target before
        # the stop for a driftless walk: P = stop / (stop + target).  With a 2.5:1
        # structure this is ~0.29 — i.e. a signal with NO edge is correctly a loser
        # after costs.  Real directional edge then tilts this baseline up/down.
        barrier_win = stop_pct / (stop_pct + target_pct)

        # Directional edge sources, each centred at 0 (no edge):
        side_dir_prob = self._estimate_win_probability(state)   # neural P(favorable), 0.5 if absent
        neural_edge = side_dir_prob - 0.5
        rag_edge = state.rag_win_rate - 0.5
        # crew_confidence is a confidence in the chosen side, not a probability —
        # treat it as a weak directional edge, not a raw win rate.
        crew_edge = (state.crew_confidence - 0.5) * 0.5
        combined_edge = 0.5 * neural_edge + 0.3 * rag_edge + 0.2 * crew_edge

        win_prob = barrier_win + self._edge_sensitivity * combined_edge
        win_prob = min(max(win_prob, 0.02), 0.98)
        blended_win_prob = win_prob   # name kept for the gate/result payload below

        # Keep avg_profit/avg_loss names for the result payload (R:R, logging).
        avg_profit = target_pct
        avg_loss = stop_pct

        # ── EV calculation — NET of round-trip trading costs ──────────────────
        ev_gross = win_prob * target_pct - (1 - win_prob) * stop_pct
        ev = ev_gross - self._round_trip_cost

        # ── Kelly fraction ────────────────────────────────────────────────────
        kelly_raw = (win_prob * rr - (1 - win_prob)) / max(rr, 0.01)
        kelly = min(max(kelly_raw, 0.0), self._kelly_max)
        # Half-Kelly for safety
        kelly_safe = kelly * 0.5

        # ── Sharpe estimate ───────────────────────────────────────────────────
        # Signal-to-noise: directional edge per unit of model uncertainty.  Only
        # gated when the neural service actually ran (see decision block below).
        sharpe = max(combined_edge, 0.0) / max(state.neural_uncertainty, 0.1)

        # ── Rolling performance check ─────────────────────────────────────────
        rolling_win_rate = self._rolling_win_rate(underlying)
        consecutive = self._consecutive_losses.get(underlying, 0)

        # ── Decision ──────────────────────────────────────────────────────────
        reasons: list[str] = []
        passed = True

        if ev < self._ev_threshold:
            passed = False
            reasons.append(f"EV={ev:.4f} < threshold={self._ev_threshold:.4f}")

        if kelly_safe < self._kelly_min:
            passed = False
            reasons.append(f"Kelly={kelly_safe:.4f} < min={self._kelly_min:.4f}")

        if sharpe < self._sharpe_min and state.neural_passed:
            passed = False
            reasons.append(f"Sharpe={sharpe:.2f} < min={self._sharpe_min:.2f}")

        if rolling_win_rate is not None and rolling_win_rate < self._win_rate_min:
            passed = False
            reasons.append(
                f"rolling_win_rate={rolling_win_rate:.1%} < min={self._win_rate_min:.1%}"
            )

        if consecutive >= self._consecutive_loss_limit:
            passed = False
            reasons.append(
                f"consecutive_losses={consecutive} >= limit={self._consecutive_loss_limit}"
            )

        gate = ProfitGateResult(
            passed=passed,
            expected_value=round(ev, 5),
            kelly_fraction=round(kelly_safe, 5),
            sharpe_estimate=round(sharpe, 4),
            win_probability=round(blended_win_prob, 4),
            risk_reward_ratio=round(rr, 4),
            reason=" | ".join(reasons) if reasons else "all gates passed",
        )

        if not passed:
            logger.info(
                "ProfitGuard BLOCKED %s: %s", underlying, gate.reason
            )
            return NodeResult(
                updates={"profit_gate": gate},
                halt=True,
                halt_reason=f"profit_guard: {gate.reason}",
            )

        logger.info(
            "ProfitGuard PASSED %s | EV=%.4f Kelly=%.4f Sharpe=%.2f win_prob=%.1f%% rr=%.2f",
            underlying, ev, kelly_safe, sharpe, blended_win_prob * 100, rr,
        )
        return NodeResult(updates={"profit_gate": gate})

    def record_outcome(self, underlying: str, won: bool, pnl_pct: float, ts: str) -> None:
        """Called by the reflection engine after a trade resolves."""
        if underlying not in self._rolling:
            self._rolling[underlying] = deque(maxlen=ROLLING_WINDOW)

        self._rolling[underlying].append(won)

        if won:
            self._consecutive_losses[underlying] = 0
        else:
            self._consecutive_losses[underlying] = (
                self._consecutive_losses.get(underlying, 0) + 1
            )

        self._all_outcomes.append(
            TradeOutcomeRecord(underlying=underlying, won=won, pnl_pct=pnl_pct, ts=ts)
        )

        if self._db is not None:
            try:
                self._db.save_outcome(underlying=underlying, won=won, pnl_pct=pnl_pct, ts=ts)
            except Exception as _e:
                logger.debug("ProfitGuard: DB persist error: %s", _e)

    def stats(self, underlying: str | None = None) -> dict:
        if underlying:
            rwr = self._rolling_win_rate(underlying)
            return {
                "underlying": underlying,
                "rolling_win_rate": rwr,
                "consecutive_losses": self._consecutive_losses.get(underlying, 0),
                "rolling_window": ROLLING_WINDOW,
            }
        # Global stats
        total = len(self._all_outcomes)
        wins = sum(1 for o in self._all_outcomes if o.won)
        return {
            "total_trades": total,
            "win_count": wins,
            "global_win_rate": wins / total if total else None,
            "per_underlying": {
                u: {
                    "rolling_win_rate": self._rolling_win_rate(u),
                    "consecutive_losses": self._consecutive_losses.get(u, 0),
                }
                for u in self._rolling
            },
        }

    # ─────────────────────────────────────────────── private helpers

    def _estimate_win_probability(self, state: OrchestratorState) -> float:
        """Derive win probability from neural direction probability."""
        raw = state.neural_direction_prob
        action = state.crew_action.upper()

        if action in {"BUY", "CALL"}:
            # P(win) = P(up) for long trades
            return raw
        if action in {"SELL", "SHORT", "PUT"}:
            # P(win) = P(down) = 1 - P(up)
            return 1.0 - raw

        # HOLD / unknown — neutral
        return 0.5

    def _rolling_win_rate(self, underlying: str) -> float | None:
        outcomes = self._rolling.get(underlying)
        if not outcomes or len(outcomes) < 5:
            return None   # not enough data to penalise
        return sum(outcomes) / len(outcomes)
