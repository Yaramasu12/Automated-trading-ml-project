"""
Risk layer — pre-trade gates, kill-switch, compliance rules.

Every order passes through this layer before reaching a venue.
It is a hard gate, not an afterthought.
"""

from risk.gates import (
    OrderIntent,
    RiskGate,
    RiskResult,
    RiskLimits,
    create_default_limits,
)
from risk.kill_switch import (
    KillSwitch,
    KillSwitchConfig,
)
from risk.compliance import (
    ComplianceChecker,
    ComplianceConfig,
    ComplianceResult,
)
from risk.audit import (
    AuditLogger,
    AuditEntry,
)

__all__ = [
    "OrderIntent",
    "RiskGate",
    "RiskResult",
    "RiskLimits",
    "create_default_limits",
    "KillSwitch",
    "KillSwitchConfig",
    "ComplianceChecker",
    "ComplianceConfig",
    "ComplianceResult",
    "AuditLogger",
    "AuditEntry",
]