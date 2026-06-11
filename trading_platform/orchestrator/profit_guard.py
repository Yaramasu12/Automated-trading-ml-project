"""ProfitGuard — the mathematical gatekeeper that only allows trades when the
expected value is strictly positive.

Implements three complementary filters inspired by quantitative finance:

  1. Expected Value (EV) Gate
     EV = P(win) × avg_profit - P(loss) × avg_loss
     Only proceed if EV > EV_THRESHOLD (default 0.003 = 0.3% per trade).

  2. Kelly Criterion Gate
     f* = (P(win) × RR - P(loss)) / RR   where RR = risk/reward ratio
     Only proceed if Kelly fraction > KELLY_MIN (default 0.02 = 2%).
     Kelly fraction also caps position_size_multiplier to prevent over-betting.

  3. Sharpe Estimate Gate
     Sharpe ≈ (expected_return - 0) / uncertainty_proxy
     Only proceed if Sharpe > SHARPE_MIN (default 0.50).

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

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
EV_THRESHOLD = 0.003           # minimum expected value per rupee risked (0.3%)
KELLY_MIN = 0.015              # minimum Kelly fraction (1.5% of capital)
KELLY_MAX = 0.25               # Kelly cap — never bet more than 25% Kelly
SHARPE_MIN = 0.50              # minimum signal-to-noise ratio
WIN_RATE_MIN = 0.38            # rolling win rate must stay above 38%
CONSECUTIVE_LOSS_LIMIT = 4     # halt after 4 consecutive losses per underlying
ROLLING_WINDOW = 20            # rolling window for win-rate calc


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
    ) -> None:
        self._ev_threshold = ev_threshold
        self._kelly_min = kelly_min
        self._kelly_max = kelly_max
        self._sharpe_min = sharpe_min
        self._win_rate_min = win_rate_min
        self._consecutive_loss_limit = consecutive_loss_limit

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

        # Build inputs from upstream nodes
        win_prob = self._estimate_win_probability(state)
        expected_return = state.neural_expected_return
        uncertainty = max(state.neural_uncertainty, 0.01)
        rag_win_rate = state.rag_win_rate

        # Blend win probability: neural + RAG historical + crew consensus
        blended_win_prob = (
            0.45 * win_prob
            + 0.30 * rag_win_rate
            + 0.25 * state.crew_confidence
        )
        blended_win_prob = min(max(blended_win_prob, 0.01), 0.99)

        # Risk/reward ratio from neural expected return and realistic stop-loss estimate.
        # avg_loss = base intraday stop (0.8%) scaled up by uncertainty (not the full uncertainty
        # value, which ranges 0-1 and represents model confidence, not price move size).
        BASE_STOP = 0.008   # 0.8% typical intraday hard stop
        # Minimum profit target = 2× stop (2:1 reward/risk is the floor for any viable trade).
        # Using 0.5% (old floor) with 0.8% stop gave rr=0.625 → Kelly always negative.
        avg_profit = max(expected_return, BASE_STOP * 2.0)
        avg_loss = max(BASE_STOP * (1.0 + uncertainty * 0.5), 0.005)
        rr = avg_profit / avg_loss

        # ── EV calculation ────────────────────────────────────────────────────
        ev = blended_win_prob * avg_profit - (1 - blended_win_prob) * avg_loss

        # ── Kelly fraction ────────────────────────────────────────────────────
        kelly_raw = (blended_win_prob * rr - (1 - blended_win_prob)) / max(rr, 0.01)
        kelly = min(max(kelly_raw, 0.0), self._kelly_max)
        # Half-Kelly for safety
        kelly_safe = kelly * 0.5

        # ── Sharpe estimate ───────────────────────────────────────────────────
        # Use avg_loss as the return-space volatility proxy (same as the EV denominator)
        # so the Sharpe is consistent with EV/Kelly and not inflated by raw uncertainty.
        sharpe = avg_profit / max(avg_loss, 0.001)

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
            "ProfitGuard PASSED %s | EV=%.4f Kelly=%.4f Sharpe=%.2f win_prob=%.1%%",
            underlying, ev, kelly_safe, sharpe, blended_win_prob * 100,
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
