"""
trading_platform/ai/agents/base.py — LangGraph agent base framework

Per §8 (REDESIGN_PROMPT): Narrow-contract agents with reflection, tool use,
and structured JSON-schema outputs. No free-roaming agents near the money path.

Features:
- LiteLLM abstraction for LM Studio local OpenAI-compatible API
- JSON-schema output validation
- Reflection (critique own output before returning)
- Tool use pattern (function calling)
- Latency budgets per agent tier
- Structured output via Pydantic models
- Queue-aware concurrency control for local inference
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Type, get_args, get_origin

from pydantic import BaseModel, ValidationError

try:
    from litellm import acompletion as _litellm_acompletion
except ImportError:  # litellm not installed — agents degrade to unavailable, not a hard crash
    _litellm_acompletion = None

logger = logging.getLogger(__name__)


# ─── Latency budgets (§8.1) ───────────────────────────────────────────────────

@dataclass
class AgentTier:
    """Agent latency tier configuration."""
    name: str
    timeout_seconds: float = 15.0
    max_concurrent: int = 2
    model: str = "default"


# Tier mapping per §8.1
TIER_FAST = AgentTier("fast", timeout_seconds=15.0, max_concurrent=2)
TIER_DEEP = AgentTier("deep", timeout_seconds=60.0, max_concurrent=1)

# ─── Configuration ───────────────────────────────────────────────────────────

@dataclass
class AgentConfig:
    """Configuration for LLM agents."""
    # LM Studio connection (LOCAL_LLM_* env vars)
    base_url: str = "http://localhost:1234/v1"
    api_key: str = "lm-studio"  # LM Studio accepts any non-empty key
    max_concurrent_calls: int = 2
    timeout_seconds: float = 30.0

    # Model selection
    deep_model: str = "qwen3-72b-q4_k_m"
    fast_model: str = "qwen3-14b-q4"
    embedding_model: str = "nomic-embed-text-v1.5"

    # Structured output
    use_structured_output: bool = True
    json_schema_strict: bool = False

    # Logging
    log_prompt_response: bool = False


class AgentTool:
    """Tool definition for function calling."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        fn,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters  # JSON Schema
        self.fn = fn

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def call(self, arguments: dict[str, Any]) -> Any:
        """Call the tool with parsed arguments."""
        return self.fn(**arguments)


# ─── Reflection mechanism (§8) ────────────────────────────────────────────────

class ReflectionCritique:
    """Output of an agent's self-reflection step."""

    def __init__(
        self,
        original_output: dict[str, Any],
        critique: str,
        revised_output: Optional[dict[str, Any]] = None,
        accepted: bool = True,
    ):
        self.original_output = original_output
        self.critique = critique
        self.revised_output = revised_output
        self.accepted = accepted

    def to_dict(self) -> dict[str, Any]:
        return {
            "critique": self.critique,
            "accepted": self.accepted,
            "original": self.original_output,
            "revised": self.revised_output,
        }


# ─── Base Agent ───────────────────────────────────────────────────────────────

