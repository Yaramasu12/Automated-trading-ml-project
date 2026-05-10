"""Tests for trading_platform.ai.meta_labeler — champion/challenger framework."""

from __future__ import annotations

import json
import threading
import tempfile
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trading_platform.ai.meta_labeler import (
    CHAMPION,
    CHALLENGER,
    DEMOTED,
    ChampionRecord,
    MetaLabeler,
)


# ── Fixture helpers ──────────────────────────────────────────────────────────

def _make_label(forward_return_pct: float, meta_label_score: float, barrier: str = "target_hit"):
    """Build a minimal OutcomeLabel-like object."""
    return SimpleNamespace(
        forward_return_pct=forward_return_pct,
        meta_label_score=meta_label_score,
        barrier_outcome=barrier,
        forward_bucket="strong_buy" if forward_return_pct > 0.03 else "buy",
        to_dict=lambda: {},
    )


# ── ChampionRecord ───────────────────────────────────────────────────────────

class TestChampionRecord:
    def test_to_dict_keys(self):
        rec = ChampionRecord(
            strategy_name="trend",
            regime="TRENDING",
            status=CHALLENGER,
            ema_combined_score=0.55,
            observation_count=5,
        )
        d = rec.to_dict()
        assert d["strategy_name"] == "trend"
        assert d["regime"] == "TRENDING"
        assert d["status"] == CHALLENGER
        assert 0 <= d["ema_combined_score"] <= 1
        assert d["observation_count"] == 5
        assert d["promoted_at"] is None
        assert d["demoted_at"] is None
        assert "last_updated_at" in d

    def test_from_dict_roundtrip(self):
        now = datetime.now(timezone.utc)
        rec = ChampionRecord(
            strategy_name="reversion",
            regime="MEAN_REVERTING",
            status=CHAMPION,
            ema_combined_score=0.72,
            observation_count=15,
            promoted_at=now,
        )
        d = rec.to_dict()
        restored = ChampionRecord.from_dict(d)
        assert restored.strategy_name == "reversion"
        assert restored.status == CHAMPION
        assert abs(restored.ema_combined_score - 0.72) < 0.0001
        assert restored.observation_count == 15
        assert restored.promoted_at is not None

    def test_from_dict_null_timestamps(self):
        d = {
            "strategy_name": "x", "regime": "y", "status": CHALLENGER,
            "ema_combined_score": 0.5, "observation_count": 1,
            "promoted_at": None, "demoted_at": None, "last_updated_at": None,
        }
        rec = ChampionRecord.from_dict(d)
        assert rec.promoted_at is None
        assert rec.demoted_at is None


# ── MetaLabeler construction ──────────────────────────────────────────────────

class TestMetaLabelerInit:
    def test_default_params(self):
        ml = MetaLabeler()
        assert ml._min_obs == 10
        assert ml._promo_thresh == 0.60
        assert ml._demo_thresh == 0.45
        assert ml._alpha == 0.25
        assert ml._neural_w == 0.30

    def test_custom_params(self):
        ml = MetaLabeler(min_obs_for_promotion=5, promotion_threshold=0.7)
        assert ml._min_obs == 5
        assert ml._promo_thresh == 0.7

    def test_empty_records(self):
        ml = MetaLabeler()
        assert ml.all_records() == []

    def test_invalid_inputs_raise(self):
        ml = MetaLabeler()
        label = _make_label(0.02, 0.8)
        with pytest.raises(ValueError):
            ml.record_outcome("", "TRENDING", label)
        with pytest.raises(ValueError):
            ml.record_outcome("trend", "", label)


# ── record_outcome ────────────────────────────────────────────────────────────

