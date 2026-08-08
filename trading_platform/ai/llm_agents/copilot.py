"""
trading_platform/ai/agents/copilot.py — Copilot chat agent (LangGraph)

Per §8 (REDESIGN_PROMPT):
- Explains any decision by tracing signal features + risk checks + agent votes
- Natural-language → backtest config
- Fast tier (Gemma-3-12B class) for low-latency chat
- Tool use: retrieve chain data, price series, backtest results as function calls
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from .base import AgentConfig, AgentTier, BaseAgent, ReflectionCritique

logger = logging.getLogger(__name__)


# ─── Output models ───────────────────────────────────────────────────────────

class CopilotResponse(BaseModel):
    """Structured response from the Copilot agent."""
    response: str = Field(description="Natural-language response to the user's query")
    explanation: str = Field(description="Detailed technical explanation of the reasoning")
    tools_used: list[str] = Field(default_factory=list, description="Which data sources were consulted")
    confidence: float = Field(description="Confidence 0..1", ge=0.0, le=1.0)
    follow_up: list[str] = Field(default_factory=list, description="Suggested next steps")
    backtest_config: Optional[dict[str, Any]] = Field(default=None, description="Parsed backtest config if user requested")
    trade_explanation: Optional[dict[str, Any]] = Field(default=None, description="Trade explanation if requested")

    @property
    def is_backtest_config(self) -> bool:
        return self.backtest_config is not None

    @property
    def is_trade_explanation(self) -> bool:
        return self.trade_explanation is not None


# ─── Input schema ────────────────────────────────────────────────────────────

@dataclass
class CopilotQuery:
    """Input to the Copilot agent."""
    user_query: str = ""
    user_id: str = ""
    context: dict[str, Any] = None  # Current market state, portfolio, etc.
    recent_trades: list[dict[str, Any]] = None  # Recent closed trades
    active_strategies: list[str] = None  # Currently running strategies
    portfolio_state: dict[str, Any] = None  # Current portfolio state
    timestamp: datetime = None

    def __post_init__(self):
        if self.context is None:
            self.context = {}
        if self.recent_trades is None:
            self.recent_trades = []
        if self.active_strategies is None:
            self.active_strategies = []
        if self.portfolio_state is None:
            self.portfolio_state = {}
        if self.timestamp is None:
            self.timestamp = datetime.now()


# ─── Tool definitions ────────────────────────────────────────────────────────

class CopilotTool:
    """Tool interface for the Copilot agent."""

    @staticmethod
    async def retrieve_chain_data(instrument: str, expiry: Optional[str] = None) -> dict[str, Any]:
        """Retrieve option chain data for an instrument."""
        # Placeholder: would query MarketDataService or Redis cache
        logger.info("[CopilotTool] retrieve_chain_data(%s, %s)", instrument, expiry)
        return {"instrument": instrument, "expiry": expiry, "data": []}

    @staticmethod
    async def retrieve_price_series(instrument: str, timeframe: str, start: str, end: str) -> list[dict[str, Any]]:
        """Retrieve price series (OHLCV) for an instrument."""
        logger.info("[CopilotTool] retrieve_price_series(%s, %s, %s, %s)", instrument, timeframe, start, end)
        return []

    @staticmethod
    async def retrieve_backtest_results(backtest_id: str) -> dict[str, Any]:
        """Retrieve backtest results by ID."""
        logger.info("[CopilotTool] retrieve_backtest_results(%s)", backtest_id)
        return {"id": backtest_id, "results": {}}

    @staticmethod
    async def retrieve_risk_state() -> dict[str, Any]:
        """Retrieve current risk state."""
        logger.info("[CopilotTool] retrieve_risk_state()")
        return {}

    @staticmethod
    async def retrieve_agent_votes(trade_id: str) -> dict[str, str]:
        """Retrieve agent votes for a specific trade."""
        logger.info("[CopilotTool] retrieve_agent_votes(%s)", trade_id)
        return {}


# ─── Copilot Agent ───────────────────────────────────────────────────────────

class CopilotAgent(BaseAgent):
    """
    Copilot chat agent per §8.

    Explains any decision by tracing signal features + risk checks + agent votes.
    Natural-language → backtest config.
    """

    def __init__(self, config: AgentConfig):
        super().__init__(
            config=config,
            tier=AgentTier("fast", timeout_seconds=15.0, max_concurrent=2, model="fast"),
            system_prompt=(
                "You are a quant trading copilot. Your job is to help traders understand the system, "
                "explain decisions, and configure backtests. You have access to tools: retrieve chain "
                "data, price series, backtest results, risk state, and agent votes. Always be specific, "
                "data-driven, and honest. If you don't know, say so."
            ),
            max_reflection_rounds=0,
        )

        self._chat_prompt = """
You are a quant trading copilot. Answer the user's query helpfully and honestly.

