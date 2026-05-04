"""Bearer-token authentication for the FastAPI control plane.

Public read-only endpoints (`/health`, `/state`, `/api/v1/health`) bypass auth.
Every endpoint that toggles the kill switch, changes execution mode, places paper
or shadow orders, enqueues intents, updates exit marks, arms live trading,
approves manual orders, fetches account snapshots, or otherwise mutates control
plane state requires a `Bearer <token>` header that matches `API_AUTH_TOKEN`.

WebSocket connections require the same token via `?token=...` on the URL or via
the first JSON message `{"action": "auth", "token": "..."}` before any
mutating command is accepted. Mutating commands sent before authentication are
rejected with `{"error": "unauthenticated"}`.

If `API_AUTH_REQUIRED=false` is set explicitly *and* `API_AUTH_TOKEN` is empty,
auth is bypassed (development only). Any other configuration enforces auth and
will return 503 if no token is configured to fail closed.
"""
from __future__ import annotations

import hmac

from trading_platform.config import Settings, load_settings

try:
    from fastapi import Header, HTTPException, status
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("Install API dependencies with: pip install -r requirements.txt") from exc


_settings_cache: Settings | None = None


def _settings() -> Settings:
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = load_settings()
    return _settings_cache


def set_settings_for_tests(settings: Settings | None) -> None:
    """Override the cached settings (test helper)."""
    global _settings_cache
    _settings_cache = settings


def _auth_disabled(settings: Settings) -> bool:
    return (not settings.api_auth_required) and not settings.api_auth_token


def verify_token(token: str | None) -> bool:
    """Constant-time check; returns True only when token matches configured value."""
    settings = _settings()
    if _auth_disabled(settings):
        return True
    if not settings.api_auth_token:
        return False
    if not token:
        return False
    return hmac.compare_digest(token, settings.api_auth_token)


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency that enforces Bearer-token auth on protected routes."""
    settings = _settings()
    if _auth_disabled(settings):
        return
    if not settings.api_auth_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_AUTH_TOKEN is not configured; control plane refuses to operate without auth.",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Expected 'Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(presented, settings.api_auth_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
