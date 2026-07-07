"""Tests for GradientBoostedReturnForecaster — the validated direction model.

The whole point of this model is HONESTY: it must find edge when edge exists and
report NO edge on a random walk (so the live system never trades a fake signal).
"""
from __future__ import annotations

import numpy as np
import pytest

from trading_platform.neural.return_forecaster import GradientBoostedReturnForecaster


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
