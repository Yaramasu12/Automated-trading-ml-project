"""DbQueryService — read-only DB query/formatting endpoints, extracted from runtime.

Phase 2 of the runtime decomposition. Thin formatters over the TradingDatabase
for the dashboard's DB views. Verbatim move; depends only on the db handle.
"""
from __future__ import annotations

from typing import Any


class DbQueryService:
    def __init__(self, *, db: Any) -> None:
        self._db = db

    def db_summary(self) -> dict:
        return self._db.summary()

    def db_trades(self, symbol: str | None = None, execution_mode: str | None = None, limit: int = 100) -> dict:
        trades = self._db.trades(symbol=symbol, execution_mode=execution_mode, limit=limit)
        return {"count": len(trades), "trades": trades}

    def db_equity_curve(self, execution_mode: str | None = None, limit: int = 200) -> dict:
        curve = self._db.equity_curve(execution_mode=execution_mode, limit=limit)
        return {"count": len(curve), "curve": curve}

    def db_daily_pnl(self, limit: int = 30) -> dict:
        history = self._db.daily_pnl_history(limit=limit)
        return {"count": len(history), "history": history}

    def db_risk_events(self, limit: int = 50) -> dict:
        events = self._db.recent_risk_events(limit=limit)
        return {"count": len(events), "events": events}
