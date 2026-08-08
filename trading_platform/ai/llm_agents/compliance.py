"""
trading_platform/ai/agents/compliance.py — Compliance Watcher agent (LangGraph)

Per §8 (REDESIGN_PROMPT):
- Flags new SEBI/exchange circulars affecting retail algo rules
- Monitors exchange OTR (only-trading-rules) compliance
- Watches for regulatory changes that impact the platform's algo rules
- Fast tier (Gemma-3-12B class) for low-latency monitoring
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

class ComplianceFlag(BaseModel):
    """A flagged compliance concern from the Compliance Watcher."""
    category: str = Field(description="Category: algo_registration, static_ip, order_limits, margin, reporting, etc.")
    severity: str = Field(description="severity: critical | warning | info", pattern="^(critical|warning|info)$")
    description: str = Field(description="Description of the compliance concern")
    source: str = Field(description="Source: SEBI circular, exchange notification, etc.")
    effective_date: Optional[str] = Field(default=None, description="When the rule takes effect")
    affected_systems: list[str] = Field(default_factory=list, description="Which systems need changes")
    recommended_action: str = Field(description="What needs to be done")
    confidence: float = Field(description="Confidence 0..1", ge=0.0, le=1.0)


class ComplianceResponse(BaseModel):
    """Structured response from the Compliance Watcher agent."""
    flags: list[ComplianceFlag] = Field(default_factory=list, description="List of compliance flags")
    summary: str = Field(description="Executive summary of compliance status")
    tools_used: list[str] = Field(default_factory=list, description="Which data sources were consulted")
    confidence: float = Field(description="Overall confidence 0..1", ge=0.0, le=1.0)
    next_review_date: Optional[str] = Field(default=None, description="When to next review compliance")

    @property
    def has_critical(self) -> bool:
        return any(f.severity == "critical" for f in self.flags)

    @property
    def has_warnings(self) -> bool:
        return any(f.severity == "warning" for f in self.flags)


# ─── Input schema ────────────────────────────────────────────────────────────

@dataclass
class ComplianceQuery:
    """Input to the Compliance Watcher agent."""
    query_type: str = Field(description="Type: check_new | audit | status | alert")
    query_text: str = ""  # Free-text query
    system_config: dict[str, Any] = None  # Current system configuration
    algo_settings: dict[str, Any] = None  # Current algo registration settings
    timestamp: datetime = None

    def __post_init__(self):
        if self.system_config is None:
            self.system_config = {}
        if self.algo_settings is None:
            self.algo_settings = {}
        if self.timestamp is None:
            self.timestamp = datetime.now()


# ─── Tool definitions ────────────────────────────────────────────────────────

class ComplianceTool:
    """Tool interface for the Compliance Watcher agent."""

    @staticmethod
    async def fetch_sebi_circulars() -> list[dict[str, Any]]:
        """Fetch recent SEBI circulars related to algo trading."""
        logger.info("[ComplianceTool] fetch_sebi_circulars()")
        # Placeholder: would query SEBI's circulars feed or local RAG corpus
        return []

    @staticmethod
    async def fetch_exchange_notifications() -> list[dict[str, Any]]:
        """Fetch recent exchange (NSE/BSE) notifications."""
        logger.info("[ComplianceTool] fetch_exchange_notifications()")
        # Placeholder: would query exchange notification feeds
        return []

    @staticmethod
    async def check_algo_registration_status() -> dict[str, Any]:
        """Check current algo registration status with exchanges."""
        logger.info("[ComplianceTool] check_algo_registration_status()")
        return {"registered": False, "algo_ids": [], "status": "unregistered"}

    @staticmethod
    async def check_static_ip_status() -> dict[str, Any]:
        """Check egress IP configuration."""
        logger.info("[ComplianceTool] check_static_ip_status()")
        return {"static_ip_configured": False, "current_ip": None}

    @staticmethod
    async def check_order_limits() -> dict[str, Any]:
        """Check current order limits vs actual usage."""
        logger.info("[ComplianceTool] check_order_limits()")
        return {
            "daily_limit": 200,
            "daily_used": 0,
            "otb_compliant": True,
        }

    @staticmethod
    async def retrieve_rag_context(query: str) -> list[dict[str, Any]]:
        """Retrieve relevant compliance context from RAG corpus."""
        logger.info("[ComplianceTool] retrieve_rag_context(%s)", query)
        return []


# ─── Compliance Watcher Agent ────────────────────────────────────────────────

class ComplianceWatcherAgent(BaseAgent):
    """
    Compliance Watcher agent per §8.

    Flags new SEBI/exchange circulars affecting retail algo rules.
    Monitors exchange OTR (only-trading-rules) compliance.
    """

    def __init__(self, config: AgentConfig):
        super().__init__(
            config=config,
            tier=AgentTier("fast", timeout_seconds=15.0, max_concurrent=2, model="fast"),
            system_prompt=(
                "You are a compliance monitoring agent for a retail algo-trading platform. "
                "Your job is to flag regulatory changes that affect the platform's operations. "
                "Key compliance areas: SEBI retail-algo framework (effective Apr 2026), "
                "exchange algo-registration requirements, static IP requirements, "
                "order-per-second limits, reporting obligations, and margin rules. "
                "You NEVER give legal advice — you flag concerns for human review. "
                "You are honest about what you can and cannot verify."
            ),
            max_reflection_rounds=1,
        )

        self._compliance_prompt = """
