"""
trading_platform/ai/agents/regime.py — Regime Analyst agent (LangGraph)

Per §8 (REDESIGN_PROMPT):
- Daily + intraday regime classification
- Cross-checked against quantitative regime (HMM on realized vol/breadth)
- Disagreement lowers system conviction multiplier
- Structured JSON output via Pydantic
- Uses LM Studio local LLM (deep tier for regime analysis)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
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

class RegimeClassification(BaseModel):
    """Structured regime classification output."""
    regime: str = Field(description="Current market regime: trending_up, trending_down, mean_reverting, high_vol_chop, low_vol_compress, breakout, crash")
    confidence: float = Field(description="Confidence 0..1", ge=0.0, le=1.0)
    volatility_regime: str = Field(description="vol_regime: low, normal, high, extreme")
    volatility_level: float = Field(description="Current realized vol (annualized)")
    trend_strength: float = Field(description="Hurst exponent or trend measure 0..1")
    breadth: float = Field(description="Market breadth indicator -1..1")
    vix_level: Optional[float] = Field(default=None, description="India VIX level if available")
    vix_trend: Optional[str] = Field(default=None, description="vix_trend: rising, stable, falling")
    liquidity: str = Field(description="liquidity: tight, normal, abundant")
    session: str = Field(description="Current session: pre_open, morning, mid_day, afternoon, close, post_open, off")
    key_levels: list[dict[str, Any]] = Field(default_factory=list, description="Key support/resistance levels")
    catalysts: list[str] = Field(default_factory=list, description="Near-term catalysts from RAG")
    cross_asset: Optional[dict[str, Any]] = Field(default=None, description="Cross-asset signals (USDINR, GIFT Nifty, etc.)")
    notes: str = Field(default="", description="Brief narrative summary")

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class RegimeDisagreementReport(BaseModel):
    """Report when LLM regime disagrees with quantitative regime."""
    llm_regime: str
    quant_regime: str
    disagreement_reason: str
    conviction_adjustment: float = Field(description="Multiplier 0..1 to apply to system conviction", ge=0.0, le=1.0)
    action: str = Field(description="Recommended action: defer, reduce_size, hold, exit")


# ─── Input schema ────────────────────────────────────────────────────────────

@dataclass
class RegimeQuery:
    """Input to the Regime Analyst agent."""
    # Market data
    nifty_ltp: float = 0.0
    banknifty_ltp: float = 0.0
    finnifty_ltp: float = 0.0
    sensex_ltp: float = 0.0
    vix: float = 0.0
    vix_change: float = 0.0
    usdinr: float = 0.0
    gift_nifty: float = 0.0

    # Realized vol
    rv_5m: float = 0.0
    rv_1h: float = 0.0
    rv_1d: float = 0.0

    # Breadth
    advance_decline: tuple[int, int] = (0, 0)
    top_gainer_count: int = 0
    top_loser_count: int = 0

    # OI data
    nifty_oi_change: float = 0.0
    banknifty_oi_change: float = 0.0
    pcr: float = 1.0
    max_pain: float = 0.0

    # Time
    session: str = "off"
    minutes_to_expiry: Optional[int] = None

    # RAG context
    rag_context: Optional[str] = None
    pending_events: list[str] = field(default_factory=list)
    news_sentiment: dict[str, float] = field(default_factory=dict)

    # Quant regime (for cross-check)
    quant_regime: Optional[str] = None
    quant_confidence: float = 0.0


# ─── Regime Analyst Agent ────────────────────────────────────────────────────

class RegimeAnalyst(BaseAgent):
    """
    Regime Analyst agent per §8.

    Provides daily + intraday regime classification.
    Cross-checked against quantitative regime.
    Disagreement lowers system conviction multiplier.
    """

    def __init__(self, config: AgentConfig):
        super().__init__(
            config=config,
            tier=AgentTier("deep", timeout_seconds=60.0, max_concurrent=1, model="deep"),
            system_prompt=(
                "You are a senior market analyst specializing in Indian equity derivatives. "
                "Your job is to classify the current market regime using all available data. "
                "Be precise, cautious, and evidence-based. Never guess. "
                "Output structured data only. If data is insufficient, state that explicitly."
            ),
            max_reflection_rounds=1,
        )

        # System prompt for regime analysis
        self._regime_prompt = """
Analyze the market regime based on the following data:

## Price Levels
{prices}

## Volatility
{volatility}

## Open Interest & Put-Call Ratio
{oi_data}

## Breadth
{breadth}

## Session Context
{session}

## RAG Context (news, events)
{rag_context}

## Quant Regime Cross-Check
{quant_regime}

