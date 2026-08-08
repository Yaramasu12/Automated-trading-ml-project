"""
AngelOneGateway — centralized Angel One rate-limiting, token management,
and login/TOTP lifecycle.  Every fetch to Angel One SmartAPI goes
through this gateway (REDESIGN_PROMPT.md §3 / §16.7).

Responsibilities:
  1. Serialized REST calls respecting ~10 orders/sec and login rate-limits.
  2. Token bucket for per-symbol fetches (shared by all adapters).
  3. TOTP login at 08:45 IST with CRITICAL alert on failure.
  4. JWT lifecycle: cached login until it starts failing, then re-login.
  5. Connection pool for up to 3 WebSocket shards (≤1000 tokens each).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token bucket — O(1) rate limiter
# ---------------------------------------------------------------------------

class TokenBucket:
    """Simple leaky-bucket rate limiter (thread-safe via asyncio lock)."""

    def __init__(self, rate: float, burst: int) -> None:
        """
        Args:
            rate: tokens replenished per second.
            burst: max bucket capacity.
        """
        self._rate = rate
        self._bucket = float(burst)
        self._max = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Block until *tokens* are available."""
        async with self._lock:
            while True:
                now = time.monotonic()
                self._bucket = min(self._max, self._bucket + (now - self._last) * self._rate)
                self._last = now
                if self._bucket >= tokens:
                    self._bucket -= tokens
                    return
                wait = (tokens - self._bucket) / self._rate
                # Release lock while waiting
                self._lock.release()
                await asyncio.sleep(wait)
                await self._lock.acquire()


# ---------------------------------------------------------------------------
# WebSocket shard tracker
# ---------------------------------------------------------------------------

@dataclass
class WSShard:
    """One Angel One WebSocket connection (≤1000 tokens)."""
    id: str                    # UUID
    tokens: set[str] = field(default_factory=set)
    last_message_ts: float = 0.0
    healthy: bool = True
    reconnect_count: int = 0
    max_reconnects: int = 50  # generous; never go permanently dead (§3.2)


class ShardPool:
    """
    Manages up to 3 Angel One WebSocket shards.
    Assigns tokens round-robin; tracks per-shard health.
    """

    MAX_SHARDS: int = 3
    MAX_TOKENS_PER_SHARD: int = 1000

    def __init__(self) -> None:
        self._shards: list[WSShard] = []
        self._idx = 0
        self._lock = asyncio.Lock()

    async def add_symbols(self, symbols: Sequence[str]) -> list[WSShard]:
        """
        Assign *symbols* across shards round-robin.
        Returns the list of shards that now hold subscriptions.
        """
        async with self._lock:
            assigned: list[WSShard] = []
            for sym in symbols:
                shard = self._pick_shard()
                if len(shard.tokens) >= self.MAX_TOKENS_PER_SHARD:
                    shard = self._ensure_next_shard()
                shard.tokens.add(sym)
                assigned.append(shard)
            return assigned

    async def remove_symbols(self, symbols: Sequence[str]) -> list[WSShard]:
        """Unassign symbols; return shards that became empty (can close)."""
        async with self._lock:
            closed_shards: list[WSShard] = []
            for sym in symbols:
                for shard in self._shards:
                    if sym in shard.tokens:
                        shard.tokens.discard(sym)
                        if not shard.tokens and shard.reconnect_count >= shard.max_reconnects:
                            closed_shards.append(shard)
            return closed_shards

    def get_healthy_shards(self) -> list[WSShard]:
        return [s for s in self._shards if s.healthy]

    def _pick_shard(self) -> WSShard:
        if not self._shards:
            return self._ensure_next_shard()
        shard = self._shards[self._idx % len(self._shards)]
        self._idx += 1
        return shard

    def _ensure_next_shard(self) -> WSShard:
        if len(self._shards) >= self.MAX_SHARDS:
            raise RuntimeError(
                "Angel One limit: max 3 WebSocket connections. "
                f"Current subscriptions={sum(len(s.tokens) for s in self._shards)}"
            )
        import uuid
        shard = WSShard(id=str(uuid.uuid4())[:8])
        self._shards.append(shard)
        return shard

    def mark_unhealthy(self, shard_id: str) -> None:
        for s in self._shards:
            if s.id == shard_id:
                s.healthy = False
                break

    def mark_healthy(self, shard_id: str) -> None:
        for s in self._shards:
            if s.id == shard_id:
                s.healthy = True
                s.reconnect_count = 0
                break


# ---------------------------------------------------------------------------
# Angel One credentials
# ---------------------------------------------------------------------------

