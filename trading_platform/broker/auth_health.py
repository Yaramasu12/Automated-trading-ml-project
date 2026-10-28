"""Process-wide Angel One authentication health + circuit breaker.

WHY THIS EXISTS (2026-10-27 audit):
Before this module, an Angel One auth failure (``AB1050`` — "Invalid totp and
client combination") was logged as a buried WARNING in whichever login path
hit it (broker orders, historical candles, or the live feed), ``/health`` kept
reporting ``HEALTHY``, and every strategy scan kept hammering the login
endpoint. On 2026-10-17 a rotated/stale TOTP secret therefore produced a
*silent* ~10-day data blackout: with no broker session the system could not
fetch VIX / option-chain / candle data, so the disciplined short-vol gate could
never build a FINNIFTY condor, so zero orders were placed — and nobody was
alerted. See reports/ and the trading-projects memory.

This module gives all three login paths ONE shared, thread-safe place to:
  * record each login outcome, classifying genuine auth failures (which a retry
    cannot fix) apart from transient ones (rate-limit / network);
  * OPEN a circuit breaker after N consecutive auth failures so a known-bad
    credential stops flooding Angel One's login endpoint — that endpoint has a
    far stricter quota than data calls and will rate-limit / flag the account;
  * fire exactly ONE CRITICAL operator alert on the transition into the failing
    state (and one recovery note), not one per retry; and
  * expose a snapshot for ``/health`` so a broker outage can never again look
    ``HEALTHY``.

It stores and logs OUTCOMES only — never credentials. The ``last_error`` text
is the broker's own (non-secret) response message, truncated.

Config (read from the environment at call time, matching the codebase idiom):
  ANGEL_AUTH_BREAKER_ENABLED           default "true"
  ANGEL_AUTH_BREAKER_THRESHOLD         default 3    (consecutive auth failures)
  ANGEL_AUTH_BREAKER_COOLDOWN_SECONDS  default 900  (15 min between trial logins)
Alerting reuses the existing monitoring channel:
  MONITORING_TELEGRAM_ENABLED / MONITORING_TELEGRAM_BOT_TOKEN / _CHAT_ID
  ANGEL_AUTH_ALERT_WEBHOOK_URL         optional generic JSON webhook
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class BrokerAuthCircuitOpen(RuntimeError):
    """Raised by ``before_login_attempt`` when the auth breaker is open.

    Subclasses RuntimeError so the existing ``except Exception`` / ``except
    RuntimeError`` handlers around every login call treat it as an ordinary
    (recoverable) login failure instead of crashing the caller."""


# Broker-side substrings that mean the credential itself is wrong — a retry with
# the same secret cannot fix these, so they (and only these) trip the breaker.
_AUTH_MARKERS: tuple[str, ...] = (
    "ab1050",                 # Angel One: "Invalid totp and client combination"
    "invalid totp",
    "invalid client",
    "invalid credential",
    "invalid session",
    "invalid refresh",
    "token is invalid",
    "unauthor",               # unauthorised / unauthorized
)

# Transient substrings — recorded as the last error but never counted toward the
# auth breaker (retrying later legitimately helps).
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "rate",                   # "exceeding access rate", "rate_limited"
    "timed out",
    "timeout",
    "connection",
    "temporarily",
    "max retries",
)


def is_auth_error(text: str | None) -> bool:
    """True when the broker message indicates a credential/auth rejection
    (not a transient rate-limit or network blip)."""
    if not text:
        return False
    low = text.lower()
    if any(m in low for m in _TRANSIENT_MARKERS) and not any(m in low for m in _AUTH_MARKERS):
        return False
    return any(m in low for m in _AUTH_MARKERS)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class _State:
    consecutive_auth_failures: int = 0
    total_auth_failures: int = 0
    total_successes: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    breaker_open_until: float | None = None   # time.monotonic() deadline
    alerted_open: bool = False                 # fire the CRITICAL alert once


class _AuthHealth:
    """Thread-safe singleton. Login paths run in background threads (scheduler
    executor, feed thread), so every field is guarded by one lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._s = _State()

    # ---- breaker gate -------------------------------------------------------

    def before_login_attempt(self) -> None:
        """Call at the very top of a login. Raises ``BrokerAuthCircuitOpen`` if
        the breaker is open and the cooldown has not elapsed. When the cooldown
        has elapsed it allows ONE trial login (half-open) and pushes the
        deadline forward so concurrent callers do not stampede the endpoint."""
        if not _env_flag("ANGEL_AUTH_BREAKER_ENABLED", True):
            return
        with self._lock:
            deadline = self._s.breaker_open_until
            if deadline is None:
                return
            now = time.monotonic()
            if now < deadline:
                remaining = deadline - now
                raise BrokerAuthCircuitOpen(
                    f"Angel One auth circuit OPEN after "
                    f"{self._s.consecutive_auth_failures} consecutive auth failures "
                    f"(last error: {self._s.last_error}); next trial login in "
                    f"{remaining:.0f}s. Refresh ANGEL_ONE_TOTP_SECRET / credentials."
                )
            # Cooldown elapsed: permit one half-open trial, re-arm the deadline.
            cooldown = _env_int("ANGEL_AUTH_BREAKER_COOLDOWN_SECONDS", 900)
            self._s.breaker_open_until = now + cooldown

    # ---- outcome recording --------------------------------------------------

    def record_success(self) -> None:
        with self._lock:
            was_failing = self._s.consecutive_auth_failures > 0 or self._s.breaker_open_until is not None
            self._s.consecutive_auth_failures = 0
            self._s.total_successes += 1
            self._s.last_success_at = datetime.now(timezone.utc)
            self._s.breaker_open_until = None
            alerted = self._s.alerted_open
            self._s.alerted_open = False
        if was_failing:
            logger.info("Angel One auth RECOVERED — broker login succeeded; circuit closed")
            if alerted:
                self._alert(
                    "RECOVERED",
                    "Angel One auth recovered",
                    "Broker login succeeded again; the trading data path is restored.",
                )

    def record_failure(self, error_text: str) -> None:
        """Record a failed login. Genuine auth rejections trip the breaker;
        transient failures (rate-limit / network) are noted but do not."""
        text = (error_text or "").strip()
        auth = is_auth_error(text)
        open_now = False
        with self._lock:
            self._s.last_failure_at = datetime.now(timezone.utc)
            self._s.last_error = text[:300]
            if not auth:
                return
            self._s.consecutive_auth_failures += 1
            self._s.total_auth_failures += 1
            threshold = max(1, _env_int("ANGEL_AUTH_BREAKER_THRESHOLD", 3))
            if self._s.consecutive_auth_failures >= threshold and _env_flag(
                "ANGEL_AUTH_BREAKER_ENABLED", True
            ):
                cooldown = _env_int("ANGEL_AUTH_BREAKER_COOLDOWN_SECONDS", 900)
                self._s.breaker_open_until = time.monotonic() + cooldown
                if not self._s.alerted_open:
                    self._s.alerted_open = True
                    open_now = True
                fails = self._s.consecutive_auth_failures
                last = self._s.last_error
        if open_now:
            logger.critical(
                "Angel One auth circuit OPEN after %d consecutive auth failures: %s. "
                "Broker data path is DOWN — refresh ANGEL_ONE_TOTP_SECRET / credentials.",
                fails, last,
            )
            self._alert(
                "CRITICAL",
                "Angel One broker login FAILING",
                f"{fails} consecutive auth failures (e.g. AB1050). No market data can be "
                f"fetched, so NO orders will be placed until fixed. Last error: {last}. "
                f"Action: regenerate the TOTP secret in the Angel One SmartAPI portal and "
                f"update ANGEL_ONE_TOTP_SECRET, then restart trading-api/scheduler.",
            )

    # ---- /health snapshot ---------------------------------------------------

    def is_healthy(self) -> bool:
        with self._lock:
            return self._s.breaker_open_until is None and self._s.consecutive_auth_failures == 0

    def snapshot(self) -> dict:
        with self._lock:
            s = self._s
            now = time.monotonic()
            breaker_open = s.breaker_open_until is not None and now < s.breaker_open_until
            reopen_in = (s.breaker_open_until - now) if breaker_open else None
            ok = s.breaker_open_until is None and s.consecutive_auth_failures == 0
            return {
                "ok": ok,
                "status": "OK" if ok else "AUTH_FAILING",
                "consecutive_auth_failures": s.consecutive_auth_failures,
                "total_auth_failures": s.total_auth_failures,
                "total_successes": s.total_successes,
                "breaker_open": breaker_open,
                "breaker_reopen_in_seconds": round(reopen_in, 1) if reopen_in is not None else None,
                "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
                "last_failure_at": s.last_failure_at.isoformat() if s.last_failure_at else None,
                "last_error": s.last_error,
            }

    # ---- alerting (best-effort, never raises, never blocks the caller) ------

    def _alert(self, level: str, title: str, message: str) -> None:
        body = f"[{level}] {title}\n{message}"

        def _send() -> None:
            try:
                if _env_flag("MONITORING_TELEGRAM_ENABLED", False):
                    token = os.getenv("MONITORING_TELEGRAM_BOT_TOKEN", "").strip()
                    chat = os.getenv("MONITORING_TELEGRAM_CHAT_ID", "").strip()
                    if token and chat:
                        payload = json.dumps({"chat_id": chat, "text": body}).encode("utf-8")
                        req = urllib.request.Request(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            data=payload,
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        urllib.request.urlopen(req, timeout=4).close()
                webhook = os.getenv("ANGEL_AUTH_ALERT_WEBHOOK_URL", "").strip()
                if webhook:
                    payload = json.dumps(
                        {"level": level, "title": title, "message": message,
                         "timestamp": datetime.now(timezone.utc).isoformat()}
                    ).encode("utf-8")
                    req = urllib.request.Request(
                        webhook, data=payload,
                        headers={"Content-Type": "application/json"}, method="POST",
                    )
                    urllib.request.urlopen(req, timeout=4).close()
            except Exception as exc:  # alerting must never break the trading path
                logger.warning("auth_health alert delivery failed: %s", exc)

        threading.Thread(target=_send, name="auth-health-alert", daemon=True).start()

    # ---- test support -------------------------------------------------------

    def reset(self) -> None:
        """Clear all state. For tests only."""
        with self._lock:
            self._s = _State()


# The process-wide singleton every login path shares.
_AUTH = _AuthHealth()

before_login_attempt = _AUTH.before_login_attempt
record_success = _AUTH.record_success
record_failure = _AUTH.record_failure
is_healthy = _AUTH.is_healthy
snapshot = _AUTH.snapshot
reset = _AUTH.reset
