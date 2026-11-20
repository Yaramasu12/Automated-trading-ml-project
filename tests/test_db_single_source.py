"""P&L single-sourcing (2026-10-13 audit #5).

The dashboard/API read trades + portfolio snapshots through runtime.db. A
missing DATABASE_URL silently falls back to the local SQLite file, which in the
real deployment is stale (trades froze 2026-09-09 while the live stack wrote to
TimescaleDB). REQUIRE_POSTGRES turns that silent fallback into a loud startup
failure.
"""
from __future__ import annotations

import unittest

from trading_platform.api.runtime import TradingRuntime
from trading_platform.config import Settings
from trading_platform.data.persistence import TradingDatabase


class RequirePostgresGuardTests(unittest.TestCase):
    def test_sqlite_backend_is_reported(self):
        # Tests run with no DATABASE_URL -> SQLite (conftest isolates the path).
        self.assertEqual(TradingDatabase().backend, "sqlite")

    def test_runtime_refuses_sqlite_when_postgres_required(self):
        settings = Settings(REQUIRE_POSTGRES=True, database_url="")
        with self.assertRaises(RuntimeError) as ctx:
            TradingRuntime(settings)
        self.assertIn("REQUIRE_POSTGRES", str(ctx.exception))

    def test_runtime_starts_on_sqlite_when_not_required(self):
        settings = Settings(REQUIRE_POSTGRES=False, database_url="")
        runtime = TradingRuntime(settings)
        self.assertEqual(runtime.db.backend, "sqlite")

    def test_bare_tradingdatabase_fails_closed_when_require_postgres_env_set(self):
        # PERSIST-1: a bare TradingDatabase() (e.g. a scheduler EOD-ingest job)
        # that auto-resolves to SQLite while REQUIRE_POSTGRES is set in the env
        # must raise, not silently diverge onto a local file nothing else reads.
        import os

        old_req, old_url = os.environ.get("REQUIRE_POSTGRES"), os.environ.get("DATABASE_URL")
        try:
            os.environ["REQUIRE_POSTGRES"] = "true"
            os.environ.pop("DATABASE_URL", None)
            with self.assertRaises(RuntimeError) as ctx:
                TradingDatabase()  # no db_path, no DATABASE_URL -> would fall back to SQLite
            self.assertIn("REQUIRE_POSTGRES", str(ctx.exception))
        finally:
            os.environ.pop("REQUIRE_POSTGRES", None) if old_req is None else os.environ.__setitem__("REQUIRE_POSTGRES", old_req)
            os.environ.pop("DATABASE_URL", None) if old_url is None else os.environ.__setitem__("DATABASE_URL", old_url)

    def test_explicit_db_path_bypasses_require_postgres_guard(self):
        # An explicit db_path is a deliberate SQLite request (tests/tools) and is
        # honored even under REQUIRE_POSTGRES — the guard targets only the silent
        # auto-resolved fallback.
        import os
        import shutil
        import tempfile
        from pathlib import Path

        old_req = os.environ.get("REQUIRE_POSTGRES")
        d = tempfile.mkdtemp()
        try:
            os.environ["REQUIRE_POSTGRES"] = "true"
            db = TradingDatabase(db_path=Path(d) / "explicit.db")
            self.assertEqual(db.backend, "sqlite")
            close = getattr(db, "close", None)
            if callable(close):
                close()
        finally:
            os.environ.pop("REQUIRE_POSTGRES", None) if old_req is None else os.environ.__setitem__("REQUIRE_POSTGRES", old_req)
            shutil.rmtree(d, ignore_errors=True)  # Windows keeps the .db handle briefly


if __name__ == "__main__":
    unittest.main()