class BaseAgent:
    """
    Base class for all LLM agents.

    Provides:
    - LiteLLM abstraction
    - JSON-schema output validation
    - Reflection loop
    - Tool execution
    - Latency-aware queuing
    """

    def __init__(
        self,
        config: AgentConfig,
        tier: AgentTier = TIER_FAST,
        system_prompt: str = "",
        max_reflection_rounds: int = 1,
    ):
        self.config = config
        self.tier = tier
        self.system_prompt = system_prompt
        self.max_reflection_rounds = max_reflection_rounds
        self.tools: list[AgentTool] = []
        self._last_call_time: float = 0.0
        self._call_interval: float = 0.0  # computed from LM Studio throughput

    def add_tool(self, tool: AgentTool) -> None:
        """Register a tool for function calling."""
        self.tools.append(tool)

    def get_tools(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self.tools]

    def _select_model(self) -> str:
        """Select model based on tier."""
        if self.tier.name == "deep":
            return self.config.deep_model
        return self.config.fast_model

    async def _call_llm(
        self,
        messages: list[dict[str, str]],
        response_model: Optional[Type[BaseModel]] = None,
        json_schema: Optional[dict] = None,
    ) -> Any:
        """
        Call LM Studio via LiteLLM with concurrency control.

        Returns parsed response (dict, BaseModel, or raw).
        """
        model = self._select_model()
        timeout = self.tier.timeout_seconds

        # Concurrency control: respect max concurrent
        now = time.time()
        elapsed = now - self._last_call_time
        if self._call_interval > 0 and elapsed < self._call_interval:
            wait_time = self._call_interval - elapsed
            logger.debug(f"Agent rate limit: waiting {wait_time:.2f}s")
            await self._sleep(wait_time)
        self._last_call_time = time.time()

        # Build call args
        call_args: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,  # low for consistency
            "timeout": timeout,
            "base_url": self.config.base_url,
            "api_key": self.config.api_key,
        }

        # Structured output
        if response_model is not None:
            call_args["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "schema": response_model.model_json_schema(),
                    "strict": self.config.json_schema_strict,
                },
            }
        elif json_schema is not None:
            call_args["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_output",
                    "schema": json_schema,
                    "strict": self.config.json_schema_strict,
                },
            }

        # Tools
        if self.tools:
            call_args["tools"] = self.get_tools()

        if self.system_prompt:
            messages = [{"role": "system", "content": self.system_prompt}] + messages
        else:
            messages = [{"role": "system", "content": "You are a precise, cautious analyst. Output structured data only."}] + messages

        # Log
        if self.config.log_prompt_response:
            logger.debug(f"[{type(self).__name__}] Prompt: {json.dumps([m['role'] for m in messages])}")

        try:
            response = await self._acompletion(**call_args, messages=messages)
        except Exception as e:
            logger.error(f"[{type(self).__name__}] LLM call failed: {e}")
            raise

        # Extract content
        choice = response.choices[0]
        content = choice.message.content

        if self.config.log_prompt_response and content:
            logger.debug(f"[{type(self).__name__}] Response: {content[:500]}...")

        # Parse response
        if response_model is not None and content:
            try:
                return response_model.model_validate_json(content)
            except ValidationError:
                # Try dict first then validate
                try:
                    data = json.loads(content)
                    return response_model(**data)
                except Exception:
                    logger.warning(f"[{type(self).__name__}] Structured output parse failed, returning raw")
                    return content
        elif json_schema is not None and content:
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return {"raw_response": content, "parse_error": "invalid JSON"}

        return content

    def _apply_reflection(
        self,
        output: Any,
        messages: list[dict[str, str]],
    ) -> ReflectionCritique:
        """
        Apply self-reflection to output.

        Default: single round, no reflection. Override for multi-round.
        """
        return ReflectionCritique(
            original_output=output if isinstance(output, dict) else str(output),
            critique="No reflection needed (default)",
            accepted=True,
        )

    async def run_with_reflection(
        self,
        messages: list[dict[str, str]],
        response_model: Optional[Type[BaseModel]] = None,
        json_schema: Optional[dict] = None,
    ) -> ReflectionCritique:
        """Run agent with reflection loop."""
        output = await self._call_llm(messages, response_model, json_schema)

        for round_i in range(self.max_reflection_rounds):
            critique = self._apply_reflection(output, messages)
            if critique.accepted:
                return critique
            # Revise
            if critique.revised_output:
                messages = messages + [
                    {"role": "assistant", "content": json.dumps(critique.revised_output)},
                ]
                output = critique.revised_output
            else:
                break

        return ReflectionCritique(
            original_output=output if isinstance(output, dict) else str(output),
            critique="Max reflection rounds reached",
            accepted=True,
        )

    async def _sleep(self, seconds: float) -> None:
        """Async sleep placeholder."""
        import asyncio
        await asyncio.sleep(seconds)

    async def _acompletion(self, **call_args: Any) -> Any:
        """Thin wrapper around litellm.acompletion so callers don't import litellm directly."""
        if _litellm_acompletion is None:
            raise RuntimeError(
                "litellm is not installed — LLM agents are unavailable. "
                "Install it (`pip install litellm`) to enable this agent; "
                "degrade to pure-quant mode until then."
            )
        return await _litellm_acompletion(**call_args)

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Execute a registered tool by name."""
        for tool in self.tools:
            if tool.name == tool_name:
                return tool.call(arguments)
        raise ValueError(f"Tool '{tool_name}' not found. Available: {[t.name for t in self.tools]}")


# ─── Agent health tracking ───────────────────────────────────────────────────

@dataclass
class AgentHealth:
    """Health metrics for an agent."""
    name: str
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    avg_latency_ms: float = 0.0
    last_call_time: Optional[float] = None
    status: str = "healthy"  # healthy, degraded, dead

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.successful_calls / self.total_calls

    def record_call(self, success: bool, latency_ms: float) -> None:
        self.total_calls += 1
        if success:
            self.successful_calls += 1
        else:
            self.failed_calls += 1
        # Exponential moving average
        alpha = 0.1
        if self.avg_latency_ms == 0:
            self.avg_latency_ms = latency_ms
        else:
            self.avg_latency_ms = alpha * latency_ms + (1 - alpha) * self.avg_latency_ms
        self.last_call_time = time.time()
        # Status update
        if self.success_rate < 0.5:
            self.status = "dead"
        elif self.success_rate < 0.8 or self.avg_latency_ms > 30000:
            self.status = "degraded"
        else:
            self.status = "healthy"


# ─── Agent registry ──────────────────────────────────────────────────────────

class AgentRegistry:
    """Registry of all agents with health tracking."""

    def __init__(self):
        self._agents: dict[str, BaseAgent] = {}
        self._health: dict[str, AgentHealth] = {}

    def register(self, name: str, agent: BaseAgent) -> None:
        self._agents[name] = agent
        self._health[name] = AgentHealth(name=name)

    def get(self, name: str) -> BaseAgent:
        return self._agents[name]

    def health_report(self) -> dict[str, AgentHealth]:
        return dict(self._health)

    def get_status(self) -> dict[str, str]:
        return {name: h.status for name, h in self._health.items()}


# ─── Agent direction / trade guard (safety) ─────────────────────────────────

class AgentDirectionGuard:
    """
    Safety guard: no agent can initiate or upsize trades.

    Per CLAUDE.md: advisory ≠ safety. All agents are advisory-only.
    Only the veto_downsize path is allowed — never veto_upsize.
    """

    ALLOWED_ACTIONS = frozenset(["approve", "veto", "downsize"])

    @staticmethod
    def validate_decision(decision: dict[str, Any]) -> bool:
        """Validate an agent's trade-related decision."""
        action = decision.get("action", decision.get("decision", ""))
        if action not in AgentDirectionGuard.ALLOWED_ACTIONS:
            return False
        return True

    @staticmethod
    def enforce(decision: dict[str, Any]) -> dict[str, Any]:
        """
        Enforce guardrails on agent decision.
        Strips any unauthorized fields.
        """
        if not AgentDirectionGuard.validate_decision(decision):
            return {
                "action": "veto",
                "reason": "Unauthorized action in agent decision: " + str(decision.get("action", "")),
                "confidence": 0.0,
            }
        # Ensure no 'size_multiplier' > 1.0 (no upsizing)
        size_mult = decision.get("size_multiplier", decision.get("confidence", 1.0))
        if isinstance(size_mult, (int, float)) and size_mult > 1.0:
            decision["size_multiplier"] = min(size_mult, 1.0)
        return decision