## User Query
{query}

## Context
{context}

## Portfolio State
{portfolio}

## Active Strategies
{strategies}

## Recent Trades
{trades}

Provide a clear, specific answer. Cite data when possible.
"""

        self._tools = CopilotTool()

    async def chat(self, query: CopilotQuery) -> CopilotResponse:
        """Process a user query and return a structured response."""
        # Classify query type
        query_lower = query.user_query.lower()

        if any(kw in query_lower for kw in ["backtest", "run backtest", "test", "config"]):
            return await self._handle_backtest_request(query)
        elif any(kw in query_lower for kw in ["explain", "why", "trade", "position", "pnl"]):
            return await self._handle_trade_explanation(query)
        elif any(kw in query_lower for kw in ["risk", "exposure", "greeks", "var"]):
            return await self._handle_risk_query(query)
        elif any(kw in query_lower for kw in ["chain", "option chain", "oi", "iv"]):
            return await self._handle_chain_query(query)
        elif any(kw in query_lower for kw in ["strategy", "enable", "disable", "param"]):
            return await self._handle_strategy_query(query)
        else:
            return await self._handle_general_query(query)

    async def _handle_backtest_request(self, query: CopilotQuery) -> CopilotResponse:
        """Handle a backtest configuration request."""
        # Parse backtest config from natural language (simplified)
        context = {
            "strategies": query.active_strategies,
            "portfolio": query.portfolio_state,
        }

        messages = [
            {
                "role": "user",
                "content": self._chat_prompt.format(
                    query=query.user_query,
                    context=json.dumps(context, indent=2),
                    portfolio=json.dumps(query.portfolio_state, indent=2, default=str),
                    strategies=", ".join(query.active_strategies) or "none",
                    trades=json.dumps(query.recent_trades[:3], indent=2, default=str) if query.recent_trades else "none",
                ),
            }
        ]

        critique = await self.run_with_reflection(
            messages,
            response_model=CopilotResponse,
        )

        if isinstance(critique.original_output, CopilotResponse):
            critique.original_output.is_backtest_config = True
            return critique.original_output

        return CopilotResponse(
            response="I can help you configure and run a backtest. What strategy and parameters would you like to test?",
            explanation="User requested a backtest. I need more details to configure it.",
            tools_used=[],
            confidence=0.5,
            follow_up=["Which strategy?", "What time period?", "What capital allocation?"],
            backtest_config={},
        )

    async def _handle_trade_explanation(self, query: CopilotQuery) -> CopilotResponse:
        """Handle a trade explanation request."""
        # Retrieve trade details
        trade_id = None
        for kw in query.user_query.split():
            if kw.startswith("trade:") or kw.startswith("#"):
                trade_id = kw.replace("trade:", "").replace("#", "")
                break

        tools_used = ["portfolio_state"]
        trade_data = {}
        if trade_id and query.recent_trades:
            for t in query.recent_trades:
                if t.get("trade_id") == trade_id:
                    trade_data = t
                    tools_used.append("agent_votes")
                    break

        if not trade_data and query.recent_trades:
            trade_data = query.recent_trades[-1]  # Default to most recent

        context = {
            "strategies": query.active_strategies,
            "portfolio": query.portfolio_state,
            "trade": trade_data,
        }

        messages = [
            {
                "role": "user",
                "content": self._chat_prompt.format(
                    query=query.user_query,
                    context=json.dumps(context, indent=2),
                    portfolio=json.dumps(query.portfolio_state, indent=2, default=str),
                    strategies=", ".join(query.active_strategies) or "none",
                    trades=json.dumps([trade_data], indent=2, default=str) if trade_data else "none",
                ),
            }
        ]

        critique = await self.run_with_reflection(
            messages,
            response_model=CopilotResponse,
        )

        if isinstance(critique.original_output, CopilotResponse):
            critique.original_output.is_trade_explanation = True
            critique.original_output.tools_used = tools_used
            return critique.original_output

        return CopilotResponse(
            response="I can explain recent trades. Here's what happened with your last position:",
            explanation="Trade explanation requested. Showing most recent trade.",
            tools_used=tools_used,
            confidence=0.6,
            follow_up=["What about this specific trade?", "Show me the backtest results?"],
            trade_explanation=trade_data,
        )

    async def _handle_risk_query(self, query: CopilotQuery) -> CopilotResponse:
        """Handle a risk state query."""
        risk_state = await self._tools.retrieve_risk_state()
        tools_used = ["risk_state"]

        context = {
            "strategies": query.active_strategies,
            "portfolio": query.portfolio_state,
            "risk": risk_state,
        }

        messages = [
            {
                "role": "user",
                "content": self._chat_prompt.format(
                    query=query.user_query,
                    context=json.dumps(context, indent=2),
                    portfolio=json.dumps(query.portfolio_state, indent=2, default=str),
                    strategies=", ".join(query.active_strategies) or "none",
                    trades=json.dumps(query.recent_trades[:3], indent=2, default=str) if query.recent_trades else "none",
                ),
            }
        ]

        critique = await self.run_with_reflection(
            messages,
            response_model=CopilotResponse,
        )

        if isinstance(critique.original_output, CopilotResponse):
            critique.original_output.tools_used = tools_used
            return critique.original_output

        return CopilotResponse(
            response=f"Current risk state: {json.dumps(risk_state, indent=2, default=str)}",
            explanation="Risk state query. Returning current risk metrics.",
            tools_used=tools_used,
            confidence=0.9,
            follow_up=["What are the risk limits?", "Show me the risk console?"],
        )

    async def _handle_chain_query(self, query: CopilotQuery) -> CopilotResponse:
        """Handle an option chain query."""
        # Extract instrument from query
        instrument = None
        for kw in query.user_query.split():
            if kw.upper() in ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]:
                instrument = kw.upper()
                break

        tools_used = []
        chain_data = {}
        if instrument:
            chain_data = await self._tools.retrieve_chain_data(instrument)
            tools_used.append("chain_data")

        context = {
            "strategies": query.active_strategies,
            "portfolio": query.portfolio_state,
            "chain": chain_data,
        }

        messages = [
            {
                "role": "user",
                "content": self._chat_prompt.format(
                    query=query.user_query,
                    context=json.dumps(context, indent=2),
                    portfolio=json.dumps(query.portfolio_state, indent=2, default=str),
                    strategies=", ".join(query.active_strategies) or "none",
                    trades=json.dumps(query.recent_trades[:3], indent=2, default=str) if query.recent_trades else "none",
                ),
            }
        ]

        critique = await self.run_with_reflection(
            messages,
            response_model=CopilotResponse,
        )

        if isinstance(critique.original_output, CopilotResponse):
            critique.original_output.tools_used = tools_used
            return critique.original_output

        return CopilotResponse(
            response=f"Option chain for {instrument or 'requested instrument'} retrieved.",
            explanation="Option chain query. Returning chain data.",
            tools_used=tools_used,
            confidence=0.7,
            follow_up=[f"What about {instrument or 'this'} chain?", "Show IV rank history?"],
        )

    async def _handle_strategy_query(self, query: CopilotQuery) -> CopilotResponse:
        """Handle a strategy configuration query."""
        context = {
            "active_strategies": query.active_strategies,
            "portfolio": query.portfolio_state,
        }

        messages = [
            {
                "role": "user",
                "content": self._chat_prompt.format(
                    query=query.user_query,
                    context=json.dumps(context, indent=2),
                    portfolio=json.dumps(query.portfolio_state, indent=2, default=str),
                    strategies=", ".join(query.active_strategies) or "none",
                    trades=json.dumps(query.recent_trades[:3], indent=2, default=str) if query.recent_trades else "none",
                ),
            }
        ]

        critique = await self.run_with_reflection(
            messages,
            response_model=CopilotResponse,
        )

        if isinstance(critique.original_output, CopilotResponse):
            return critique.original_output

        return CopilotResponse(
            response=f"Active strategies: {', '.join(query.active_strategies) or 'none'}. What would you like to change?",
            explanation="Strategy query. Showing active strategies.",
            tools_used=[],
            confidence=0.8,
            follow_up=["Enable a new strategy?", "Adjust parameters?"],
        )

    async def _handle_general_query(self, query: CopilotQuery) -> CopilotResponse:
        """Handle a general query."""
        context = {
            "strategies": query.active_strategies,
            "portfolio": query.portfolio_state,
        }

        messages = [
            {
                "role": "user",
                "content": self._chat_prompt.format(
                    query=query.user_query,
                    context=json.dumps(context, indent=2),
                    portfolio=json.dumps(query.portfolio_state, indent=2, default=str),
                    strategies=", ".join(query.active_strategies) or "none",
                    trades=json.dumps(query.recent_trades[:3], indent=2, default=str) if query.recent_trades else "none",
                ),
            }
        ]

        critique = await self.run_with_reflection(
            messages,
            response_model=CopilotResponse,
        )

        if isinstance(critique.original_output, CopilotResponse):
            return critique.original_output

        return CopilotResponse(
            response="I'm your quant trading copilot. I can help with backtests, trade explanations, risk state, and strategy configuration.",
            explanation="General query. Providing overview of capabilities.",
            tools_used=[],
            confidence=0.5,
            follow_up=[
                "Run a backtest",
                "Explain a trade",
                "Check risk state",
                "Show option chain",
            ],
        )


# ─── Registry helper ─────────────────────────────────────────────────────────

def make_copilot_agent(config: AgentConfig) -> CopilotAgent:
    """Factory to create a CopilotAgent with config."""
    return CopilotAgent(config)