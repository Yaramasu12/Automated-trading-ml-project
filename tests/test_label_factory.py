"""Tests for trading_platform.trace.label_factory."""

from __future__ import annotations

import threading

import pytest

from trading_platform.trace.label_factory import (
    OutcomeFactory,
    TripleBarrierParams,
    _barrier_outcome,
    _forward_bucket,
    _meta_score,
    _vol_expansion,
)


# ── Pure helper tests ─────────────────────────────────────────────────────────

class TestForwardBucket:
    def test_strong_buy(self):
        assert _forward_bucket(0.04) == "strong_buy"
        assert _forward_bucket(0.03) == "strong_buy"

    def test_buy(self):
        assert _forward_bucket(0.02) == "buy"
        assert _forward_bucket(0.01) == "buy"

    def test_hold_positive(self):
        assert _forward_bucket(0.005) == "hold"

    def test_hold_zero(self):
        assert _forward_bucket(0.0) == "hold"

    def test_hold_small_negative(self):
        assert _forward_bucket(-0.005) == "hold"

    def test_sell(self):
        assert _forward_bucket(-0.011) == "sell"
        assert _forward_bucket(-0.025) == "sell"

    def test_strong_sell(self):
        assert _forward_bucket(-0.031) == "strong_sell"
        assert _forward_bucket(-0.10) == "strong_sell"


class TestBarrierOutcome:
    PARAMS = TripleBarrierParams(target_pct=0.03, stop_pct=0.015)

    def test_target_hit(self):
        assert _barrier_outcome(0.03, self.PARAMS) == "target_hit"
        assert _barrier_outcome(0.05, self.PARAMS) == "target_hit"

    def test_stop_hit(self):
        assert _barrier_outcome(-0.015, self.PARAMS) == "stop_hit"
        assert _barrier_outcome(-0.05, self.PARAMS) == "stop_hit"

    def test_hold_expired(self):
        assert _barrier_outcome(0.0, self.PARAMS) == "hold_expired"
        assert _barrier_outcome(0.01, self.PARAMS) == "hold_expired"
        assert _barrier_outcome(-0.01, self.PARAMS) == "hold_expired"


class TestMetaScore:
    def test_perfect_prediction_win(self):
        score = _meta_score(0.05, 0.03)
        assert score == 1.0

    def test_breakeven(self):
        score = _meta_score(0.0, 0.0)
        assert score == pytest.approx(0.5, abs=0.01)

    def test_total_loss(self):
        score = _meta_score(-0.10, 0.0)
        assert score == 0.0

    def test_direction_mismatch_penalty(self):
        # Predicted positive, got negative — should be penalised
        score_mismatch = _meta_score(-0.02, 0.03)
        score_match = _meta_score(-0.02, -0.03)
        assert score_mismatch < score_match

    def test_direction_mismatch_factor(self):
        # 30% penalty for direction mismatch
        base = _meta_score(-0.02, 0.0)
        penalised = _meta_score(-0.02, 0.03)
        assert penalised == pytest.approx(base * 0.70, rel=0.01)

    def test_score_in_range(self):
        for pct in [-0.10, -0.05, -0.01, 0.0, 0.01, 0.05, 0.10]:
            s = _meta_score(pct, 0.0)
            assert 0.0 <= s <= 1.0


class TestVolExpansion:
    def test_unchanged(self):
        assert _vol_expansion(0.01, 0.01) == 1.0

    def test_expansion(self):
        assert _vol_expansion(0.01, 0.02) == pytest.approx(2.0, rel=0.01)

    def test_contraction(self):
        assert _vol_expansion(0.02, 0.01) == pytest.approx(0.5, rel=0.01)

    def test_zero_entry_vol(self):
        assert _vol_expansion(0.0, 0.01) == 1.0

    def test_zero_exit_vol(self):
        assert _vol_expansion(0.01, 0.0) == 1.0


# ── OutcomeFactory integration tests ─────────────────────────────────────────

