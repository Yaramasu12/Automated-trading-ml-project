"""Tests for IntradayReturnForecaster — the validated intraday direction model.

Same honesty contract as the daily forecaster: it must find edge when genuine
within-day momentum exists and report NO edge on a driftless random walk, so the
live system never trades a fabricated intraday signal. It must also refuse to
predict from the wrong data frequency (daily bars → None).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from trading_platform.neural.intraday_forecaster import IntradayReturnForecaster

BARS_PER_DAY = 75   # ~09:15–15:30 in 5-min bars


def _intraday_bars(day_return_fn, n_days: int, base: float = 20000.0, seed: int = 0) -> list[dict]:
    """Build 5-min bars across n_days trading days. `day_return_fn(rng)` yields one
    day's per-bar returns array (length BARS_PER_DAY). Features reset each day."""
    rng = np.random.default_rng(seed)
    bars: list[dict] = []
    day0 = datetime(2026, 1, 5, 9, 15)   # a Monday
    made = 0
    d = 0
    while made < n_days:
        day = day0 + timedelta(days=d)
        d += 1
        if day.weekday() >= 5:
            continue
        made += 1
        rets = day_return_fn(rng)
        price = base
        t = day
        for r in rets:
            o = price
            price = max(price * (1 + r), 1e-3)
            hi = max(o, price) * 1.0005
            lo = min(o, price) * 0.9995
            bars.append({"ts": t, "open": o, "high": hi, "low": lo,
                         "close": float(price), "volume": float(rng.integers(1000, 5000))})
            t += timedelta(minutes=5)
    return bars


def _momentum_day(rng, phi: float = 0.7):
    """Positive AR(1) intraday returns → within-day momentum is predictable."""
    r = np.zeros(BARS_PER_DAY)
    for t in range(1, BARS_PER_DAY):
        r[t] = phi * r[t - 1] + rng.normal(0, 0.0008)
    return r


def _random_day(rng):
    """Driftless iid intraday returns → unpredictable by construction."""
    return rng.normal(0, 0.001, BARS_PER_DAY)


# ~15 trading days × ~60 rows/day ≈ 900 pooled rows — above MIN_TRAIN_ROWS (400)
# and enough folds for a stable walk-forward verdict, while keeping each GBM
# training test to a few seconds so the suite stays CI-fast.
_DAYS = 15


def test_accepts_real_intraday_edge():
    f = IntradayReturnForecaster(horizon=3)
    accepted = f.train_pooled({"AAA": _intraday_bars(_momentum_day, _DAYS, seed=1),
                               "BBB": _intraday_bars(_momentum_day, _DAYS, seed=7)})
    assert accepted is True
    assert f.is_available()
    m = f.last_train_metrics
    assert m["oos_auc"] > m["auc_threshold"]
    assert m["accepted"] is True


def test_rejects_intraday_random_walk():
    bars_a = _intraday_bars(_random_day, n_days=_DAYS, seed=2)
    bars_b = _intraday_bars(_random_day, n_days=_DAYS, seed=3)
    f = IntradayReturnForecaster(horizon=3)
    accepted = f.train_pooled({"AAA": bars_a, "BBB": bars_b})
    assert accepted is False
    assert not f.is_available()
    # On noise the OOS AUC must sit near 0.5 — no fabricated edge.
    assert f.last_train_metrics["oos_auc"] < 0.55


def test_predict_none_when_not_trained():
    f = IntradayReturnForecaster()
    assert f.predict("NIFTY", _intraday_bars(_random_day, 2)) is None


def test_predict_defers_on_daily_bars():
    """A trained intraday model fed DAILY bars (one per day) must return None —
    it never fabricates an intraday signal from the wrong frequency."""
    f = IntradayReturnForecaster(horizon=3)
    assert f.train_pooled({"AAA": _intraday_bars(_momentum_day, _DAYS, seed=1)}) is True
    daily = [
        {"ts": datetime(2026, 2, 1) + timedelta(days=i), "open": 100, "high": 101,
         "low": 99, "close": 100 + i, "volume": 1000}
        for i in range(30)
    ]
    assert f.predict("NIFTY", daily) is None


def test_predict_shape_when_trained():
    f = IntradayReturnForecaster(horizon=3)
    assert f.train_pooled({"AAA": _intraday_bars(_momentum_day, _DAYS, seed=1)}) is True
    pred = f.predict("NIFTY", _intraday_bars(_momentum_day, 1, seed=99))
    assert pred is not None
    assert 0.05 <= pred.direction_probability <= 0.95
    assert 0.0 <= pred.model_uncertainty <= 1.0
    assert pred.model_id == "intraday_gbm_v1"


def test_save_load_roundtrip(tmp_path):
    f = IntradayReturnForecaster(horizon=3)
    assert f.train_pooled({"AAA": _intraday_bars(_momentum_day, _DAYS, seed=1)}) is True
    path = str(tmp_path / "intra")
    f.save(path)
    g = IntradayReturnForecaster()
    assert g.load(path) is True
    assert g.is_available()
    # A rejected model must NOT load as available.
    bad = IntradayReturnForecaster(horizon=3)
    bad.train_pooled({"AAA": _intraday_bars(_random_day, _DAYS, seed=2),
                      "BBB": _intraday_bars(_random_day, _DAYS, seed=3)})
    bad_path = str(tmp_path / "bad")
    bad.save(bad_path)
    h = IntradayReturnForecaster()
    assert h.load(bad_path) is False
    assert not h.is_available()
