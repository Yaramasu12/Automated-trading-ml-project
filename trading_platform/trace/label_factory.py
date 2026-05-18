from __future__ import annotations

"""Label/Outcome Factory — generates ML labels from trade lifecycle events.

Computes per the canvas specification:
- Triple-barrier outcome  (target hit / stop hit / hold expired)
- Forward return buckets  (strong_buy / buy / hold / sell / strong_sell)
- Drawdown after entry
- Volatility expansion    (exit_vol / entry_vol)
- Stop/target hit ordering
- Slippage surprise       (realized − expected; positive = worse)
- News reaction tag       (supplied externally by caller)
- Meta-label score        (0.0 = total failure → 1.0 = perfect, used by champion/challenger)

Usage
-----
On entry fill:
    factory.record_entry(symbol, side, fill_price, ...)

On exit fill:
    label = factory.compute_exit_label(symbol, exit_price, ...)
    if label:
        trace_store.save(...)          # attach to trace
        neural_meta_labeler.update(...)
"""

import collections
import itertools
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

# ── Public type aliases ───────────────────────────────────────────────────────

TripleBarrierOutcome = Literal["target_hit", "stop_hit", "hold_expired", "unknown"]
ForwardReturnBucket = Literal["strong_buy", "buy", "hold", "sell", "strong_sell"]


# ── Parameter container ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class TripleBarrierParams:
    """Thresholds for the triple-barrier label method."""
    hold_bars: int = 20          # max bars before the hold_expired barrier fires
    target_pct: float = 0.030   # +3 % upper barrier
    stop_pct: float = 0.015     # -1.5 % lower barrier
    volatility_window: int = 20  # lookback bars for entry vol estimation


# ── Output dataclasses ────────────────────────────────────────────────────────

@dataclass
class OutcomeLabel:
    """Complete ML label set produced for one entry/exit trade pair.

    All fields are safe to serialise (no secrets, no credentials).
    """
    trace_id: str
    symbol: str
    side: str                              # BUY | SELL

    # Triple-barrier
    barrier_outcome: TripleBarrierOutcome
    barrier_return_pct: float
    bars_held: int

    # Forward return bucket
    forward_bucket: ForwardReturnBucket
    forward_return_pct: float

    # Risk metrics
    max_drawdown_pct: float                # worst intra-trade drawdown (0.0 if unknown)
    volatility_expansion: float            # exit_vol / entry_vol (1.0 = unchanged)

    # Slippage
    expected_slippage_pct: float
    realized_slippage_pct: float
    slippage_surprise: float               # realized − expected; positive = worse than expected

    # Meta-label for champion/challenger (0.0 = worst, 1.0 = best)
    meta_label_score: float

    # Raw prices and prediction error
    entry_price: float = 0.0
    exit_price: float = 0.0
    predicted_return: float = 0.0
    prediction_error: float = 0.0

    # Optional news-reaction tag (set by caller if a news event overlapped the trade)
    news_reaction_tag: str = ""

    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "symbol": self.symbol,
            "side": self.side,
            "barrier_outcome": self.barrier_outcome,
            "barrier_return_pct": round(self.barrier_return_pct, 6),
            "bars_held": self.bars_held,
            "forward_bucket": self.forward_bucket,
            "forward_return_pct": round(self.forward_return_pct, 6),
            "max_drawdown_pct": round(self.max_drawdown_pct, 6),
            "volatility_expansion": round(self.volatility_expansion, 4),
            "expected_slippage_pct": round(self.expected_slippage_pct, 6),
            "realized_slippage_pct": round(self.realized_slippage_pct, 6),
            "slippage_surprise": round(self.slippage_surprise, 6),
            "meta_label_score": round(self.meta_label_score, 4),
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "predicted_return": self.predicted_return,
            "prediction_error": self.prediction_error,
            "news_reaction_tag": self.news_reaction_tag,
            "ts": self.ts.isoformat(),
        }


# ── Private entry state ───────────────────────────────────────────────────────

@dataclass
class _EntryRecord:
    symbol: str
    side: str
    entry_price: float
    predicted_return: float
    entry_vol: float
    expected_slippage_pct: float
    trace_id: str
    metadata: dict[str, Any]
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _forward_bucket(pct: float) -> ForwardReturnBucket:
    if pct >= 0.03:
        return "strong_buy"
    if pct >= 0.01:
        return "buy"
    if pct >= -0.01:
        return "hold"
    if pct >= -0.03:
        return "sell"
    return "strong_sell"


def _barrier_outcome(return_pct: float, params: TripleBarrierParams) -> TripleBarrierOutcome:
    if return_pct >= params.target_pct:
        return "target_hit"
    if return_pct <= -params.stop_pct:
        return "stop_hit"
    return "hold_expired"


def _meta_score(return_pct: float, predicted_return: float) -> float:
    """Meta-label in [0, 1].

    0.5 = breakeven, 1.0 = +5 % or more (hit target), 0.0 = −5 % or worse.
    Direction mismatch (predicted positive, got negative) is penalised by 30 %.
    """
    raw = min(1.0, max(0.0, 0.5 + return_pct * 10.0))
    # Penalise prediction direction error
    if predicted_return != 0.0 and (predicted_return * return_pct < 0):
        raw *= 0.70
    return round(raw, 4)


def _vol_expansion(entry_vol: float, exit_vol: float) -> float:
    """Ratio of exit-time volatility to entry-time volatility (1.0 = unchanged)."""
    if entry_vol > 0 and exit_vol > 0:
        return round(exit_vol / entry_vol, 4)
    return 1.0


# ── Main factory ──────────────────────────────────────────────────────────────

