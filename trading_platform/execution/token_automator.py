"""
trading_platform/execution/token_automator.py — Token automation (TOTP at 08:45 IST)

Per §13 Phase 2: Token automation — TOTP login at 08:45 IST with CRITICAL alert on failure.
Centralizes the daily Angel One SmartAPI token refresh discipline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import pyotp

logger = logging.getLogger(__name__)


class TokenStatus(str, Enum):
    OK = "ok"
    EXPIRED = "expired"
    FAILED = "failed"
    PENDING = "pending"


@dataclass
class TokenEvent:
    """Represents a token lifecycle event."""
    status: TokenStatus
    timestamp: float
    message: str
    next_retry_seconds: float = 0.0


@dataclass
class BrokerSessionState:
    """Per-tenant broker session state."""
    tenant_id: str
    access_token: str = ""
    refresh_token: str = ""
    otp_secret: str = ""
    api_key: str = ""
    client_log_id: str = ""
    status: TokenStatus = TokenStatus.FAILED
    last_login_time: Optional[float] = None
    last_failure_time: Optional[float] = None
    consecutive_failures: int = 0
    algo_id: str = ""  # SEBI retail-algo compliance (§6.2)


class TokenAutomator:
    """
    Daily token automation for Angel One SmartAPI.

    - TOTP login at 08:45 IST with exponential backoff on failure
    - CRITICAL alert on failure via Telegram
    - Per-tenant session management
    - Token expiration detection and auto-refresh
    """

    # Angel One token validity: tokens typically expire at market close (15:30 IST)
    # but we refresh at 08:45 to ensure freshness
    _MAX_RETRIES = 5
    _BASE_RETRY_DELAY_SECONDS = 30
    _MAX_RETRY_DELAY_SECONDS = 300  # 5 minutes

    def __init__(
        self,
        tenant_id: str = "tenant_default",
        otp_secret: str = "",
        api_key: str = "",
        feed_api_key: str = "",
        client_log_id: str = "",
        parent_token: str = "",
        refresh_token: str = "",
        angel_one_base_url: str = "https://margincalculator.angelbroking.com",
        alert_callback=None,  # Telegram alert callback
    ):
        self.tenant_id = tenant_id
        self.otp_secret = otp_secret
        self.api_key = api_key
        self.feed_api_key = feed_api_key
        self.client_log_id = client_log_id
        self.parent_token = parent_token
        self.refresh_token = refresh_token
        self.angel_one_base_url = angel_one_base_url
        self.alert_callback = alert_callback

        self.state = BrokerSessionState(tenant_id=tenant_id)
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the token automator loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"[TOKEN] TokenAutomator started for tenant={self.tenant_id}")

    async def stop(self) -> None:
        """Stop the token automator loop."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"[TOKEN] TokenAutomator stopped for tenant={self.tenant_id}")

    async def _loop(self) -> None:
        """Main loop: check token status, refresh at 08:45 IST or on expiry."""
        while self._running:
            try:
                now_ist = self._get_ist_now()
                should_refresh = False

                # Check if we need to refresh:
                # 1. Token expired or failed
                if self.state.status != TokenStatus.OK:
                    should_refresh = True
                # 2. It's past 08:45 and last login was before today's 08:45
                elif self.state.last_login_time:
                    last_login = datetime.fromtimestamp(self.state.last_login_time)
                    today_845 = datetime(now_ist.year, now_ist.month, now_ist.day, 8, 45)
                    if last_login < today_845:
                        should_refresh = True

                if should_refresh:
                    await self._refresh_token()

                # Sleep until next check (every 60 seconds during market hours)
                await asyncio.sleep(60)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[TOKEN] TokenAutomator loop error: {exc}", exc_info=True)
                await asyncio.sleep(30)

    async def _refresh_token(self) -> None:
        """Attempt to refresh the Angel One token with retry logic."""
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                logger.info(f"[TOKEN] Refresh attempt {attempt}/{self._MAX_RETRIES} for tenant={self.tenant_id}")

                # Step 1: Generate TOTP
                if not self.otp_secret:
                    raise ValueError(f"OTP secret not configured for tenant={self.tenant_id}")

                otp_code = pyotp.totp.TOTP(self.otp_secret).now()

                # Step 2: Call Angel One SmartAPI login endpoint
                # This is a placeholder — the actual login logic depends on the
                # broker adapter implementation. The key discipline is:
                # - Use TOTP + parent token
                # - Rate limit: max 5 login attempts per day per client code
                success = await self._do_login(otp_code)

                if success:
                    self.state.status = TokenStatus.OK
                    self.state.last_login_time = time.time()
                    self.state.consecutive_failures = 0
                    logger.info(f"[TOKEN] Token refreshed successfully for tenant={self.tenant_id}")

                    # Alert success (INFO tier)
                    if self.alert_callback:
                        await self.alert_callback(
                            "INFO",
            "Token refreshed",
            f"Angel One token refreshed for tenant {self.tenant_id}",
        )
                    return

                # Login failed
                self.state.status = TokenStatus.FAILED
                self.state.last_failure_time = time.time()
                self.state.consecutive_failures += 1
                logger.warning(f"[TOKEN] Login attempt {attempt} failed for tenant={self.tenant_id}")

            except Exception as exc:
                self.state.status = TokenStatus.FAILED
                self.state.last_failure_time = time.time()
                self.state.consecutive_failures += 1
                logger.error(f"[TOKEN] Login attempt {attempt} error: {exc}", exc_info=True)

            # Exponential backoff
            delay = min(
                self._BASE_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)),
                self._MAX_RETRY_DELAY_SECONDS,
            )
            if attempt < self._MAX_RETRIES:
                logger.info(f"[TOKEN] Retrying in {delay}s...")
                await asyncio.sleep(delay)

        # All retries exhausted — CRITICAL alert
        logger.critical(
            f"[TOKEN] Token refresh FAILED after {self._MAX_RETRIES} attempts "
            f"for tenant={self.tenant_id}. Consecutive failures: {self.state.consecutive_failures}"
        )

        # CRITICAL alert via Telegram
        if self.alert_callback:
            await self.alert_callback(
                "CRITICAL",
                "Token refresh failed",
                f"Angel One token refresh failed for tenant {self.tenant_id} "
                f"after {self._MAX_RETRIES} attempts. Manual intervention required.",
            )

    async def _do_login(self, otp_code: str) -> bool:
        """
        Perform the actual login to Angel One SmartAPI.

        This is a placeholder that delegates to the broker adapter.
        The real implementation should:
        1. POST to SmartAPI's login endpoint with OTP
        2. Exchange response for access_token + refresh_token
        3. Store tokens in state
        """
        # Placeholder — the actual broker adapter (AngelOneGateway) will implement this.
        # For now, return False to simulate failure.
        from trading_platform.config import settings
        from trading_platform.broker.angel_one_client import AngelOneClient

        client = AngelOneClient(
            api_key=self.api_key or settings.ANGEL_ONE_API_KEY,
            feed_api_key=self.feed_api_key or settings.ANGEL_ONE_FEED_API_KEY,
            client_log_id=self.client_log_id or settings.ANGEL_ONE_CLIENT_LOG_ID,
            parent_token=self.parent_token or settings.ANGEL_ONE_PARENT_TOKEN,
            refresh_token=self.refresh_token or settings.ANGEL_ONE_REFRESH_TOKEN,
        )

        try:
            result = await client.generate_session(otp_code)
            if result and result.get("success"):
                self.state.access_token = result.get("data", {}).get("access_token", "")
                self.state.refresh_token = result.get("data", {}).get("refresh_token", "")
                self.state.algo_id = result.get("data", {}).get("algo_id", "")
                return True
            return False
        except Exception:
            return False

    def _get_ist_now(self) -> datetime:
        """Get current time in IST timezone."""
        from trading_platform.utils import now_ist
        return now_ist()

    def get_state(self) -> BrokerSessionState:
        """Get current broker session state."""
        return self.state

    async def force_refresh(self) -> bool:
        """Force an immediate token refresh (e.g., from UI or risk event)."""
        self.state.status = TokenStatus.PENDING
        await self._refresh_token()
        return self.state.status == TokenStatus.OK

    def is_healthy(self) -> bool:
        """Check if the token is healthy."""
        if self.state.status != TokenStatus.OK:
            return False
        if self.state.last_login_time is None:
            return False
        # Token should be refreshed if older than 12 hours
        return (time.time() - self.state.last_login_time) < 43200


