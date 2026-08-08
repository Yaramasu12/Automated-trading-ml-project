"""
trading_platform/ai/agents/veto.py — Signal Veto Agent (LangGraph)

Per §8 (REDESIGN_PROMPT):
- Reviews each short-vol/swing entry against RAG context
- Pending events, fresh news on the underlying
- Powers: approve | veto | downsize
- Never initiates, never upsizes — consistent with "advisory ≠ safety"
- Fast tier (Gemma-3-12B class) for low-latency review
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from .base import (
    AgentConfig,
    AgentTier,
    BaseAgent,
    ReflectionCritique,
)

logger = logging.getLogger(__name__)


# ─── Enums ───────────────────────────────────────────────────────────────────

class VetoDecision(str, Enum):
    """Veto agent decision."""
    APPROVE = "approve"
    VETO = "veto"
    DOWNSIZE = "downsize"


class VetoReason(str, Enum):
    """Reason for veto/downsize."""
    PENDING_EVENT = "pending_event"
    NEWS_CATALYST = "news_catalyst"
    LIQUIDITY_CONCERN = "liquidity_concern"
    REGIME_MISMATCH = "regime_mismatch"
    IV_ANOMALY = "iv_anomaly"
    CROSS_ASSET_DIVERGENCE = "cross_asset_divergence"
    EXPIRY_GAMMA = "expiry_gamma_risk"
    RBI_SEBI = "rbi_sebi_circular"
    NONE = "none"  # no issues, approve as-is


# ─── Output models ───────────────────────────────────────────────────────────

class VetoAction(BaseModel):
    """Structured veto decision output."""
    decision: VetoDecision = Field(description="approve | veto | downsize")
    reason: VetoReason = Field(description="Primary reason for decision")
    size_multiplier: float = Field(
        description="Multiplier to apply to proposed size (0.0 = veto, 0.5 = downsize 50%, 1.0 = approve full)",
        ge=0.0, le=1.0
    )
    risk_flags: list[str] = Field(default_factory=list, description="Specific risk flags identified")
    suggested_adjustments: list[str] = Field(default_factory=list, description="Suggested trade adjustments")
    expiry_caution: bool = Field(default=False, description="True if expiry-day gamma risk is elevated")
    confidence: float = Field(description="Confidence in this decision 0..1", ge=0.0, le=1.0)
    notes: str = Field(default="", description="Brief narrative")

    @field_validator("size_multiplier")
    @classmethod
    def clamp_multiplier(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


# ─── Input schema ────────────────────────────────────────────────────────────

@dataclass
class VetoQuery:
    """Input to the Signal Veto Agent."""
    # Signal / strategy context
    strategy: str = ""
    instrument: str = ""
    direction: str = ""  # long, short, iron_condor, strangle, etc.
    conviction: float = 0.0
    proposed_size: float = 0.0
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None

    # Market context
    iv: float = 0.0
    rv: float = 0.0
    iv_rank: float = 0.0
    iv_skew: float = 0.0
    vix: float = 0.0
    vix_change: float = 0.0

    # RAG context
    rag_context: str = ""
    pending_events: list[str] = field(default_factory=list)
    news_sentiment: dict[str, float] = field(default_factory=dict)
    rbi_sebi_flags: list[str] = field(default_factory=list)

    # Expiry context
    minutes_to_expiry: Optional[int] = None
    is_expiry_day: bool = False

    # Cross-asset
    usdinr: float = 0.0
    usdinr_change: float = 0.0
    gift_nifty: float = 0.0
    gift_nifty_change: float = 0.0

    # Quant regime
    quant_regime: Optional[str] = None
    regime_confidence: float = 0.0

    # Time
    query_time: datetime = field(default_factory=datetime.now)


# ─── Signal Veto Agent ───────────────────────────────────────────────────────

class SignalVetoAgent(BaseAgent):
    """
    Signal Veto Agent per §8.

    Reviews entries against RAG context. VETO-ONLY power.
    Never initiates, never upsizes — consistent with "advisory ≠ safety".
    """

    def __init__(self, config: AgentConfig):
        super().__init__(
            config=config,
            tier=AgentTier("fast", timeout_seconds=15.0, max_concurrent=3, model="fast"),
            system_prompt=(
                "You are a risk-focused trading analyst. Your job is to REVIEW trading signals "
                "against market context, news, and pending events. You have VETO power — you can "
                "approve, veto, or downsize a signal. You CANNOT initiate new trades or increase "
                "size beyond what was proposed. Always be conservative. If in doubt, downsize."
            ),
            max_reflection_rounds=0,  # No reflection for fast-tier veto
        )

        self._veto_prompt = """
Review the following trading signal against the provided context.

## Signal
{signal}

## Market Context
{market}

## Pending Events
{events}

## News Sentiment
{news}

## RAG Context (recent events, filings, announcements)
{rag}

## RBI/SEBI Regulatory Flags
{regulatory}

## Cross-Asset Signals
{cross_asset}

