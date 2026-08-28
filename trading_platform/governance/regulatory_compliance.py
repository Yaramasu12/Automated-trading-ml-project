"""
trading_platform/governance/regulatory_compliance.py -- Multi-jurisdictional
automated-trading regulatory framework.

Per Architecture Section 06 (Standards - Automated-trading regulation):
Maps every target jurisdiction regime to concrete system requirements.
Each regime has:
  - pre_trade_controls   : what must pass before any order leaves the system
  - post_trade_controls  : record-keeping, reporting, reconciliation
  - kill_switch          : mandatory square-off / circuit-breaker capability
  - conformance_testing  : venue-specific testing requirements
  - throttling           : order-rate / position limits imposed by the venue

Design rule: governance is advisory/gating only. It never executes orders.
The actual broker adapter enforces venue-specific limits.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


class ComplianceStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    PENDING = "pending"


class Jurisdiction(str, Enum):
    """Illustrative jurisdictions from the architecture document.

    IMPORTANT: Educational sampler, NOT legal advice.
    Each regime changes over time; verify per market with qualified
    compliance and legal professionals.
    """
    EU_MIFID2 = "EU_MiFID_II"
    EU_AI_ACT = "EU_AI_Act"
    US_SEC_FINRA = "US_SEC_FINRA"
    US_CFTC = "US_CFTC"
    UK_FCA = "UK_FCA"
    SINGAPORE_MAS = "Singapore_MAS"
    INSEI_SEBI = "IN_SEBI"
    GENERIC = "generic"


@dataclass(frozen=True)
class PreTradeControl:
    name: str
    description: str
    required: bool


@dataclass(frozen=True)
class PostTradeControl:
    name: str
    description: str
    required: bool
    record_fields: tuple = ()


@dataclass(frozen=True)
class ConformanceRequirement:
    venue: str
    conformance_test_url: str
    max_order_rate_per_second: float
    max_position_limit: float
    kill_switch_required: bool


@dataclass
class JurisdictionConfig:
    jurisdiction: Jurisdiction
    name: str
    pre_trade_controls: tuple = ()
    post_trade_controls: tuple = ()
    conformance: tuple = ()
    kill_switch_required: bool = True
    ai_governance_required: bool = False
    human_oversight_required: bool = False
    record_retention_years: int = 7
    notes: str = ""


@dataclass
class ComplianceResult:
    jurisdiction: Jurisdiction
    status: ComplianceStatus
    checks: dict = field(default_factory=dict)
    violations: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def all_mandatory_passed(self) -> bool:
        return self.status == ComplianceStatus.PASS

    def to_dict(self) -> dict:
        return {
            "jurisdiction": self.jurisdiction.value,
            "status": self.status.value,
            "checks": self.checks,
            "violations": self.violations,
            "warnings": self.warnings,
            "checked_at": self.checked_at.isoformat(),
        }


def _default_configs() -> dict:
    """Return the canonical jurisdiction profiles from the architecture."""
    return {
        Jurisdiction.EU_MIFID2: JurisdictionConfig(
            jurisdiction=Jurisdiction.EU_MIFID2,
            name="EU MiFID II / RTS 6 / ESMA 2026",
            pre_trade_controls=(
                PreTradeControl("pre_trade_risk_check", "All orders must pass pre-trade risk check", True),
                PreTradeControl("kill_switch_available", "Kill switch must be operational", True),
                PreTradeControl("order_throttling", "Order rate must respect venue limits", True),
                PreTradeControl("position_limit_check", "Position must not exceed venue limits", True),
            ),
            post_trade_controls=(
                PostTradeControl("order_record", "Every order must be recorded", True, ("order_id", "symbol", "side", "qty", "price", "timestamp", "outcome")),
                PostTradeControl("trade_reconciliation", "Daily trade reconciliation", True),
                PostTradeControl("annual_self_assessment", "Annual MiFID II self-assessment", True),
            ),
            kill_switch_required=True,
            ai_governance_required=True,
            human_oversight_required=True,
            record_retention_years=7,
            notes="ESMA 2026 briefing adds explicit AI/ML governance and accountability",
        ),
        Jurisdiction.EU_AI_ACT: JurisdictionConfig(
            jurisdiction=Jurisdiction.EU_AI_ACT,
            name="EU AI Act",
            pre_trade_controls=(
                PreTradeControl("risk_tier_classification", "AI system must be classified by risk tier", True),
                PreTradeControl("documentation_complete", "Required documentation must be complete", True),
                PreTradeControl("transparency_obligation", "Users must be informed when AI is involved", True),
                PreTradeControl("human_oversight", "Human oversight mechanisms in place", True),
                PreTradeControl("monitoring_active", "Post-market monitoring active", True),
            ),
            post_trade_controls=(
                PostTradeControl("ai_system_log", "AI system decisions logged", True),
                PostTradeControl("drift_report", "Periodic drift monitoring report", True),
            ),
            kill_switch_required=True,
            ai_governance_required=True,
            human_oversight_required=True,
            record_retention_years=10,
            notes="Obligations scale with risk tier; financial trading is high-risk",
        ),
        Jurisdiction.US_SEC_FINRA: JurisdictionConfig(
            jurisdiction=Jurisdiction.US_SEC_FINRA,
            name="US SEC / FINRA Rule 15c3-5",
            pre_trade_controls=(
                PreTradeControl("market_access_risk_control", "Market-access risk controls per 15c3-5", True),
                PreTradeControl("pre_trade_limit_check", "Dollar and position limit checks", True),
                PreTradeControl("kill_switch_mandatory", "Kill switch mandatory for automated trading", True),
                PreTradeControl("supervision_system", "Supervision of algorithms in place", True),
            ),
            post_trade_controls=(
                PostTradeControl("algo_recordkeeping", "Algorithm documentation and changes recorded", True),
                PostTradeControl("market_abuse_prevention", "Market-abuse prevention monitoring", True),
                PostTradeControl("reg_scia_compliance", "Reg SCI for critical systems", True),
            ),
            kill_switch_required=True,
            ai_governance_required=True,
            human_oversight_required=True,
            record_retention_years=6,
            notes="SR 11-7 model risk management; 2026 guidance treats genAI as high-risk",
        ),
        Jurisdiction.US_CFTC: JurisdictionConfig(
            jurisdiction=Jurisdiction.US_CFTC,
            name="US CFTC (Derivatives/Futures)",
            pre_trade_controls=(
                PreTradeControl("derivatives_algo_control", "Derivatives algorithm controls", True),
                PreTradeControl("position_limit_check", "CFTC position limits enforced", True),
                PreTradeControl("risk_limit_enforcement", "Risk limits enforced before order submission", True),
            ),
            post_trade_controls=(
                PostTradeControl("daily_position_report", "Daily position reporting to CFTC", True),
                PostTradeControl("trade_log", "Complete trade log maintained", True),
            ),
            kill_switch_required=True,
            ai_governance_required=False,
            human_oversight_required=True,
            record_retention_years=6,
            notes="Testing and risk limits specific to derivatives markets",
        ),
        Jurisdiction.UK_FCA: JurisdictionConfig(
            jurisdiction=Jurisdiction.UK_FCA,
            name="UK FCA",
            pre_trade_controls=(
                PreTradeControl("algo_controls", "FCA algorithmic trading controls", True),
                PreTradeControl("pre_trade_check", "Pre-trade controls in place", True),
                PreTradeControl("post_trade_check", "Post-trade monitoring", True),
                PreTradeControl("kill_functionality", "Kill switch functionality required", True),
            ),
            post_trade_controls=(
                PostTradeControl("trade_report", "Trade reporting to FCA", True),
                PostTradeControl("testing_documentation", "Testing documentation maintained", True),
            ),
            kill_switch_required=True,
            ai_governance_required=True,
            human_oversight_required=True,
            record_retention_years=5,
            notes="Governance, pre/post-trade controls, testing, and kill-functionality expectations",
        ),
        Jurisdiction.SINGAPORE_MAS: JurisdictionConfig(
            jurisdiction=Jurisdiction.SINGAPORE_MAS,
            name="Singapore MAS (FEAT + Model Risk)",
            pre_trade_controls=(
                PreTradeControl("fairness_check", "Fairness principle check", True),
                PreTradeControl("ethics_review", "Ethics review for AI decisions", True),
                PreTradeControl("accountability_framework", "Accountability framework in place", True),
                PreTradeControl("model_risk_assessment", "Model risk assessment completed", True),
            ),
            post_trade_controls=(
                PostTradeControl("transparency_report", "Transparency documentation", True),
                PostTradeControl("model_governance_log", "Model governance decisions logged", True),
            ),
            kill_switch_required=True,
            ai_governance_required=True,
            human_oversight_required=True,
            record_retention_years=5,
            notes="Fairness, ethics, accountability and transparency principles plus model-risk expectations",
        ),
        Jurisdiction.INSEI_SEBI: JurisdictionConfig(
            jurisdiction=Jurisdiction.INSEI_SEBI,
            name="India SEBI (NSE/BSE)",
            pre_trade_controls=(
                PreTradeControl("sebi_compliance", "SEBI algo trading regulations", True),
                PreTradeControl("pre_trade_risk_gate", "Pre-trade risk gate", True),
                PreTradeControl("kill_switch", "Kill switch mandatory", True),
                PreTradeControl("exchange_conformance", "Exchange-specific conformance", True),
            ),
            post_trade_controls=(
                PostTradeControl("trade_log", "Complete trade log", True),
                PostTradeControl("sebi_report", "SEBI periodic reporting", True),
            ),
            kill_switch_required=True,
            ai_governance_required=False,
            human_oversight_required=True,
            record_retention_years=7,
            notes="Each Indian venue (NSE, BSE) imposes its own conformance testing and throttles",
        ),
    }


class RegulatoryComplianceFramework:
    """Multi-jurisdictional automated-trading compliance checker.

    Maps each jurisdiction regime to concrete system requirements.
    Evaluates the trading system against each regime controls.

    Per Architecture Section 06: This is orientation, not legal advice.
    Compliance and legal teams own the specifics per jurisdiction.
    """

    def __init__(self, configs: dict | None = None):
        self._configs = configs or _default_configs()
        self._results: dict = {}

    def register_config(self, config: JurisdictionConfig) -> None:
        self._configs[config.jurisdiction] = config

    def get_config(self, jurisdiction: Jurisdiction) -> JurisdictionConfig:
        return self._configs[jurisdiction]

    def available_jurisdictions(self) -> list:
        return list(self._configs.keys())

    def check_pre_trade(
        self,
        jurisdiction: Jurisdiction,
        **kwargs,
    ) -> ComplianceResult:
        config = self._configs[jurisdiction]
        checks: dict = {}
        violations: list = []
        for ctrl in config.pre_trade_controls:
            passed = kwargs.get(ctrl.name, True)
            checks[ctrl.name] = passed
            if not passed and ctrl.required:
                violations.append(f"MANDATORY: {ctrl.name} - {ctrl.description}")
        status = ComplianceStatus.PASS if not violations else ComplianceStatus.FAIL
        result = ComplianceResult(
            jurisdiction=jurisdiction,
            status=status,
            checks=checks,
            violations=violations,
            checked_at=datetime.now(timezone.utc),
        )
        self._results[jurisdiction] = result
        return result

    def check_ai_governance(
        self,
        jurisdiction: Jurisdiction,
        **kwargs,
    ) -> ComplianceResult:
        config = self._configs[jurisdiction]
        if not config.ai_governance_required:
            return ComplianceResult(
                jurisdiction=jurisdiction,
                status=ComplianceStatus.PASS,
                checks={"ai_governance": "not_required"},
            )
        checks = {k: v for k, v in kwargs.items()}
        violations = [f"AI governance: {k} not satisfied" for k, v in checks.items() if not v]
        status = ComplianceStatus.PASS if not violations else ComplianceStatus.WARN
        return ComplianceResult(
            jurisdiction=jurisdiction,
            status=status,
            checks=checks,
            violations=violations,
            checked_at=datetime.now(timezone.utc),
        )

    def check_all_jurisdictions(self, **pre_trade_kwargs) -> dict:
        results = {}
        for j in self.available_jurisdictions():
            results[j] = self.check_pre_trade(j, **pre_trade_kwargs)
        self._results.update(results)
        return results

    def get_result(self, jurisdiction: Jurisdiction) -> ComplianceResult | None:
        return self._results.get(jurisdiction)

    def get_all_results(self) -> dict:
        return dict(self._results)

    def to_dict(self) -> dict:
        return {j.value: r.to_dict() for j, r in self._results.items()}

    def save_results(self, path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
        logger.info("Compliance results saved to %s", p)

    @classmethod
    def load_results(cls, path) -> dict:
        p = Path(path)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        results = {}
        for j_str, r_data in data.items():
            try:
                jur = Jurisdiction(j_str)
                results[jur] = ComplianceResult(
                    jurisdiction=jur,
                    status=ComplianceStatus(r_data["status"]),
                    checks=r_data.get("checks", {}),
                    violations=r_data.get("violations", []),
                    warnings=r_data.get("warnings", []),
                    checked_at=datetime.fromisoformat(r_data["checked_at"]),
                )
            except (ValueError, KeyError):
                logger.warning("Skipping invalid compliance result for %s", j_str)
        return results


__all__ = [
    "ComplianceStatus",
    "Jurisdiction",
    "PreTradeControl",
    "PostTradeControl",
    "ConformanceRequirement",
    "JurisdictionConfig",
    "ComplianceResult",
    "RegulatoryComplianceFramework",
]