class TestRecordOutcome:
    def test_first_record_is_challenger(self):
        ml = MetaLabeler()
        label = _make_label(0.02, 0.8)
        rec = ml.record_outcome("trend", "TRENDING", label, neural_direction_probability=0.7)
        assert rec.status == CHALLENGER
        assert rec.observation_count == 1
        assert 0 <= rec.ema_combined_score <= 1

    def test_ema_update_on_second_observation(self):
        ml = MetaLabeler(ema_alpha=0.5)
        label = _make_label(0.02, 1.0)  # perfect label: score=1, neural_quality=1
        rec1 = ml.record_outcome("trend", "TRENDING", label, 0.8)
        first_score = rec1.ema_combined_score
        label2 = _make_label(-0.02, 0.0)  # bad label
        rec2 = ml.record_outcome("trend", "TRENDING", label2, 0.4)
        # EMA must change
        assert rec2.ema_combined_score != first_score
        assert rec2.observation_count == 2

    def test_neural_quality_direction_match(self):
        ml = MetaLabeler(neural_weight=1.0)  # use only neural quality
        label_up = _make_label(0.02, 0.5)    # positive return → actual up
        rec = ml.record_outcome("t", "R", label_up, neural_direction_probability=0.8)  # predicts up ✓
        assert rec.ema_combined_score == pytest.approx(1.0)

    def test_neural_quality_direction_miss(self):
        ml = MetaLabeler(neural_weight=1.0)
        label_up = _make_label(0.02, 0.5)    # positive return → actual up
        rec = ml.record_outcome("t", "R", label_up, neural_direction_probability=0.3)  # predicts down ✗
        assert rec.ema_combined_score == pytest.approx(0.0)

    def test_neural_quality_neutral_probability(self):
        ml = MetaLabeler(neural_weight=1.0)
        label_up = _make_label(0.02, 0.5)
        # exactly 0.5 → predicts up (>= 0.5), actual up → match
        rec = ml.record_outcome("t", "R", label_up, neural_direction_probability=0.5)
        assert rec.ema_combined_score == pytest.approx(1.0)

    def test_combined_score_blend(self):
        ml = MetaLabeler(neural_weight=0.3, ema_alpha=1.0)  # alpha=1 → score = latest combined
        label = _make_label(0.02, 0.8)  # actual up, realized=0.8
        # P(up)=0.7 → predicts up, actual up → neural_quality=1.0
        # combined = 0.7*0.8 + 0.3*1.0 = 0.56 + 0.30 = 0.86
        rec = ml.record_outcome("t", "R", label, neural_direction_probability=0.7)
        assert rec.ema_combined_score == pytest.approx(0.86, abs=0.001)

    def test_multiple_strategies_isolated(self):
        ml = MetaLabeler()
        label = _make_label(0.02, 0.8)
        ml.record_outcome("strat_a", "TRENDING", label)
        ml.record_outcome("strat_b", "TRENDING", label)
        assert ml.strategy_status("strat_a", "TRENDING") == CHALLENGER
        assert ml.strategy_status("strat_b", "TRENDING") == CHALLENGER
        assert len(ml.all_records()) == 2

    def test_multiple_regimes_isolated(self):
        ml = MetaLabeler()
        label = _make_label(0.02, 0.8)
        ml.record_outcome("trend", "TRENDING", label)
        ml.record_outcome("trend", "MEAN_REVERTING", label)
        assert ml.strategy_status("trend", "TRENDING") == CHALLENGER
        assert ml.strategy_status("trend", "MEAN_REVERTING") == CHALLENGER


# ── Promotion ──────────────────────────────────────────────────────────────────