class TokenSessionManager:
    """
    Per-tenant token session manager.

    Manages multiple TokenAutomator instances (one per tenant).
    """

    def __init__(self, alert_callback=None):
        self._sessions: dict[str, TokenAutomator] = {}
        self.alert_callback = alert_callback

    async def register_tenant(
        self,
        tenant_id: str,
        otp_secret: str = "",
        api_key: str = "",
        feed_api_key: str = "",
        client_log_id: str = "",
        parent_token: str = "",
        refresh_token: str = "",
    ) -> TokenAutomator:
        """Register a tenant's token automator."""
        if tenant_id in self._sessions:
            return self._sessions[tenant_id]

        automator = TokenAutomator(
            tenant_id=tenant_id,
            otp_secret=otp_secret,
            api_key=api_key,
            feed_api_key=feed_api_key,
            client_log_id=client_log_id,
            parent_token=parent_token,
            refresh_token=refresh_token,
            alert_callback=self.alert_callback,
        )
        self._sessions[tenant_id] = automator
        await automator.start()
        return automator

    def get_session(self, tenant_id: str) -> Optional[TokenAutomator]:
        """Get a tenant's token automator."""
        return self._sessions.get(tenant_id)

    def get_all_states(self) -> dict[str, BrokerSessionState]:
        """Get all tenant session states."""
        return {tid: s.get_state() for tid, s in self._sessions.items()}

    async def stop_all(self) -> None:
        """Stop all token automators."""
        for automator in self._sessions.values():
            await automator.stop()
        self._sessions.clear()