Classify the regime and provide a structured output.
"""

    async def analyze(self, query: RegimeQuery) -> RegimeClassification:
        """Run regime analysis on current market data."""
        # Build messages
        context = self._build_context(query)
        messages = [
            {
                "role": "user",
                "content": self._regime_prompt.format(**context),
            }
        ]

        # Call LLM with structured output
        critique = await self.run_with_reflection(
            messages,
            response_model=RegimeClassification,
        )

        # Cross-check against quant regime
        if query.quant_regime is not None:
            disagreement = self._cross_check(critique, query)
            if disagreement is not None:
                logger.warning(
                    f"[RegimeAnalyst] Disagreement: LLM={critique.original_output.get('regime', '?')} vs "
                    f"Quant={query.quant_regime} (confidence={query.quant_confidence:.2f})"
                )

        return critique.original_output if isinstance(critique.original_output, RegimeClassification) else RegimeClassification(
            regime="unknown",
            confidence=0.0,
            volatility_regime="unknown",
            volatility_level=0.0,
            trend_strength=0.0,
            breadth=0.0,
            liquidity="unknown",
            session=query.session,
            key_levels=[],
            catalysts=[],
            notes="LLM output parse failed, fallback regime",
        )

    def _build_context(self, query: RegimeQuery) -> dict[str, str]:
        """Build context dict for prompt formatting."""
        prices = f"NIFTY: {query.nifty_ltp}, BANKNIFTY: {query.banknifty_ltp}, FINNIFTY: {query.finnifty_ltp}, SENSEX: {query.sensex_ltp}"
        volatility = f"RV 5m: {query.rv_5m:.2f}%, RV 1h: {query.rv_1h:.2f}%, RV 1D: {query.rv_1d:.2f}%, VIX: {query.vix:.1f} ({query.vix_change:+.1f})"
        oi_data = f"NIFTY OI Δ: {query.nifty_oi_change:+.0f}, BANKNIFTY OI Δ: {query.banknifty_oi_change:+.0f}, PCR: {query.pcr:.2f}, Max Pain: {query.max_pain:.0f}"
        adv = f"{query.advance_decline[0]} advance / {query.advance_decline[1]} decline"
        breadth = f"{adv}, Gainners: {query.top_gainer_count}, Losers: {query.top_loser_count}"
        session = f"Session: {query.session}"
        if query.minutes_to_expiry is not None:
            session += f", TTE: {query.minutes_to_expiry}min"
        rag = query.rag_context or "No RAG context available"
        quant = f"Quant regime: {query.quant_regime} (confidence: {query.quant_confidence:.2f})" if query.quant_regime else "No quant regime available"

        return {
            "prices": prices,
            "volatility": volatility,
            "oi_data": oi_data,
            "breadth": breadth,
            "session": session,
            "rag_context": rag,
            "quant_regime": quant,
        }

    def _cross_check(
        self,
        critique: ReflectionCritique,
        query: RegimeQuery,
    ) -> Optional[RegimeDisagreementReport]:
        """Cross-check LLM regime against quantitative regime."""
        llm_regime = None
        if isinstance(critique.original_output, RegimeClassification):
            llm_regime = critique.original_output.regime
        elif isinstance(critique.original_output, dict):
            llm_regime = critique.original_output.get("regime")

        if llm_regime is None or query.quant_regime is None:
            return None

        # Define disagreement: different regime family
        families = {
            "trending": {"trending_up", "trending_down"},
            "mean-rev": {"mean_reverting"},
            "high-vol": {"high_vol_chop", "crash"},
            "low-vol": {"low_vol_compress"},
            "breakout": {"breakout"},
        }

        llm_family = None
        for fam, regimes in families.items():
            if llm_regime in regimes:
                llm_family = fam
                break

        quant_family = None
        quant_map = {
            "trending_up": "trending",
            "trending_down": "trending",
            "mean_reverting": "mean-rev",
            "high_vol_chop": "high-vol",
            "low_vol_compress": "low-vol",
            "breakout": "breakout",
        }
        quant_family = quant_map.get(query.quant_regime)

        if llm_family is None or quant_family is None:
            return None

        # Disagreement if different family AND quant confidence is high
        if llm_family != quant_family and query.quant_confidence > 0.6:
            adjustment = max(0.3, 1.0 - query.quant_confidence)
            return RegimeDisagreementReport(
                llm_regime=llm_regime,
                quant_regime=query.quant_regime,
                disagreement_reason=f"LLM regime family '{llm_family}' differs from quant regime family '{quant_family}'",
                conviction_adjustment=adjustment,
                action="reduce_size",
            )

        return None


# ─── Registry helper ─────────────────────────────────────────────────────────

def make_regime_analyst(config: AgentConfig) -> RegimeAnalyst:
    """Factory to create a RegimeAnalyst with config."""
    return RegimeAnalyst(config)