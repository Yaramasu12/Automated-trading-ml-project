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
_swallow_logger = _logging.getLogger("trading_platform.swallowed")


def note_swallowed(component: str, exc: BaseException) -> None:
    """Record a deliberately-swallowed exception (log + count). Never raises."""
    _SWALLOWED["count"] += 1
    try:
        _swallow_logger.debug("swallowed[%s]: %s: %s", component, type(exc).__name__, exc)
    except Exception:
        pass


def swallowed_error_count() -> int:
    """Total exceptions routed through note_swallowed() since process start."""
    return _SWALLOWED["count"]