class TestOutcomeFactoryRoundTrip:
    def test_buy_profit(self):
        factory = OutcomeFactory()
        factory.record_entry("NIFTY", "BUY", 22000.0, predicted_return=0.03,
                             entry_vol=0.01, expected_slippage_pct=0.001, trace_id="t1")
        label = factory.compute_exit_label("NIFTY", 22700.0, bars_held=5,
                                           exit_vol=0.012, realized_slippage_pct=0.0012)
        assert label is not None
        assert label.symbol == "NIFTY"
        assert label.side == "BUY"
        assert label.forward_return_pct == pytest.approx(700 / 22000, rel=0.001)
        assert label.barrier_outcome == "target_hit"
        assert label.forward_bucket == "strong_buy"
        assert label.bars_held == 5
        assert label.trace_id == "t1"

    def test_buy_loss(self):
        factory = OutcomeFactory()
        factory.record_entry("BANKNIFTY", "BUY", 48000.0, predicted_return=0.01,
                             expected_slippage_pct=0.001, trace_id="t2")
        label = factory.compute_exit_label("BANKNIFTY", 47000.0, bars_held=10,
                                           realized_slippage_pct=0.002)
        assert label is not None
        assert label.forward_return_pct < 0
        assert label.barrier_outcome == "stop_hit"

    def test_sell_profit(self):
        factory = OutcomeFactory()
        factory.record_entry("RELIANCE", "SELL", 2800.0, trace_id="t3")
        label = factory.compute_exit_label("RELIANCE", 2750.0, bars_held=3)
        assert label is not None
        # SELL profits when price drops
        assert label.forward_return_pct > 0
        assert label.side == "SELL"

    def test_missing_entry_returns_none(self):
        factory = OutcomeFactory()
        label = factory.compute_exit_label("UNKNOWN", 100.0)
        assert label is None

    def test_double_exit_returns_none(self):
        factory = OutcomeFactory()
        factory.record_entry("HDFC", "BUY", 1500.0, trace_id="t4")
        factory.compute_exit_label("HDFC", 1550.0)
        label2 = factory.compute_exit_label("HDFC", 1560.0)
        assert label2 is None

    def test_slippage_surprise(self):
        factory = OutcomeFactory()
        factory.record_entry("TCS", "BUY", 3500.0, expected_slippage_pct=0.001)
        label = factory.compute_exit_label("TCS", 3550.0, realized_slippage_pct=0.003)
        assert label is not None
        assert label.slippage_surprise == pytest.approx(0.002, abs=1e-9)

    def test_news_reaction_tag(self):
        factory = OutcomeFactory()
        factory.record_entry("INFY", "BUY", 1600.0)
        label = factory.compute_exit_label("INFY", 1650.0, news_reaction_tag="earnings_beat")
        assert label is not None
        assert label.news_reaction_tag == "earnings_beat"

    def test_trace_id_override(self):
        factory = OutcomeFactory()
        factory.record_entry("WIPRO", "BUY", 400.0, trace_id="original")
        label = factory.compute_exit_label("WIPRO", 420.0, trace_id="override")
        assert label is not None
        assert label.trace_id == "override"

    def test_to_dict_round_trip(self):
        factory = OutcomeFactory()
        factory.record_entry("NIFTY", "BUY", 22000.0, predicted_return=0.02, trace_id="td1")
        label = factory.compute_exit_label("NIFTY", 22440.0)
        assert label is not None
        d = label.to_dict()
        assert d["symbol"] == "NIFTY"
        assert d["side"] == "BUY"
        assert "barrier_outcome" in d
        assert "forward_bucket" in d
        assert "meta_label_score" in d
        assert "ts" in d

    def test_invalid_entry_skipped(self):
        factory = OutcomeFactory()
        factory.record_entry("", "BUY", 100.0)  # empty symbol
        factory.record_entry("SYM", "BUY", 0.0)  # zero price
        assert factory.pending_symbols() == []


class TestOutcomeFactoryInspection:
    def _fill_factory(self, n: int = 5) -> OutcomeFactory:
        factory = OutcomeFactory()
        prices = [22000, 22700, 21700, 22200, 22050, 22300]
        for i in range(n):
            sym = f"SYM{i}"
            factory.record_entry(sym, "BUY", prices[i], trace_id=f"t{i}")
            factory.compute_exit_label(sym, prices[i + 1])
        return factory

    def test_count(self):
        factory = self._fill_factory(4)
        assert factory.count() == 4

    def test_pending_symbols_empty_after_all_exits(self):
        factory = self._fill_factory(3)
        assert factory.pending_symbols() == []

    def test_pending_symbols_before_exit(self):
        factory = OutcomeFactory()
        factory.record_entry("AAA", "BUY", 100.0)
        factory.record_entry("BBB", "BUY", 200.0)
        assert set(factory.pending_symbols()) == {"AAA", "BBB"}

    def test_barrier_distribution_keys(self):
        factory = self._fill_factory(4)
        dist = factory.barrier_distribution()
        assert set(dist.keys()) >= {"target_hit", "stop_hit", "hold_expired", "unknown"}

    def test_bucket_distribution_sum(self):
        factory = self._fill_factory(4)
        dist = factory.bucket_distribution()
        assert sum(dist.values()) == 4

    def test_average_meta_score_range(self):
        factory = self._fill_factory(5)
        score = factory.average_meta_score()
        assert 0.0 <= score <= 1.0

    def test_average_meta_score_empty(self):
        factory = OutcomeFactory()
        assert factory.average_meta_score() == 0.5

    def test_slippage_surprise_mean(self):
        factory = OutcomeFactory()
        factory.record_entry("A", "BUY", 100.0, expected_slippage_pct=0.001)
        factory.compute_exit_label("A", 103.0, realized_slippage_pct=0.003)
        factory.record_entry("B", "BUY", 200.0, expected_slippage_pct=0.001)
        factory.compute_exit_label("B", 206.0, realized_slippage_pct=0.001)
        mean = factory.slippage_surprise_mean()
        assert mean == pytest.approx(0.001, abs=1e-9)

    def test_slippage_surprise_empty(self):
        factory = OutcomeFactory()
        assert factory.slippage_surprise_mean() == 0.0

    def test_recent_labels_limit(self):
        factory = self._fill_factory(5)
        recent = factory.recent_labels(n=3)
        assert len(recent) == 3

    def test_recent_labels_are_dicts(self):
        factory = self._fill_factory(2)
        for item in factory.recent_labels():
            assert isinstance(item, dict)
            assert "symbol" in item


class TestOutcomeFactoryThreadSafety:
    def test_concurrent_entries_and_exits(self):
        factory = OutcomeFactory()
        errors = []

        def worker(i: int) -> None:
            try:
                sym = f"SYM{i}"
                factory.record_entry(sym, "BUY", 1000.0 + i, trace_id=f"t{i}")
                factory.compute_exit_label(sym, 1010.0 + i)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert factory.count() == 50
