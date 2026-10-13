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


if __name__ == "__main__":
    unittest.main()
