"""
trading_platform/ai/agents/journalist.py — Trade Journalist agent (LangGraph)

Per §8 (REDESIGN_PROMPT):
- Structured postmortem per closed trade
- Embedded into Qdrant → weekly pattern mining
- "condor losses cluster on expiry Wednesdays with VIX > 16"
- Uses LM Studio local LLM (fast tier for routine analysis)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from .base import (
    AgentConfig,
    AgentTier,
    BaseAgent,
    ReflectionCritique,
)

logger = logging.getLogger(__name__)


# ─── Output models ───────────────────────────────────────────────────────────

class TradePostmortem(BaseModel):
    """Structured post-trade analysis."""
    trade_id: str = Field(description="Unique trade identifier")
    strategy: str = Field(description="Strategy name")
    instrument: str = Field(description="Instrument symbol")
    direction: str = Field(description="long / short / iron_condor / strangle / etc.")
    entry_regime: str = Field(description="Regime at entry")
    exit_regime: str = Field(description="Regime at exit")
    planned_pnl: float = Field(description="Expected P&L at entry")
    actual_pnl: float = Field(description="Actual realized P&L")
    pnl_deviation: float = Field(description="actual - planned")
    hold_duration_sec: float = Field(description="Holding period in seconds")
    max_adverse_sec: float = Field(description="Maximum adverse excursion during trade")
    max_beneficial_sec: float = Field(description="Maximum beneficial excursion during trade")
    key_drivers: list[str] = Field(default_factory=list, description="Key P&L drivers")
    execution_quality: str = Field(description="poor / fair / good / excellent")
    execution_notes: list[str] = Field(default_factory=list, description="Execution-specific observations")
    agent_votes: Optional[dict[str, str]] = Field(default=None, description="Agent vote summary at entry time")
    cost_breakdown: dict[str, float] = Field(default_factory=dict, description="Brokerage, STT, slippage breakdown")
    pattern_match: Optional[str] = Field(default=None, description="Match against known patterns")
    lessons: list[str] = Field(default_factory=list, description="Key lessons learned")
    improvement_suggestions: list[str] = Field(default_factory=list, description="Suggestions for strategy improvement")
    confidence: float = Field(description="Confidence in this analysis 0..1", ge=0.0, le=1.0)
    notes: str = Field(default="", description="Brief narrative summary")

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class WeeklyPattern(BaseModel):
    """Mined pattern from weekly aggregation."""
    pattern_name: str = Field(description="Descriptive pattern name")
    condition: str = Field(description="Condition that triggers this pattern")
    hit_rate: float = Field(description="Hit rate 0..1")
    avg_pnl: float = Field(description="Average P&L per trade")
    max_dd: float = Field(description="Maximum drawdown during pattern")
    sample_size: int = Field(description="Number of trades in pattern")
    significance: str = Field(description="high / medium / low")
    strategy: str = Field(description="Strategy this applies to")
    notes: str = Field(default="", description="Brief narrative")


# ─── Input schema ────────────────────────────────────────────────────────────

@dataclass
class JournalistQuery:
    """Input to the Trade Journalist agent."""
    trade_id: str = ""
    strategy: str = ""
    instrument: str = ""
    direction: str = ""
    entry_time: datetime = field(default_factory=datetime.now)
    exit_time: datetime = field(default_factory=lambda: datetime.now())
    entry_price: float = 0.0
    exit_price: float = 0.0
    size: float = 0.0
    planned_pnl: float = 0.0
    actual_pnl: float = 0.0
    entry_regime: str = ""
    exit_regime: str = ""
    entry_iv: float = 0.0
    exit_iv: float = 0.0
    entry_rv: float = 0.0
    exit_rv: float = 0.0
    entry_conviction: float = 0.0
    veto_agent_decision: str = ""
    veto_agent_votes: dict[str, str] = field(default_factory=dict)
    costs: dict[str, float] = field(default_factory=dict)
    max_adverse_excursion: float = 0.0
    max_beneficial_excursion: float = 0.0
    fill_details: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""


# ─── Trade Journalist Agent ──────────────────────────────────────────────────

class TradeJournalist(BaseAgent):
    """
    Trade Journalist agent per §8.

    Generates structured postmortems for every closed trade.
    Embeds into Qdrant for weekly pattern mining.
    """

    def __init__(self, config: AgentConfig):
        super().__init__(
            config=config,
            tier=AgentTier("fast", timeout_seconds=15.0, max_concurrent=2, model="fast"),
            system_prompt=(
                "You are a trading historian and analyst. Your job is to analyze closed trades, "
                "understand what happened, and extract actionable lessons. Be objective, data-driven, "
                "and specific. Never sugarcoat losses. Celebrate wins but analyze why they worked."
            ),
            max_reflection_rounds=1,
        )

        self._postmortem_prompt = """
