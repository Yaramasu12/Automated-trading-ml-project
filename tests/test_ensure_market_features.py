"""Tests for MasterOrchestrator._ensure_market_features()'s synthetic-bars
persistence guard.

Found 2026-09-07 alongside the identical bug in decision/pipeline.py's
scan(): this method also calls rt.decision_pipeline._fetch_bars(...) and
then unconditionally appended the resulting FeatureEngine snapshot into
rt.feature_store — the SAME store the live orchestrator reads bars from for
neural forecasting — with no check for whether those bars were fabricated
(bars_were_synthetic()). If history_provider were ever absent for a
deployment, this path would silently persist synthetic snapshots labelled
with today's real date, indistinguishable from genuine data. Fixed by
skipping the append whenever bars_were_synthetic(underlying) is True.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from trading_platform.domain.models import MarketBar
from trading_platform.orchestrator.master_orchestrator import MasterOrchestrator


def _bars(n: int = 25, base: float = 1000.0) -> list[MarketBar]:
    now = datetime.now(timezone.utc)
    bars = []
    price = base
    for i in range(n):
        price *= 1.001
        bars.append(MarketBar(
            timestamp=now - timedelta(days=n - i),
            symbol="RELIANCE",
            open=price, high=price * 1.01, low=price * 0.99, close=price,
            volume=100_000,
        ))
    return bars


class _FakeFeatureStore:
    def __init__(self, existing: dict | None = None):
        self.appended: list[tuple] = []
        self._existing = existing or {}

    def get_features(self, underlying):
        return self._existing

    def append(self, underlying, as_of, features, regime):
        self.appended.append((underlying, as_of, features, regime))


def _runtime(bars_were_synthetic: bool, feature_store: _FakeFeatureStore) -> SimpleNamespace:
    return SimpleNamespace(
        decision_pipeline=SimpleNamespace(
            _fetch_bars=lambda underlying, start, days: _bars(),
            bars_were_synthetic=lambda underlying: bars_were_synthetic,
        ),
        feature_store=feature_store,
        live_feed=None,
    )


def test_synthetic_bars_are_not_persisted():
    store = _FakeFeatureStore()
    rt = _runtime(bars_were_synthetic=True, feature_store=store)
    orch = MasterOrchestrator(rt)

    result = orch._ensure_market_features("RELIANCE", "TRENDING")

    assert result  # still returns the computed snapshot to the caller
    assert store.appended == []


def test_real_bars_are_persisted_once_per_day():
    store = _FakeFeatureStore()
    rt = _runtime(bars_were_synthetic=False, feature_store=store)
    orch = MasterOrchestrator(rt)

    orch._ensure_market_features("RELIANCE", "TRENDING")

    assert len(store.appended) == 1
    assert store.appended[0][0] == "RELIANCE"


def test_real_bars_not_reappended_same_day():
    from trading_platform.orchestrator.master_orchestrator import _IST
    today = datetime.now(timezone.utc).astimezone(_IST).date().isoformat()
    store = _FakeFeatureStore(existing={"date": today})
    rt = _runtime(bars_were_synthetic=False, feature_store=store)
    orch = MasterOrchestrator(rt)

    orch._ensure_market_features("RELIANCE", "TRENDING")

    # Already has today's snapshot -> must not append again, regardless of
    # the synthetic-bars fix (this guards the pre-existing "once per day"
    # behavior stays intact).
    assert store.appended == []
