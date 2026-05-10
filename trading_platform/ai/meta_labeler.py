from __future__ import annotations

"""MetaLabeler — champion/challenger strategy promotion by regime.

Combines two signal sources per trade:
  1. Realized outcome quality  (OutcomeLabel.meta_label_score, triple-barrier derived)
  2. Neural prediction quality (did the neural model's direction call agree with reality?)

Combined score formula
----------------------
combined = (1 - NEURAL_WEIGHT) * realized_quality + NEURAL_WEIGHT * neural_quality

Where:
  realized_quality = label.meta_label_score              (0-1 from OutcomeFactory)
  neural_quality   = 1.0 if neural direction == actual else 0.0
  NEURAL_WEIGHT    = 0.30  (tunable at construction time)

Champion/Challenger promotion rules
------------------------------------
  CHALLENGER:  default status for every new (strategy, regime) pair.
  CHAMPION:    after MIN_OBSERVATIONS fills with EMA combined score >= PROMOTION_THRESHOLD.
  DEMOTED:     champion falls below DEMOTION_THRESHOLD; re-qualifies via CHALLENGER again.

Thread safety: all state mutations use threading.RLock.

Integration
-----------
runtime._on_fill (exit path):
    label = outcome_factory.compute_exit_label(...)
    if label:
        meta_labeler.record_outcome(
            strategy_name=strat,
            regime=regime,
            label=label,
            neural_direction_probability=ctx.get("neural_direction_probability", 0.5),
        )

Signal router:
    champions = meta_labeler.champion_strategies(current_regime)
    # prefer champion strategies when multiple strategies signal the same direction
"""

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trading_platform.trace.label_factory import OutcomeLabel

logger = logging.getLogger(__name__)

# ── Default thresholds ────────────────────────────────────────────────────────

_MIN_OBS_FOR_PROMOTION: int = 10
_PROMOTION_THRESHOLD: float = 0.60
_DEMOTION_THRESHOLD: float = 0.45
_EMA_ALPHA: float = 0.25
_NEURAL_WEIGHT: float = 0.30

CHAMPION = "champion"
CHALLENGER = "challenger"
DEMOTED = "demoted"


# ── Record dataclass ──────────────────────────────────────────────────────────

@dataclass
class ChampionRecord:
    """Per (strategy, regime) tracking record for champion/challenger state."""
    strategy_name: str
    regime: str
    status: str              # champion | challenger | demoted
    ema_combined_score: float
    observation_count: int
    promoted_at: datetime | None = None
    demoted_at: datetime | None = None
    last_updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "strategy_name": self.strategy_name,
            "regime": self.regime,
            "status": self.status,
            "ema_combined_score": round(self.ema_combined_score, 4),
            "observation_count": self.observation_count,
            "promoted_at": self.promoted_at.isoformat() if self.promoted_at else None,
            "demoted_at": self.demoted_at.isoformat() if self.demoted_at else None,
            "last_updated_at": self.last_updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> ChampionRecord:
        def _parse_dt(v: str | None) -> datetime | None:
            return datetime.fromisoformat(v) if v else None
        return cls(
            strategy_name=d["strategy_name"],
            regime=d["regime"],
            status=d["status"],
            ema_combined_score=float(d["ema_combined_score"]),
            observation_count=int(d["observation_count"]),
            promoted_at=_parse_dt(d.get("promoted_at")),
            demoted_at=_parse_dt(d.get("demoted_at")),
            last_updated_at=_parse_dt(d.get("last_updated_at")) or datetime.now(timezone.utc),
        )


# ── Main class ────────────────────────────────────────────────────────────────

