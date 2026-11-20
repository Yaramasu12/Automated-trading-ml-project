from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any


_SENSITIVE_KEYS = {
    "authorization",
    "x-privatekey",
    "x-api-key",
    "x-feed-token",
    "jwtToken".lower(),
    "refreshToken".lower(),
    "feedToken".lower(),
    "access_token",
    "auth_token",
    "api_key",
    "privateKey".lower(),
    "password",
    "pin",
    "totp",
}

_KEY_PATTERN = "|".join(re.escape(key) for key in sorted(_SENSITIVE_KEYS, key=len, reverse=True))
_QUOTED_PAIR_RE = re.compile(
    rf"(?P<prefix>['\"]?(?:{_KEY_PATTERN})['\"]?\s*:\s*)"
    r"(?P<value>'[^']*'|\"[^\"]*\"|[^,}\]\s]+)",
    flags=re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", flags=re.IGNORECASE)


def redact_secret_text(value: str) -> str:
    """Redact likely credential fields in third-party exception/log strings."""

    def replace_pair(match: re.Match[str]) -> str:
        raw = match.group("value")
        if raw.startswith("'") and raw.endswith("'"):
            replacement = "'[REDACTED]'"
        elif raw.startswith('"') and raw.endswith('"'):
            replacement = '"[REDACTED]"'
        else:
            replacement = "[REDACTED]"
        return f"{match.group('prefix')}{replacement}"

    value = _QUOTED_PAIR_RE.sub(replace_pair, value)
    return _BEARER_RE.sub(r"\1[REDACTED]", value)


def _redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secret_text(value)
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else _redact_obj(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_obj(item) for item in value)
    if isinstance(value, list):
        return [_redact_obj(item) for item in value]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return type(value)(_redact_obj(item) for item in value)
    return value


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_obj(record.msg)
        if record.args:
            record.args = _redact_obj(record.args)
        return True


_INSTALLED = False
_ORIGINAL_FACTORY = logging.getLogRecordFactory()


def install_secret_redaction() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = _ORIGINAL_FACTORY(*args, **kwargs)
        record.msg = _redact_obj(record.msg)
        if record.args:
            record.args = _redact_obj(record.args)
        return record

    logging.setLogRecordFactory(record_factory)

    redaction_filter = SecretRedactionFilter()
    logging.getLogger().addFilter(redaction_filter)
    logging.getLogger("logzero_default").addFilter(redaction_filter)

    try:
        from logzero import logger as logzero_logger  # type: ignore[import]

        logzero_logger.addFilter(redaction_filter)
        logzero_logger.setLevel(logging.WARNING)
    except Exception:
        pass

    _INSTALLED = True


# ── Swallowed-exception observability (review finding #3) ──────────────────────
# In a trading system, `except Exception: pass` hides fill/persistence/broker
# failures. Where a swallow is genuinely intentional (must not disrupt a hot
# path), route it through note_swallowed() so it is at least LOGGED and COUNTED
# instead of vanishing. The counter is surfaced in /health for observability.

import logging as _logging

_SWALLOWED = {"count": 0}
# Per-component tallies. The bare count told an operator that SOMETHING was
# swallowing exceptions but never what — during the 2026-08-10 session /health
# reported "4 swallowed errors" with no way to identify the source, and the
# WARNING logs alone did not narrow it either. A signal that fires without
# saying where is barely more actionable than no signal at all.
_SWALLOWED_BY_COMPONENT: dict[str, int] = {}
_swallow_logger = _logging.getLogger("trading_platform.swallowed")


def note_swallowed(component: str, exc: BaseException) -> None:
    """Record a deliberately-swallowed exception (log + count). Never raises.

    Logged at WARNING, not DEBUG: nothing in this app's own startup calls
    logging.basicConfig, so the root logger sits at Python's implicit default
    (WARNING) — every note_swallowed() call was silently invisible in
    container logs regardless of how many fired (confirmed 2026-07-28: 57
    swallowed exceptions, zero corresponding log lines). The /health counter
    still incremented, so this wasn't a correctness bug, just a total
    monitoring blind spot on exactly the mechanism meant to prevent one.

    Redaction is applied explicitly here rather than relying on the
    SecretRedactionFilter installed on the root logger above: that filter is
    attached via logger.addFilter(), which only runs when the ROOT logger
    itself is asked to log a record directly. Records from this CHILD logger
    reach root's handlers via propagation, which checks HANDLER-level
    filters, not ancestor LOGGER-level ones — so that filter may not actually
    apply to what note_swallowed logs. Redacting the exception text directly
    here is correct regardless of that propagation subtlety.
    """
    _SWALLOWED["count"] += 1
    try:
        _SWALLOWED_BY_COMPONENT[component] = _SWALLOWED_BY_COMPONENT.get(component, 0) + 1
    except Exception:
        pass
    try:
        _swallow_logger.warning(
            "swallowed[%s]: %s: %s", component, type(exc).__name__, redact_secret_text(str(exc))
        )
    except Exception:
        pass


def swallowed_errors_by_component() -> dict[str, int]:
    """Which components swallowed exceptions, and how many each — so the
    /health counter points somewhere instead of just alarming."""
    return dict(_SWALLOWED_BY_COMPONENT)


def swallowed_error_count() -> int:
    """Total exceptions routed through note_swallowed() since process start."""
    return _SWALLOWED["count"]


# OBS-2: the subset of note_swallowed() components where a swallow means a
# money-path WRITE/EXEC fault — a position/exit/fill failed to PERSIST,
# unresolved-order remediation failed to record its resolving event, or the
# kill switch failed to restore on boot. These are "investigate now", unlike
# the constant benign background swallows the runtime-monitor comment calls
# out (absent news feed, AI-council stub fallbacks, vector memory, TCA
# analytics, the fail-closed data fallback that SKIPS rather than persists).
# Only these flip the top-line operational_status to DEGRADED; the full
# per-component breakdown (swallowed_by_component) still shows everything.
MONEY_PATH_CRITICAL_SWALLOW_COMPONENTS = frozenset({
    "save_exit_plan",                   # exit plan failed to persist
    "settle_expired.save_positions",    # post-settlement position write lost
    "settle_expired.apply_fill",        # expiry settlement fill not applied
    "square_off.save_risk_event",       # EOD square-off risk event unrecorded
    "unresolved_remediation.append",    # resolving OMS event failed to write
    "restore_state.rearm_exit_plan",    # an open position's exit not re-armed
    "runtime.init.kill_switch_restore", # kill-switch state lost across restart
})


def critical_swallowed_error_count() -> int:
    """Count of swallows from money-path-critical components (OBS-2).

    Sticky until process restart on purpose: a failed write of money state is
    an investigate-now condition, not a transient to auto-clear — the same
    semantics as unresolved_orders in the operational snapshot. Resets when
    the process restarts (the swallow counters are in-memory)."""
    by_component = swallowed_errors_by_component()
    return sum(
        count for component, count in by_component.items()
        if component in MONEY_PATH_CRITICAL_SWALLOW_COMPONENTS
    )