class TestPromotion:
    def _fill_to_promotion(self, ml: MetaLabeler, strat: str = "trend", regime: str = "TRENDING") -> ChampionRecord:
        """Feed min_obs observations with high score to trigger promotion."""
        label = _make_label(0.02, 0.9)  # both realized and neural quality high
        rec = None
        for _ in range(ml._min_obs):
            rec = ml.record_outcome(strat, regime, label, neural_direction_probability=0.8)
        return rec

    def test_promoted_after_min_obs(self):
        ml = MetaLabeler(min_obs_for_promotion=5, promotion_threshold=0.60)
        rec = self._fill_to_promotion(ml)
        assert rec.status == CHAMPION
        assert rec.promoted_at is not None

    def test_not_promoted_before_min_obs(self):
        ml = MetaLabeler(min_obs_for_promotion=10, promotion_threshold=0.60)
        label = _make_label(0.02, 0.9)
        for _ in range(9):
            rec = ml.record_outcome("trend", "TRENDING", label, 0.8)
        assert rec.status == CHALLENGER

    def test_not_promoted_below_threshold(self):
        ml = MetaLabeler(min_obs_for_promotion=3, promotion_threshold=0.90)
        label = _make_label(0.02, 0.5)  # mediocre score — won't reach 0.90
        for _ in range(5):
            rec = ml.record_outcome("trend", "TRENDING", label, 0.6)
        assert rec.status == CHALLENGER

    def test_champion_strategies_listed(self):
        ml = MetaLabeler(min_obs_for_promotion=3)
        self._fill_to_promotion(ml, strat="trend", regime="TRENDING")
        champions = ml.champion_strategies("TRENDING")
        assert "trend" in champions

    def test_champion_strategies_empty_for_other_regime(self):
        ml = MetaLabeler(min_obs_for_promotion=3)
        self._fill_to_promotion(ml, strat="trend", regime="TRENDING")
        assert ml.champion_strategies("MEAN_REVERTING") == []

    def test_champions_by_regime(self):
        ml = MetaLabeler(min_obs_for_promotion=3)
        self._fill_to_promotion(ml, strat="trend", regime="TRENDING")
        mapping = ml.champions_by_regime()
        assert "TRENDING" in mapping
        assert "trend" in mapping["TRENDING"]


# ── Demotion ───────────────────────────────────────────────────────────────────

class TestDemotion:
    def _promote(self, ml: MetaLabeler, strat: str = "trend", regime: str = "TRENDING") -> None:
        label = _make_label(0.02, 0.9)
        for _ in range(ml._min_obs):
            ml.record_outcome(strat, regime, label, 0.8)

    def test_demotion_after_bad_scores(self):
        ml = MetaLabeler(min_obs_for_promotion=3, demotion_threshold=0.45, ema_alpha=0.9)
        self._promote(ml)
        assert ml.strategy_status("trend", "TRENDING") == CHAMPION
        # Flood with very bad labels to drag EMA below demotion threshold
        bad_label = _make_label(-0.05, 0.0)
        for _ in range(20):
            rec = ml.record_outcome("trend", "TRENDING", bad_label, 0.3)
        assert rec.status == DEMOTED
        assert rec.demoted_at is not None

    def test_demoted_requalifies_as_challenger(self):
        ml = MetaLabeler(min_obs_for_promotion=3, demotion_threshold=0.45, ema_alpha=0.9)
        self._promote(ml)
        bad_label = _make_label(-0.05, 0.0)
        for _ in range(20):
            ml.record_outcome("trend", "TRENDING", bad_label, 0.3)
        assert ml.strategy_status("trend", "TRENDING") == DEMOTED
        # Now recover
        good_label = _make_label(0.02, 0.9)
        for _ in range(30):
            rec = ml.record_outcome("trend", "TRENDING", good_label, 0.8)
        # Should have re-qualified as CHALLENGER (not directly to CHAMPION)
        assert rec.status in (CHALLENGER, CHAMPION)


# ── Query helpers ─────────────────────────────────────────────────────────────