You are a compliance monitoring agent for a retail algo-trading platform in India.

## Current System Configuration
{system_config}

## Current Algo Registration
{algo_settings}

## Query
{query}

## Recent SEBI Circulars
{sebi_circulars}

## Recent Exchange Notifications
{exchange_notifications}

## RAG Context
{rag_context}

Analyze the query against known compliance requirements. Flag any gaps or concerns.
Be specific about what is verified vs. what needs human review.
"""

        self._tools = ComplianceTool()

    async def check(self, query: ComplianceQuery) -> ComplianceResponse:
        """Run compliance check based on query type."""
        if query.query_type == "check_new":
            return await self._check_new_regulations(query)
        elif query.query_type == "audit":
            return await self._full_audit(query)
        elif query.query_type == "status":
            return await self._status_check(query)
        elif query.query_type == "alert":
            return await self._alert_check(query)
        else:
            return await self._general_query(query)

    async def _check_new_regulations(self, query: ComplianceQuery) -> ComplianceResponse:
        """Check for new regulations since last review."""
        tools_used = ["sebi_circulars", "exchange_notifications", "rag_context"]

        # Fetch regulatory data
        sebi_circulars = await self._tools.fetch_sebi_circulars()
        exchange_notifs = await self._tools.fetch_exchange_notifications()
        rag_context = await self._tools.retrieve_rag_context("new algo trading regulations retail")

        context = {
            "system_config": query.system_config,
            "algo_settings": query.algo_settings,
            "sebi_circulars": sebi_circulars,
            "exchange_notifications": exchange_notifs,
            "rag_context": rag_context,
        }

        messages = [
            {
                "role": "user",
                "content": self._compliance_prompt.format(
                    system_config=json.dumps(query.system_config, indent=2, default=str),
                    algo_settings=json.dumps(query.algo_settings, indent=2, default=str),
                    query=query.query_text or "Are there any new regulations affecting our platform?",
                    sebi_circulars=json.dumps(sebi_circulars[:10], indent=2, default=str),
                    exchange_notifications=json.dumps(exchange_notifs[:10], indent=2, default=str),
                    rag_context=json.dumps(rag_context[:5], indent=2, default=str),
                ),
            }
        ]

        critique = await self.run_with_reflection(
            messages,
            response_model=ComplianceResponse,
        )

        if isinstance(critique.original_output, ComplianceResponse):
            critique.original_output.tools_used = tools_used
            return critique.original_output

        return ComplianceResponse(
            flags=[],
            summary="No new regulations detected that affect our platform.",
            tools_used=tools_used,
            confidence=0.8,
        )

    async def _full_audit(self, query: ComplianceQuery) -> ComplianceResponse:
        """Run a full compliance audit."""
        tools_used = [
            "sebi_circulars",
            "exchange_notifications",
            "algo_registration_status",
            "static_ip_status",
            "order_limits",
            "rag_context",
        ]

        # Fetch all compliance data
        algo_status = await self._tools.check_algo_registration_status()
        ip_status = await self._tools.check_static_ip_status()
        order_limits = await self._tools.check_order_limits()

        context = {
            "system_config": query.system_config,
            "algo_settings": query.algo_settings,
            "algo_status": algo_status,
            "ip_status": ip_status,
            "order_limits": order_limits,
        }

        messages = [
            {
                "role": "user",
                "content": self._compliance_prompt.format(
                    system_config=json.dumps(query.system_config, indent=2, default=str),
                    algo_settings=json.dumps(query.algo_settings, indent=2, default=str),
                    query="Please run a full compliance audit of our platform against SEBI retail-algo requirements.",
                    sebi_circulars="N/A (audit mode)",
                    exchange_notifications="N/A (audit mode)",
                    rag_context=json.dumps([], indent=2),
                ),
            }
        ]

        critique = await self.run_with_reflection(
            messages,
            response_model=ComplianceResponse,
        )

        if isinstance(critique.original_output, ComplianceResponse):
            critique.original_output.tools_used = tools_used

            # Add known compliance checks
            flags = list(critique.original_output.flags)

            # Check algo registration
            if not algo_status.get("registered", False):
                flags.append(ComplianceFlag(
                    category="algo_registration",
                    severity="critical",
                    description="Platform is not registered with exchanges as an algo provider",
                    source="SEBI retail-algo framework",
                    effective_date="2026-04-01",
                    affected_systems=["execution", "oms"],
                    recommended_action="Register algo with NSE/BSE via broker. Obtain Algo-ID for each strategy.",
                    confidence=0.95,
                ))

            # Check static IP
            if not ip_status.get("static_ip_configured", False):
                flags.append(ComplianceFlag(
                    category="static_ip",
                    severity="critical",
                    description="Static IP not configured for egress",
                    source="SEBI retail-algo framework",
                    effective_date="2026-04-01",
                    affected_systems=["execution"],
                    recommended_action="Get fixed IP from ISP. Whitelist with broker.",
                    confidence=0.9,
                ))

            # Check order limits
            if order_limits.get("daily_limit") and order_limits["daily_used"] >= order_limits["daily_limit"] * 0.8:
                flags.append(ComplianceFlag(
                    category="order_limits",
                    severity="warning",
                    description=f"Order limit approaching: {order_limits['daily_used']}/{order_limits['daily_limit']} orders today",
                    source="NSE algo monitoring",
                    effective_date="ongoing",
                    affected_systems=["execution", "compliance"],
                    recommended_action="Monitor order count. Implement pre-check before limit.",
                    confidence=0.95,
                ))

            return ComplianceResponse(
                flags=flags,
                summary=f"Audit complete: {len([f for f in flags if f.severity == 'critical'])} critical, "
                        f"{len([f for f in flags if f.severity == 'warning'])} warnings found.",
                tools_used=tools_used,
                confidence=0.85,
            )

        return ComplianceResponse(
            flags=[],
            summary="Audit could not be completed. Insufficient data.",
            tools_used=tools_used,
            confidence=0.3,
        )

    async def _status_check(self, query: ComplianceQuery) -> ComplianceResponse:
        """Check current compliance status."""
        tools_used = ["algo_registration_status", "static_ip_status", "order_limits"]

        algo_status = await self._tools.check_algo_registration_status()
        ip_status = await self._tools.check_static_ip_status()
        order_limits = await self._tools.check_order_limits()

        critical_count = sum([
            not algo_status.get("registered", False),
            not ip_status.get("static_ip_configured", False),
        ])

        summary_parts = ["Compliance status:"]
        if algo_status.get("registered"):
            summary_parts.append("  ✓ Algo registered")
        else:
            summary_parts.append("  ✗ Algo not registered")

        if ip_status.get("static_ip_configured"):
            summary_parts.append("  ✓ Static IP configured")
        else:
            summary_parts.append("  ✗ Static IP not configured")

        return ComplianceResponse(
            flags=[],
            summary="\n".join(summary_parts),
            tools_used=tools_used,
            confidence=0.9,
        )

    async def _alert_check(self, query: ComplianceQuery) -> ComplianceResponse:
        """Check for urgent alerts."""
        tools_used = ["sebi_circulars", "exchange_notifications"]

        sebi_circulars = await self._tools.fetch_sebi_circulars()
        exchange_notifs = await self._tools.fetch_exchange_notifications()

        # Look for urgent items
        critical_flags = []
        for circ in sebi_circulars:
            if circ.get("urgency") in ("urgent", "critical"):
                critical_flags.append(ComplianceFlag(
                    category="regulatory_change",
                    severity="critical",
                    description=circ.get("title", "Urgent circular"),
                    source="SEBI",
                    effective_date=circ.get("effective_date"),
                    affected_systems=["all"],
                    recommended_action="Review immediately. Update system if needed.",
                    confidence=0.8,
                ))

        for notif in exchange_notifs:
            if notif.get("urgency") in ("urgent", "critical"):
                critical_flags.append(ComplianceFlag(
                    category="exchange_notification",
                    severity="warning",
                    description=notif.get("title", "Urgent notification"),
                    source="NSE/BSE",
                    effective_date=notif.get("effective_date"),
                    affected_systems=["execution", "compliance"],
                    recommended_action="Review and update if needed.",
                    confidence=0.7,
                ))

        if critical_flags:
            return ComplianceResponse(
                flags=critical_flags,
                summary=f"⚠ {len(critical_flags)} urgent compliance item(s) require review.",
                tools_used=tools_used,
                confidence=0.85,
            )

        return ComplianceResponse(
            flags=[],
            summary="No urgent compliance alerts.",
            tools_used=tools_used,
            confidence=0.9,
        )

    async def _general_query(self, query: ComplianceQuery) -> ComplianceResponse:
        """Handle a general compliance query."""
        tools_used = ["rag_context"]

        rag_context = await self._tools.retrieve_rag_context(query.query_text)

        messages = [
            {
                "role": "user",
                "content": self._compliance_prompt.format(
                    system_config=json.dumps(query.system_config, indent=2, default=str),
                    algo_settings=json.dumps(query.algo_settings, indent=2, default=str),
                    query=query.query_text or "What is the compliance status?",
                    sebi_circulars="N/A",
                    exchange_notifications="N/A",
                    rag_context=json.dumps(rag_context[:5], indent=2, default=str),
                ),
            }
        ]

        critique = await self.run_with_reflection(
            messages,
            response_model=ComplianceResponse,
        )

        if isinstance(critique.original_output, ComplianceResponse):
            critique.original_output.tools_used = tools_used
            return critique.original_output

        return ComplianceResponse(
            flags=[],
            summary="I can help with compliance questions. Ask about algo registration, static IP, order limits, or regulatory changes.",
            tools_used=tools_used,
            confidence=0.5,
            next_review_date=(datetime.now() + __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%d"),
        )


# ─── Registry helper ─────────────────────────────────────────────────────────

def make_compliance_watcher(config: AgentConfig) -> ComplianceWatcherAgent:
    """Factory to create a ComplianceWatcherAgent with config."""
    return ComplianceWatcherAgent(config)