@dataclass
class AngelOneCredentials:
    """Loaded from .env — never hard-coded or logged."""
    user_id: str
    password: str
    totp_secret: str  # for 08:45 IST login automation
    api_key: Optional[str] = None  # optional explicit key (bypasses password flow)
    master_token: Optional[str] = None
    session_token: Optional[str] = None
    token_expiry: Optional[float] = None  # monotonic timestamp

    @property
    def is_token_valid(self) -> bool:
        if not self.session_token or not self.token_expiry:
            return False
        return time.monotonic() < self.token_expiry


# ---------------------------------------------------------------------------
# AngelOneGateway — the single entry point for all Angel One operations
# ---------------------------------------------------------------------------

class AngelOneGateway:
    """
    Centralized Angel One gateway:
      - REST rate-limit token bucket
      - JWT/session lifecycle
      - TOTP login automation at 08:45 IST
      - WebSocket shard pool management
      - Shared negative-TTL cache for REST fetches
    """

    # Angel One SmartAPI defaults (2026)
    DEFAULT_REST_RATE: float = 10.0       # orders/sec
    DEFAULT_REST_BURST: int = 20          # burst capacity
    LOGIN_RELOGIN_THRESHOLD_SEC: float = 300.0  # re-login if <5 min left
    JWT_STALE_RELOGIN_SEC: float = 60.0       # re-login if JWT starts failing

    def __init__(
        self,
        creds: AngelOneCredentials,
        rate_limit: Optional[TokenBucket] = None,
    ) -> None:
        self._creds = creds
        self._bucket = rate_limit or TokenBucket(
            rate=self.DEFAULT_REST_RATE, burst=self.DEFAULT_REST_BURST
        )
        self._shards = ShardPool()
        self._cache: dict[str, tuple[Any, float]] = {}  # key → (value, monotonic_ttl)
        self._cache_ttl_sec: float = 30.0  # default negative-TTL for REST fetches

    # --- Rate limiting ---

    async def acquire_rest(self, tokens: float = 1.0) -> None:
        """Acquire rate-limit tokens before any Angel One REST call."""
        await self._bucket.acquire(tokens)

    # --- Shard management ---

    @property
    def shard_pool(self) -> ShardPool:
        return self._shards

    async def add_symbols_to_shards(self, symbols: Sequence[str]) -> list[WSShard]:
        return await self._shards.add_symbols(symbols)

    async def remove_symbols_from_shards(self, symbols: Sequence[str]) -> list[WSShard]:
        return await self._shards.remove_symbols(symbols)

    # --- Cache ---

    def cache_get(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry and (time.monotonic() - entry[1]) < self._cache_ttl_sec:
            return entry[0]
        return None

    def cache_put(self, key: str, value: Any, ttl_sec: Optional[float] = None) -> None:
        ttl = ttl_sec or self._cache_ttl_sec
        self._cache[key] = (value, time.monotonic() + ttl)

    def cache_clear(self) -> None:
        self._cache.clear()

    # --- Session lifecycle (to be wired with TOTP automation) ---

    async def ensure_session(self) -> str:
        """
        Return a valid session JWT. Re-logs in if current token is stale.
        CRITICAL alert on TOTP failure.
        """
        if self._creds.is_token_valid:
            return self._creds.session_token  # type: ignore[return-value]

        logger.warning("Angel One session expired — triggering re-login")
        # TBD: wire with TOTP library (pyotp) at 08:45 IST
        # For now, return placeholder — the deploy script (deploy/set-angelone.sh)
        # handles the TOTP login flow externally.
        if not self._creds.master_token:
            raise RuntimeError(
                "Angel One master_token not set. "
                "Ensure deploy/set-angelone.sh ran at 08:45 IST."
            )
        # Placeholder: exchange master_token → session_token
        self._creds.session_token = self._creds.master_token
        self._creds.token_expiry = time.monotonic() + 86400  # 24h
        return self._creds.session_token  # type: ignore[return-value]

    # --- Health ---

    async def health(self) -> dict[str, Any]:
        return {
            "source": "ANGEL_ONE",
            "healthy": len(self._shards.get_healthy_shards()) > 0,
            "shards": len(self._shards._shards),
            "healthy_shards": len(self._shards.get_healthy_shards()),
            "total_subscriptions": sum(len(s.tokens) for s in self._shards._shards),
            "token_valid": self._creds.is_token_valid,
            "cache_entries": len(self._cache),
        }


# ---------------------------------------------------------------------------
# Module-level singleton — DI container wires this from Settings
# ---------------------------------------------------------------------------

_gateway: Optional[AngelOneGateway] = None


def get_gateway(creds: AngelOneCredentials) -> AngelOneGateway:
    """Get or create the module-level gateway singleton."""
    global _gateway
    if _gateway is None:
        _gateway = AngelOneGateway(creds)
    return _gateway


def reset_gateway() -> None:
    """For testing only."""
    global _gateway
    _gateway = None