class MetaLabeler:
    """Tracks per-strategy, per-regime performance and manages champion/challenger state.

    Designed to be called from the fill path — all operations are O(1) and
    thread-safe.  Never blocks, never calls the broker layer.
    """

    def __init__(
        self,
        min_obs_for_promotion: int = _MIN_OBS_FOR_PROMOTION,
        promotion_threshold: float = _PROMOTION_THRESHOLD,
        demotion_threshold: float = _DEMOTION_THRESHOLD,
        ema_alpha: float = _EMA_ALPHA,
        neural_weight: float = _NEURAL_WEIGHT,
    ) -> None:
        self._min_obs = min_obs_for_promotion
        self._promo_thresh = promotion_threshold
        self._demo_thresh = demotion_threshold
        self._alpha = ema_alpha
        self._neural_w = neural_weight

        # (regime, strategy) -> ChampionRecord
        self._records: dict[tuple[str, str], ChampionRecord] = {}
        self._lock = threading.RLock()

    # ── Core update ───────────────────────────────────────────────────────────

    def record_outcome(
        self,
        strategy_name: str,
        regime: str,
        label: OutcomeLabel,
        neural_direction_probability: float = 0.5,
    ) -> ChampionRecord:
        """Process one realized outcome and update champion/challenger state.

        Parameters
        ----------
        strategy_name:              name of the originating strategy (e.g. "trend_momentum")
        regime:                     regime at trade entry (e.g. "TRENDING")
        label:                      OutcomeLabel from OutcomeFactory.compute_exit_label()
        neural_direction_probability: P(up) from NeuralPredictionBundle at entry time; 0.5 = neutral
        """
        if not strategy_name or not regime:
            raise ValueError("strategy_name and regime must be non-empty strings")

        realized_quality = float(label.meta_label_score)

        # Neural quality: 1.0 if the neural direction call agreed with actual direction
        actual_positive = label.forward_return_pct >= 0.0
        neural_predicts_up = neural_direction_probability >= 0.5
        neural_quality = 1.0 if (actual_positive == neural_predicts_up) else 0.0

        combined = (1.0 - self._neural_w) * realized_quality + self._neural_w * neural_quality

        key = (regime, strategy_name)
        now = datetime.now(timezone.utc)

        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                rec = ChampionRecord(
                    strategy_name=strategy_name,
                    regime=regime,
                    status=CHALLENGER,
                    ema_combined_score=combined,
                    observation_count=1,
                    last_updated_at=now,
                )
            else:
                rec.ema_combined_score = (
                    self._alpha * combined + (1.0 - self._alpha) * rec.ema_combined_score
                )
                rec.observation_count += 1
                rec.last_updated_at = now

            # Promotion / demotion logic
            if rec.observation_count >= self._min_obs:
                if rec.status == CHALLENGER and rec.ema_combined_score >= self._promo_thresh:
                    rec.status = CHAMPION
                    rec.promoted_at = now
                    logger.info(
                        "MetaLabeler: PROMOTED %s in %s (score=%.3f after %d obs)",
                        strategy_name, regime, rec.ema_combined_score, rec.observation_count,
                    )
                elif rec.status == DEMOTED and rec.ema_combined_score >= self._promo_thresh:
                    # Re-qualify: step back through challenger first
                    rec.status = CHALLENGER
                    logger.info(
                        "MetaLabeler: %s/%s re-qualified as CHALLENGER (score=%.3f)",
                        strategy_name, regime, rec.ema_combined_score,
                    )
                elif rec.status == CHAMPION and rec.ema_combined_score < self._demo_thresh:
                    rec.status = DEMOTED
                    rec.demoted_at = now
                    logger.warning(
                        "MetaLabeler: DEMOTED %s in %s (score=%.3f)",
                        strategy_name, regime, rec.ema_combined_score,
                    )

            self._records[key] = rec
            return rec

    # ── Query helpers ─────────────────────────────────────────────────────────

    def champion_strategies(self, regime: str) -> list[str]:
        """Return strategy names with CHAMPION status in this regime, sorted by score."""
        with self._lock:
            champions = [
                rec for (r, _), rec in self._records.items()
                if r == regime and rec.status == CHAMPION
            ]
        champions.sort(key=lambda r: r.ema_combined_score, reverse=True)
        return [rec.strategy_name for rec in champions]

    def strategy_status(self, strategy_name: str, regime: str) -> str:
        """Return champion | challenger | demoted | unknown."""
        with self._lock:
            rec = self._records.get((regime, strategy_name))
        return rec.status if rec else "unknown"

    def record(self, strategy_name: str, regime: str) -> ChampionRecord | None:
        with self._lock:
            return self._records.get((regime, strategy_name))

    def all_records(self) -> list[dict]:
        with self._lock:
            return [rec.to_dict() for rec in self._records.values()]

    def champions_by_regime(self) -> dict[str, list[str]]:
        """Return {regime: [champion_strategy_names]} mapping."""
        result: dict[str, list[str]] = {}
        with self._lock:
            for (regime, _), rec in self._records.items():
                if rec.status == CHAMPION:
                    result.setdefault(regime, []).append(rec.strategy_name)
        return result

    def summary(self) -> dict[str, Any]:
        """Serialisable summary for API and monitoring endpoints."""
        with self._lock:
            records = list(self._records.values())
        by_regime: dict[str, dict[str, Any]] = {}
        for rec in records:
            by_regime.setdefault(rec.regime, {})[rec.strategy_name] = {
                "status": rec.status,
                "score": round(rec.ema_combined_score, 4),
                "obs": rec.observation_count,
            }
        return {
            "total_records": len(records),
            "champion_count": sum(1 for r in records if r.status == CHAMPION),
            "challenger_count": sum(1 for r in records if r.status == CHALLENGER),
            "demoted_count": sum(1 for r in records if r.status == DEMOTED),
            "by_regime": by_regime,
            "thresholds": {
                "min_obs": self._min_obs,
                "promotion": self._promo_thresh,
                "demotion": self._demo_thresh,
                "ema_alpha": self._alpha,
                "neural_weight": self._neural_w,
            },
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: str) -> None:
        """Persist all records to a JSON file."""
        with self._lock:
            data = [rec.to_dict() for rec in self._records.values()]
        with open(path, "w") as fh:
            json.dump(data, fh)

    def load(self, path: str) -> bool:
        """Load records from a JSON file. Returns True if successful."""
        try:
            with open(path) as fh:
                data = json.load(fh)
            loaded: dict[tuple[str, str], ChampionRecord] = {}
            for item in data:
                rec = ChampionRecord.from_dict(item)
                loaded[(rec.regime, rec.strategy_name)] = rec
            with self._lock:
                self._records = loaded
            logger.info("MetaLabeler: loaded %d records from %s", len(loaded), path)
            return True
        except FileNotFoundError:
            return False
        except Exception as exc:
            logger.warning("MetaLabeler: load error from %s: %s", path, exc)
            return False
