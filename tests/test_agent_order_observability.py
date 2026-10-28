"""The agent must explain policy suppression and short-vol no-entry decisions."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from trading_platform.agent.trading_agent import AgentCycleResult, TradingAgent


class _DecliningShortVol:
    async def auto_enter(self, _now):
        return {
            "ran": True,
            "results": [
                {"underlying": "FINNIFTY", "submitted": False, "reason": "premium below threshold"}
            ],
        }


def test_agent_status_keeps_short_vol_no_entry_reason(monkeypatch):
    monkeypatch.setenv("SHORTVOL_CONTINUOUS", "true")
    monkeypatch.delenv("AGENT_DIRECTIONAL_ENABLED", raising=False)
    agent = TradingAgent(SimpleNamespace(short_vol_executor=_DecliningShortVol()))
    now = datetime(2026, 10, 27, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    asyncio.run(agent._maybe_enter_short_vol(now))

    status = agent.status()
    assert status["directional_execution_enabled"] is False
    assert status["last_shortvol_entry"]["results"][0]["reason"] == "premium below threshold"


def test_cycle_reports_candidates_suppressed_by_policy():
    cycle = AgentCycleResult(
        ts="11:00:00",
        market_status="OPEN",
        underlyings_scanned=["FINNIFTY"],
        total_candidates=1,
        approved=1,
        rejected=0,
        enqueued=0,
        skipped_existing=0,
        regimes={},
        signals=[],
        errors=[],
        duration_ms=10,
        suppressed_policy=1,
    )

    assert TradingAgent._cycle_to_dict(cycle)["suppressed_policy"] == 1