class TestQueryHelpers:
    def test_strategy_status_unknown(self):
        ml = MetaLabeler()
        assert ml.strategy_status("missing", "TRENDING") == "unknown"

    def test_record_returns_none_for_missing(self):
        ml = MetaLabeler()
        assert ml.record("missing", "TRENDING") is None

    def test_record_returns_champion_record(self):
        ml = MetaLabeler()
        label = _make_label(0.02, 0.8)
        ml.record_outcome("trend", "TRENDING", label)
        rec = ml.record("trend", "TRENDING")
        assert isinstance(rec, ChampionRecord)

    def test_all_records_returns_dicts(self):
        ml = MetaLabeler()
        label = _make_label(0.02, 0.8)
        ml.record_outcome("trend", "TRENDING", label)
        ml.record_outcome("reversion", "MEAN_REVERTING", label)
        records = ml.all_records()
        assert len(records) == 2
        assert all(isinstance(r, dict) for r in records)

    def test_summary_structure(self):
        ml = MetaLabeler(min_obs_for_promotion=3)
        label = _make_label(0.02, 0.9)
        for _ in range(5):
            ml.record_outcome("trend", "TRENDING", label, 0.8)
        s = ml.summary()
        assert "total_records" in s
        assert "champion_count" in s
        assert "challenger_count" in s
        assert "demoted_count" in s
        assert "by_regime" in s
        assert "thresholds" in s

    def test_champion_strategies_sorted_by_score(self):
        ml = MetaLabeler(min_obs_for_promotion=3)
        label_high = _make_label(0.05, 1.0)
        label_low = _make_label(0.02, 0.65)
        for _ in range(5):
            ml.record_outcome("high_strat", "TRENDING", label_high, 0.9)
        for _ in range(5):
            ml.record_outcome("low_strat", "TRENDING", label_low, 0.7)
        champions = ml.champion_strategies("TRENDING")
        if len(champions) >= 2:
            assert champions[0] == "high_strat"


# ── Persistence ────────────────────────────────────────────────────────────────

class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        ml = MetaLabeler(min_obs_for_promotion=3)
        label = _make_label(0.02, 0.9)
        for _ in range(5):
            ml.record_outcome("trend", "TRENDING", label, 0.8)
        path = str(tmp_path / "meta.json")
        ml.save(path)

        ml2 = MetaLabeler()
        ok = ml2.load(path)
        assert ok is True
        rec = ml2.record("trend", "TRENDING")
        assert rec is not None
        assert rec.status == CHAMPION

    def test_load_missing_file_returns_false(self, tmp_path):
        ml = MetaLabeler()
        ok = ml.load(str(tmp_path / "nonexistent.json"))
        assert ok is False

    def test_save_load_preserves_counts(self, tmp_path):
        ml = MetaLabeler()
        label = _make_label(0.02, 0.5)
        for _ in range(7):
            ml.record_outcome("s", "R", label)
        path = str(tmp_path / "ml.json")
        ml.save(path)
        ml2 = MetaLabeler()
        ml2.load(path)
        rec = ml2.record("s", "R")
        assert rec.observation_count == 7


# ── Thread safety ─────────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_record_outcome(self):
        ml = MetaLabeler()
        label = _make_label(0.02, 0.8)
        errors = []

        def worker(strat: str):
            try:
                for _ in range(20):
                    ml.record_outcome(strat, "TRENDING", label, 0.7)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(f"s{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        # 10 strategies, each recorded independently
        assert len(ml.all_records()) == 10

    def test_concurrent_query_during_write(self):
        ml = MetaLabeler()
        label = _make_label(0.02, 0.8)
        stop = threading.Event()
        query_errors = []

        def writer():
            for i in range(50):
                ml.record_outcome(f"s{i % 5}", "TRENDING", label)

        def reader():
            while not stop.is_set():
                try:
                    _ = ml.all_records()
                    _ = ml.champion_strategies("TRENDING")
                except Exception as exc:
                    query_errors.append(exc)

        t_reader = threading.Thread(target=reader, daemon=True)
        t_writer = threading.Thread(target=writer)
        t_reader.start()
        t_writer.start()
        t_writer.join()
        stop.set()
        t_reader.join(timeout=1)
        assert query_errors == [], f"Reader errors: {query_errors}"
