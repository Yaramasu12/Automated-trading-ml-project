"""Tests for GradientBoostedReturnForecaster — the validated direction model.

The whole point of this model is HONESTY: it must find edge when edge exists and
report NO edge on a random walk (so the live system never trades a fake signal).
"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from trading_platform.neural.return_forecaster import (
    GradientBoostedReturnForecaster,
    _walk_forward_metrics,
)


def _bars_from_returns(returns: np.ndarray, base: float = 1000.0) -> list[dict]:
    closes = base * np.cumprod(1.0 + returns)
    bars = []
    prev = base
    for c in closes:
        high = max(prev, c) * 1.001
        low = min(prev, c) * 0.999
        bars.append({"open": prev, "high": high, "low": low, "close": float(c), "volume": 500_000})
        prev = c
    return bars


def _autocorrelated_series(n: int, phi: float = 0.6, seed: int = 1) -> list[dict]:
    """Returns with positive AR(1) autocorrelation → momentum is genuinely predictable.

    phi=0.6 keeps the OOS AUC comfortably above the statistical acceptance
    threshold (0.5 + 2·SE_null ≈ 0.564 at this sample size) across seeds, so the
    test asserts the gate mechanism rather than a borderline draw."""
    rng = np.random.default_rng(seed)
    r = np.zeros(n)
    for t in range(1, n):
        r[t] = phi * r[t - 1] + rng.normal(0, 0.01)
    return _bars_from_returns(r)


def _random_walk(n: int, seed: int = 2) -> list[dict]:
    """Driftless iid returns → unpredictable by construction."""
    rng = np.random.default_rng(seed)
    return _bars_from_returns(rng.normal(0, 0.012, n))


def test_accepts_real_edge():
    f = GradientBoostedReturnForecaster()
    accepted = f.train(_autocorrelated_series(800), n_splits=5)
    assert accepted is True
    assert f.is_available()
    m = f.last_train_metrics
    assert m["oos_auc"] > 0.52
    assert m["accepted"] is True


def test_rejects_random_walk():
    f = GradientBoostedReturnForecaster()
    accepted = f.train(_random_walk(800), n_splits=5)
    assert accepted is False
    assert not f.is_available()
    # On noise the OOS AUC must sit near 0.5 — no fabricated edge.
    assert f.last_train_metrics["oos_auc"] < 0.55


def test_predict_none_when_not_trained():
    f = GradientBoostedReturnForecaster()
    assert f.predict("NIFTY", _random_walk(100)) is None


def test_predict_shape_when_trained():
    f = GradientBoostedReturnForecaster()
    assert f.train(_autocorrelated_series(800)) is True
    pred = f.predict("NIFTY", _autocorrelated_series(60, seed=9))
    assert pred is not None
    assert 0.05 <= pred.direction_probability <= 0.95
    assert 0.0 <= pred.model_uncertainty <= 1.0
    assert pred.model_id == "gbm_direction_v1"


class _RecordingModel:
    """Fake sklearn-compatible model that records the training-set size it
    was fit on, so the purge boundary can be checked exactly without
    depending on stochastic classifier behavior."""

    fit_sizes: list[int] = []

    def fit(self, X, y):
        _RecordingModel.fit_sizes.append(len(X))
        self.classes_ = np.array(sorted(set(y.tolist())))
        return self

    def predict_proba(self, X):
        # Deterministic 50/50 output — irrelevant to what this test checks.
        return np.full((len(X), len(self.classes_)), 1.0 / max(len(self.classes_), 1))


def test_walk_forward_embargo_purges_the_contaminated_training_tail():
    """Regression 2026-09-14 (Section 21H audit): each row's label looks
    `horizon` bars ahead (see _build_rows), so without a purge the last
    `horizon` training rows before a fold boundary peek across it into the
    test block — a real overlapping-label leak. embargo=N must drop
    exactly the last N rows from every fold's training slice."""
    n = 400
    rng = np.random.default_rng(0)
    X = rng.normal(size=(n, 3))
    y = rng.integers(0, 2, size=n)
    n_splits = 5
    fold = n // (n_splits + 1)
    embargo = 10

    _RecordingModel.fit_sizes = []
    with patch(
        "trading_platform.neural.return_forecaster._make_model",
        return_value=_RecordingModel(),
    ):
        _walk_forward_metrics(X, y, n_splits, random_state=42, embargo=embargo)

    assert _RecordingModel.fit_sizes  # at least one fold actually ran
    for k, observed_size in enumerate(_RecordingModel.fit_sizes, start=1):
        train_end = fold * k
        assert observed_size == train_end - embargo

    # embargo=0 must reproduce the original (unpurged) boundary exactly.
    _RecordingModel.fit_sizes = []
    with patch(
        "trading_platform.neural.return_forecaster._make_model",
        return_value=_RecordingModel(),
    ):
        _walk_forward_metrics(X, y, n_splits, random_state=42, embargo=0)
    for k, observed_size in enumerate(_RecordingModel.fit_sizes, start=1):
        assert observed_size == fold * k


def test_save_load_roundtrip(tmp_path):
    f = GradientBoostedReturnForecaster()
    assert f.train(_autocorrelated_series(800)) is True
    path = str(tmp_path / "rf")
    f.save(path)
    g = GradientBoostedReturnForecaster()
    assert g.load(path) is True
    assert g.is_available()
    # A rejected model must NOT load as available.
    bad = GradientBoostedReturnForecaster()
    bad.train(_random_walk(800))
    bad_path = str(tmp_path / "bad")
    bad.save(bad_path)
    h = GradientBoostedReturnForecaster()
    assert h.load(bad_path) is False
    assert not h.is_available()