class OutcomeFactory:
    """Stateful factory that records entries and computes outcome labels on exit.

    Thread-safe via RLock (multiple fill callbacks may fire concurrently).
    Never blocks the critical order path — all heavy work is O(1).
    """

    DEFAULT_PARAMS = TripleBarrierParams()

    def __init__(self, params: TripleBarrierParams | None = None) -> None:
        self._params = params or self.DEFAULT_PARAMS
        self._pending: dict[str, _EntryRecord] = {}
        self._labels: collections.deque[OutcomeLabel] = collections.deque(maxlen=5000)
        self._lock = threading.RLock()

    # ── Entry registration ────────────────────────────────────────────────────

    def record_entry(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        *,
        predicted_return: float = 0.0,
        entry_vol: float = 0.0,
        expected_slippage_pct: float = 0.001,
        trace_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register an entry fill so we can compute the label on exit.

        Parameters
        ----------
        symbol:               instrument symbol
        side:                 "BUY" | "SELL"
        entry_price:          actual fill price
        predicted_return:     signal/neural forecast at entry time (proxy if unknown)
        entry_vol:            realised volatility at entry (e.g. stddev of last N returns)
        expected_slippage_pct: broker-estimated half-spread + impact cost
        trace_id:             trace id from DecisionTrace for linking
        metadata:             arbitrary strategy / regime context
        """
        if not symbol or entry_price <= 0:
            return
        with self._lock:
            self._pending[symbol] = _EntryRecord(
                symbol=symbol,
                side=side.upper(),
                entry_price=entry_price,
                predicted_return=predicted_return,
                entry_vol=entry_vol,
                expected_slippage_pct=expected_slippage_pct,
                trace_id=trace_id,
                metadata=metadata or {},
            )

    # ── Exit label computation ────────────────────────────────────────────────

    def compute_exit_label(
        self,
        symbol: str,
        exit_price: float,
        *,
        bars_held: int = 1,
        exit_vol: float = 0.0,
        realized_slippage_pct: float = 0.0,
        max_drawdown_pct: float = 0.0,
        trace_id: str = "",
        news_reaction_tag: str = "",
    ) -> OutcomeLabel | None:
        """Compute a complete OutcomeLabel when an exit fill arrives.

        Returns None if no matching entry record exists (e.g. double-exit).

        Parameters
        ----------
        symbol:                 instrument symbol
        exit_price:             actual fill price on exit
        bars_held:              number of bars the position was held
        exit_vol:               realised volatility at exit time
        realized_slippage_pct:  |fill_price − mid| / mid at exit
        max_drawdown_pct:       maximum adverse excursion during the trade
        trace_id:               overrides the stored trace_id if provided
        news_reaction_tag:      e.g. "earnings_beat", "macro_surprise"
        """
        with self._lock:
            rec = self._pending.pop(symbol, None)
        if rec is None:
            return None

        direction = 1 if rec.side == "BUY" else -1
        forward_return_pct = direction * (exit_price - rec.entry_price) / rec.entry_price
        slippage_surprise = realized_slippage_pct - rec.expected_slippage_pct
        prediction_error = abs(rec.predicted_return - forward_return_pct)

        label = OutcomeLabel(
            trace_id=trace_id or rec.trace_id,
            symbol=symbol,
            side=rec.side,
            barrier_outcome=_barrier_outcome(forward_return_pct, self._params),
            barrier_return_pct=forward_return_pct,
            bars_held=bars_held,
            forward_bucket=_forward_bucket(forward_return_pct),
            forward_return_pct=forward_return_pct,
            max_drawdown_pct=max_drawdown_pct,
            volatility_expansion=_vol_expansion(rec.entry_vol, exit_vol),
            expected_slippage_pct=rec.expected_slippage_pct,
            realized_slippage_pct=realized_slippage_pct,
            slippage_surprise=slippage_surprise,
            meta_label_score=_meta_score(forward_return_pct, rec.predicted_return),
            entry_price=rec.entry_price,
            exit_price=exit_price,
            predicted_return=rec.predicted_return,
            prediction_error=prediction_error,
            news_reaction_tag=news_reaction_tag,
            metadata=rec.metadata,
        )

        with self._lock:
            self._labels.append(label)
        return label

    # ── Inspection helpers ────────────────────────────────────────────────────

    def pending_symbols(self) -> list[str]:
        """Return symbols that have an open entry but no exit yet."""
        with self._lock:
            return list(self._pending.keys())

    def recent_labels(self, n: int = 50) -> list[dict]:
        """Return the most recent n labels as dicts (newest last)."""
        with self._lock:
            return [lbl.to_dict() for lbl in itertools.islice(reversed(self._labels), n)][::-1]

    def count(self) -> int:
        with self._lock:
            return len(self._labels)

    def barrier_distribution(self) -> dict[str, int]:
        """Tally of barrier outcomes across all labels so far."""
        dist: dict[str, int] = {"target_hit": 0, "stop_hit": 0, "hold_expired": 0, "unknown": 0}
        with self._lock:
            for lbl in self._labels:
                dist[lbl.barrier_outcome] = dist.get(lbl.barrier_outcome, 0) + 1
        return dist

    def bucket_distribution(self) -> dict[str, int]:
        """Tally of forward-return buckets."""
        dist: dict[str, int] = {}
        with self._lock:
            for lbl in self._labels:
                dist[lbl.forward_bucket] = dist.get(lbl.forward_bucket, 0) + 1
        return dist

    def average_meta_score(self) -> float:
        with self._lock:
            if not self._labels:
                return 0.5
            return sum(lbl.meta_label_score for lbl in self._labels) / len(self._labels)

    def slippage_surprise_mean(self) -> float:
        """Mean slippage surprise across all labels (positive = consistently worse than expected)."""
        with self._lock:
            if not self._labels:
                return 0.0
            return sum(lbl.slippage_surprise for lbl in self._labels) / len(self._labels)
