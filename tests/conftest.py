"""Session-wide test isolation for the four local SQLite stores.

Regression 2026-07-29: TradingRuntime() and its component stores
(TradingDatabase, OMSEventStore, TraceStore, PaperLearningJournal) all fall
back to hardcoded default paths under data/ when constructed without an
explicit path — which is exactly how most tests construct them (bare
TradingRuntime()). Those default paths are the SAME physical files the live
Docker container (and Agent Sai's hourly automated test runs) read and write
in production. Every test run was silently injecting test-fixture rows
(strategy names like "trace_rejection_test", "capital_bypass_test") into the
live-facing data/oms_events.db and data/trading.db — found because a user
asked "where are my orders" and the real 16 rows from actual trades were
buried under 10,379 test-generated ones accumulated over days of hourly runs.

This autouse fixture redirects every test to a fresh, isolated temp directory
before it runs, so no test — run by anyone, at any time — can write into the
shared data/ files again.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_default_db_paths(tmp_path, monkeypatch):
    from trading_platform.data import persistence
    from trading_platform.execution import oms_store
    from trading_platform.trace import learning_journal, store as trace_store_module

    monkeypatch.setattr(persistence, "_DEFAULT_DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(oms_store, "_DEFAULT_DB_PATH", tmp_path / "oms_events.db")
    monkeypatch.setattr(learning_journal, "_DEFAULT_DB_PATH", tmp_path / "paper_learning_journal.db")
    monkeypatch.setattr(trace_store_module, "_DEFAULT_DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(trace_store_module, "_DEFAULT_DIR", tmp_path / "decision_traces")
    yield