## Expiry Context
{expiry}

Provide a structured veto decision. Be conservative.
"""

    async def review(self, query: VetoQuery) -> VetoAction:
        """Review a trading signal for veto risk."""
        # Quick-path: if no RAG context and no pending events, auto-approve
        if not query.rag_context and not query.pending_events and not query.rbi_sebi_flags:
            # Still check IV anomaly
            if query.iv > 0 and query.rv > 0 and abs(query.iv - query.rv) / query.rv > 0.5:
                return VetoAction(
                    decision=VetoDecision.DOWNSIZE,
                    reason=VetoReason.IV_ANOMALY,
                    size_multiplier=0.5,
                    risk_flags=["IV/RV divergence > 50%"],
                    suggested_adjustments=["Consider wider strikes to reduce gamma exposure"],
                    expiry_caution=query.is_expiry_day,
                    confidence=0.7,
                    notes="IV/RV divergence detected — downsize as precaution",
                )
            return VetoAction(
                decision=VetoDecision.APPROVE,
                reason=VetoReason.NONE,
                size_multiplier=1.0,
                confidence=0.95,
                notes="No context flags — approve as-is",
            )

        # Build messages for LLM review
        context = self._build_context(query)
        messages = [
            {
                "role": "user",
                "content": self._veto_prompt.format(**context),
            }
        ]

        # Call LLM with structured output
        critique = await self.run_with_reflection(
            messages,
            response_model=VetoAction,
        )

        # Ensure output is VetoAction
        if isinstance(critique.original_output, VetoAction):
            return critique.original_output
        elif isinstance(critique.original_output, dict):
            # Parse from dict
            try:
                decision = VetoDecision(critique.original_output.get("decision", "approve"))
                reason = VetoReason(critique.original_output.get("reason", "none"))
                return VetoAction(
                    decision=decision,
                    reason=reason,
                    size_multiplier=float(critique.original_output.get("size_multiplier", 1.0)),
                    risk_flags=critique.original_output.get("risk_flags", []),
                    suggested_adjustments=critique.original_output.get("suggested_adjustments", []),
                    expiry_caution=bool(critique.original_output.get("expiry_caution", False)),
                    confidence=float(critique.original_output.get("confidence", 0.5)),
                    notes=critique.original_output.get("notes", "LLM output parse partial"),
                )
            except (ValueError, TypeError) as e:
                logger.warning(f"[SignalVetoAgent] LLM output parse failed: {e}")

        # Fallback: auto-approve on parse failure (safety: never block valid trades)
        return VetoAction(
            decision=VetoDecision.APPROVE,
            reason=VetoReason.NONE,
            size_multiplier=1.0,
            confidence=0.5,
            notes="LLM output parse failed — auto-approve (safety fallback)",
        )

    def _build_context(self, query: VetoQuery) -> dict[str, str]:
        """Build context dict for prompt formatting."""
        signal = (
            f"Strategy: {query.strategy}\n"
            f"Instrument: {query.instrument}\n"
            f"Direction: {query.direction}\n"
            f"Conviction: {query.conviction:.2f}\n"
            f"Size: {query.proposed_size:.4f}\n"
            f"Entry: {query.entry_price}\n"
            f"Stop: {query.stop_loss}\n"
            f"Target: {query.target_price}"
        )

        market = (
            f"IV: {query.iv:.1f}, RV: {query.rv:.1f}, IV Rank: {query.iv_rank:.0f}%\n"
            f"IV Skew: {query.iv_skew:.3f}\n"
            f"VIX: {query.vix:.1f} ({query.vix_change:+.1f})"
        )

        events = "\n".join(query.pending_events) if query.pending_events else "No pending events"

        news_items = [f"{k}: {v:+.3f}" for k, v in query.news_sentiment.items()]
        news = "\n".join(news_items) if news_items else "No news sentiment data"

        rag = query.rag_context if query.rag_context else "No RAG context available"

        regulatory = "\n".join(query.rbi_sebi_flags) if query.rbi_sebi_flags else "No regulatory flags"

        cross = (
            f"USDINR: {query.usdinr:.4f} ({query.usdinr_change:+.4f})\n"
            f"GIFT Nifty: {query.gift_nifty:.1f} ({query.gift_nifty_change:+.1f})"
        )

        if query.is_expiry_day and query.minutes_to_expiry is not None:
            expiry = f"EXPIRY DAY — {query.minutes_to_expiry} min remaining"
        else:
            expiry = "Not an expiry day"

        return {
            "signal": signal,
            "market": market,
            "events": events,
            "news": news,
            "rag": rag,
            "regulatory": regulatory,
            "cross_asset": cross,
            "expiry": expiry,
        }


# ─── Registry helper ─────────────────────────────────────────────────────────

def make_signal_veto_agent(config: AgentConfig) -> SignalVetoAgent:
    """Factory to create a SignalVetoAgent with config."""
    return SignalVetoAgent(config)