Analyze the following closed trade and provide a structured postmortem.

## Trade Details
{trade}

## Market Conditions
{market}

## Execution Details
{execution}

## Agent Votes at Entry
{agents}

## Cost Breakdown
{costs}

Provide a structured postmortem. Be specific and data-driven.
"""

        self._pattern_prompt = """
Analyze these trade summaries and identify recurring patterns.

## Trade Summaries
{trades}

List the top 3-5 patterns with conditions, hit rates, and sample sizes.
"""

    async def postmortem(self, query: JournalistQuery) -> TradePostmortem:
        """Generate a structured postmortem for a closed trade."""
        # Quick-path: if P&L is within 5% of planned and no unusual conditions, use template
        if query.planned_pnl != 0 and abs(query.actual_pnl - query.planned_pnl) / max(abs(query.planned_pnl), 1) < 0.05:
            if not query.fill_details:
                return TradePostmortem(
                    trade_id=query.trade_id,
                    strategy=query.strategy,
                    instrument=query.instrument,
                    direction=query.direction,
                    entry_regime=query.entry_regime,
                    exit_regime=query.exit_regime,
                    planned_pnl=query.planned_pnl,
                    actual_pnl=query.actual_pnl,
                    pnl_deviation=query.actual_pnl - query.planned_pnl,
                    hold_duration_sec=(query.exit_time - query.entry_time).total_seconds(),
                    max_adverse_sec=query.max_adverse_excursion,
                    max_beneficial_sec=query.max_beneficial_excursion,
                    key_drivers=["Trade performed as planned"],
                    execution_quality="good",
                    execution_notes=["Fill quality within expected parameters"],
                    agent_votes=query.veto_agent_votes or None,
                    cost_breakdown=query.costs,
                    pattern_match=None,
                    lessons=["Strategy behaved as backtested"],
                    improvement_suggestions=[],
                    confidence=0.9,
                    notes="Trade within expected parameters — no unusual features",
                )

        # Build context for LLM analysis
        context = self._build_context(query)
        messages = [
            {
                "role": "user",
                "content": self._postmortem_prompt.format(**context),
            }
        ]

        # Call LLM with structured output
        critique = await self.run_with_reflection(
            messages,
            response_model=TradePostmortem,
        )

        # Ensure output is TradePostmortem
        if isinstance(critique.original_output, TradePostmortem):
            pm = critique.original_output
            pm.pnl_deviation = query.actual_pnl - query.planned_pnl
            pm.hold_duration_sec = (query.exit_time - query.entry_time).total_seconds()
            pm.max_adverse_sec = query.max_adverse_excursion
            pm.max_beneficial_sec = query.max_beneficial_excursion
            pm.cost_breakdown = query.costs
            pm.agent_votes = query.veto_agent_votes or None
            return pm
        elif isinstance(critique.original_output, dict):
            try:
                return TradePostmortem(
                    trade_id=query.trade_id,
                    strategy=query.strategy,
                    instrument=query.instrument,
                    direction=query.direction,
                    entry_regime=query.entry_regime,
                    exit_regime=query.exit_regime,
                    planned_pnl=query.planned_pnl,
                    actual_pnl=query.actual_pnl,
                    pnl_deviation=query.actual_pnl - query.planned_pnl,
                    hold_duration_sec=(query.exit_time - query.entry_time).total_seconds(),
                    max_adverse_sec=query.max_adverse_excursion,
                    max_beneficial_sec=query.max_beneficial_excursion,
                    key_drivers=critique.original_output.get("key_drivers", ["LLM output partial"]),
                    execution_quality=critique.original_output.get("execution_quality", "unknown"),
                    execution_notes=critique.original_output.get("execution_notes", []),
                    agent_votes=query.veto_agent_votes or None,
                    cost_breakdown=query.costs,
                    pattern_match=None,
                    lessons=critique.original_output.get("lessons", []),
                    improvement_suggestions=critique.original_output.get("improvement_suggestions", []),
                    confidence=float(critique.original_output.get("confidence", 0.5)),
                    notes=critique.original_output.get("notes", "LLM output parse partial"),
                )
            except (ValueError, TypeError) as e:
                logger.warning(f"[TradeJournalist] LLM output parse failed: {e}")

        # Fallback: template-based postmortem
        return TradePostmortem(
            trade_id=query.trade_id,
            strategy=query.strategy,
            instrument=query.instrument,
            direction=query.direction,
            entry_regime=query.entry_regime,
            exit_regime=query.exit_regime,
            planned_pnl=query.planned_pnl,
            actual_pnl=query.actual_pnl,
            pnl_deviation=query.actual_pnl - query.planned_pnl,
            hold_duration_sec=(query.exit_time - query.entry_time).total_seconds(),
            max_adverse_sec=query.max_adverse_excursion,
            max_beneficial_sec=query.max_beneficial_excursion,
            key_drivers=["Template-based analysis (LLM output failed)"],
            execution_quality="unknown",
            execution_notes=[],
            agent_votes=query.veto_agent_votes or None,
            cost_breakdown=query.costs,
            pattern_match=None,
            lessons=[],
            improvement_suggestions=[],
            confidence=0.3,
            notes="Template fallback — LLM postmortem unavailable",
        )

    async def mine_patterns(self, trades: list[JournalistQuery]) -> list[WeeklyPattern]:
        """Mine recurring patterns from a batch of trade postmortems."""
        if len(trades) < 10:
            logger.info("[TradeJournalist] Need >= 10 trades for pattern mining, got %d", len(trades))
            return []

        # Build trade summaries
        summaries = []
        for t in trades[:50]:  # Limit to 50 for LLM context window
            summaries.append(
                f"Trade {t.trade_id}: {t.strategy} on {t.instrument}, "
                f"P&L={t.actual_pnl:+.2f}, hold={((t.exit_time - t.entry_time).total_seconds()/60):.0f}min, "
                f"regime={t.entry_regime}"
            )
        trade_text = "\n".join(summaries)

        messages = [
            {
                "role": "user",
                "content": self._pattern_prompt.format(trades=trade_text),
            }
        ]

        critique = await self.run_with_reflection(
            messages,
            response_model=list[WeeklyPattern],
        )

        if isinstance(critique.original_output, list):
            return critique.original_output
        elif isinstance(critique.original_output, dict):
            patterns_data = critique.original_output.get("patterns", [])
            return [
                WeeklyPattern(
                    pattern_name=p.get("pattern_name", "Unknown"),
                    condition=p.get("condition", ""),
                    hit_rate=float(p.get("hit_rate", 0)),
                    avg_pnl=float(p.get("avg_pnl", 0)),
                    max_dd=float(p.get("max_dd", 0)),
                    sample_size=int(p.get("sample_size", 0)),
                    significance=p.get("significance", "low"),
                    strategy=p.get("strategy", ""),
                    notes=p.get("notes", ""),
                )
                for p in patterns_data
            ]

        return []

    def _build_context(self, query: JournalistQuery) -> dict[str, str]:
        """Build context dict for prompt formatting."""
        trade = (
            f"ID: {query.trade_id}\n"
            f"Strategy: {query.strategy}\n"
            f"Instrument: {query.instrument}\n"
            f"Direction: {query.direction}\n"
            f"Entry: {query.entry_price:.4f} @ {query.entry_time}\n"
            f"Exit: {query.exit_price:.4f} @ {query.exit_time}\n"
            f"Size: {query.size:.4f}\n"
            f"Planned P&L: {query.planned_pnl:+.2f}\n"
            f"Actual P&L: {query.actual_pnl:+.2f}\n"
            f"Deviation: {query.actual_pnl - query.planned_pnl:+.2f}"
        )

        market = (
            f"Entry IV: {query.entry_iv:.1f}, Exit IV: {query.exit_iv:.1f}\n"
            f"Entry RV: {query.entry_rv:.1f}, Exit RV: {query.exit_rv:.1f}\n"
            f"Entry Conviction: {query.entry_conviction:.2f}"
        )

        exec_details = [
            f"  Leg {i+1}: {leg.get('side', '?')} {leg.get('qty', 0)} @ {leg.get('price', 0)} "
            f"slippage={leg.get('slippage', 0):+.4f}"
            for i, leg in enumerate(query.fill_details[:5])
        ]
        execution = "\n".join(exec_details) if exec_details else "No detailed fill data"

        agents = "\n".join(f"  {k}: {v}" for k, v in query.veto_agent_votes.items()) if query.veto_agent_votes else "No agent votes"

        cost_items = [f"  {k}: {v:+.4f}" for k, v in query.costs.items()]
        costs = "\n".join(cost_items) if cost_items else "No cost data"

        return {
            "trade": trade,
            "market": market,
            "execution": execution,
            "agents": agents,
            "costs": costs,
        }


# ─── Registry helper ─────────────────────────────────────────────────────────

def make_trade_journalist(config: AgentConfig) -> TradeJournalist:
    """Factory to create a TradeJournalist with config."""
    return TradeJournalist(config)