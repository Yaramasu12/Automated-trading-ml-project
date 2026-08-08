"""
UpstoxDataAdapter — free broker data API with rich options data (REDESIGN_PROMPT.md §3.0, §16.7).

Purpose: full option chain WITH GREEKS + expired F&O historical data.
Used as the SECONDARY/ALTERNATE live feed alongside Angel One.
Individual-KYC only (no company/business KYC required).

Key advantages over Angel One:
- Richer options data (full chain with Greeks)
- Serves EXPIRED F&O historical data (critical for IV-rank history backtesting)
- Longer-lived tokens (less daily-login burden at scale)
- 25 orders/sec rate limit

Legal: broker data API, NOT redistributable. Each user must have their own account.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional, Sequence
from dataclasses import dataclass, field

from trading_platform.config import Settings
from trading_platform.data.market_adapter import MarketDataAdapter, TickV2, DepthSnapshot, FeedSource, SymbolMapper

logger = logging.getLogger(__name__)

# Upstox API base URL (v2 API)
UPSTOX_API_BASE: str = "https://api.upstox.com"
UPSTOX_WS_URL: str = "wss://ws.upstox.com/v2"


@dataclass
class UpstoxAuthState:
    """Track Upstox OAuth token lifecycle."""
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_expiry: float = 0.0  # unix timestamp
    is_authorizing: bool = False

    def is_expired(self) -> bool:
        """Check if access token is expired (with 30s safety margin)."""
        return time.time() > (self.token_expiry - 30) if self.token_expiry > 0 else True


class UpstoxDataAdapter(MarketDataAdapter):
    """
    Upstox broker data adapter.

    Provides:
    - Live tick feed via WebSocket (mode 3 + depth)
    - Full option chain with Greeks
    - Expired F&O historical data (for IV-rank backtesting)
    - Account data (funds, positions, orders, fills)

    Publishes TickV2 to Redis Streams tick bus (§3.2).
    """

    def __init__(
        self,
        settings: Settings,
        symbol_mapper: SymbolMapper | None = None,
    ) -> None:
        self._settings = settings
        self._mapper = symbol_mapper or SymbolMapper()
        self._auth = UpstoxAuthState()
        self._ws: Any = None
        self._connected = False
        self._subscribed_symbols: list[str] = []
        self._reconnect_attempts = 0
        self._MAX_RECONNECT_ATTEMPTS = 100
        self._RECONNECT_BASE_DELAY = 1.0
        self._RECONNECT_MAX_DELAY = 60.0
        self._staleness: dict[str, float] = {}
        self._staleness_threshold_sec: float = 15.0  # seconds
        self._on_tick_callback = None
        self._on_depth_callback = None
        self._on_staleness_callback = None
        self._reconnect_delay = self._RECONNECT_BASE_DELAY
        self._heartbeat_task: Any = None
        self._watchdog_task: Any = None

        # Upstox credentials
        self._api_key = settings.upstox_api_key or ""
        self._api_secret = settings.upstox_api_secret or ""
        self._redirect_url = settings.upstox_redirect_url or "https://example.com/callback"
        self._auth_code = settings.upstox_auth_code or ""

    async def start(
        self,
        symbols: Sequence[str],
        on_tick=None,
        on_depth=None,
        on_staleness=None,
    ) -> None:
        """Start live feed with symbol sharding (up to 3 connections)."""
        self._on_tick_callback = on_tick
        self._on_depth_callback = on_depth
        self._on_staleness_callback = on_staleness

        # Authorize if needed
        if not self._auth.access_token or self._auth.is_expired():
            await self._authorize()

        if not self._auth.access_token:
            logger.error("Upstox: authorization failed — cannot start feed")
            return

        # Shard symbols across up to 3 WebSocket connections
        shards = self._shard_symbols(list(symbols), max_shards=3)

        # Connect to each shard
        for i, shard_symbols in enumerate(shards):
            asyncio.create_task(self._connect_shard(i, shard_symbols))

        self._subscribed_symbols = list(symbols)
        logger.info("UpstoxDataAdapter starting with %d symbols in %d shards", len(symbols), len(shards))

    def _shard_symbols(self, symbols: list[str], max_shards: int = 3) -> list[list[str]]:
        """Round-robin shard symbols across max_shards buckets."""
        shards: list[list[str]] = [[] for _ in range(max_shards)]
        for i, sym in enumerate(symbols):
            shards[i % max_shards].append(sym)
        return [s for s in shards if s]  # remove empty shards

    async def _connect_shard(self, shard_id: int, symbols: list[str]) -> None:
        """Connect a single WebSocket shard to Upstox."""
        try:
            # Upstox WebSocket v2 connection
            # URL: wss://ws.upstox.com/v2/connect/realtime?apikey=...&symbols=...
            symbols_param = ",".join(self._upstox_symbol(s) for s in symbols)
            ws_url = f"{UPSTOX_WS_URL}/connect/realtime?apikey={self._api_key}&symbols={symbols_param}"

            logger.info("Upstox shard %d connecting to %s (%d symbols)", shard_id, ws_url[:80], len(symbols))

            # WebSocket connection (using websockets library)
            import websockets
            self._ws = await websockets.connect(ws_url)

            self._connected = True
            self._reconnect_delay = self._RECONNECT_BASE_DELAY

            # Heartbeat
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(shard_id))
            self._watchdog_task = asyncio.create_task(self._watchdog_loop(shard_id, symbols))

            # Listen for messages
            async for message in self._ws:
                if not self._connected:
                    break
                await self._handle_message(message, shard_id)

        except Exception as e:
            logger.error("Upstox shard %d connection error: %s", shard_id, e)
            await self._reconnect_shard(shard_id, symbols)
        finally:
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    pass

    async def _reconnect_shard(self, shard_id: int, symbols: list[str]) -> None:
        """Reconnect with exponential backoff."""
        while self._reconnect_attempts < self._MAX_RECONNECT_ATTEMPTS:
            self._reconnect_attempts += 1
            delay = min(
                self._RECONNECT_BASE_DELAY * (2 ** (self._reconnect_attempts - 1)),
                self._RECONNECT_MAX_DELAY,
            )
            logger.warning(
                "Upstox shard %d reconnecting in %.1fs (attempt %d/%d)",
                shard_id, delay, self._reconnect_attempts, self._MAX_RECONNECT_ATTEMPTS,
            )
            await asyncio.sleep(delay)

            try:
                await self._connect_shard(shard_id, symbols)
                self._reconnect_attempts = 0
                self._reconnect_delay = self._RECONNECT_BASE_DELAY
                return
            except Exception as e:
                logger.error("Upstox shard %d reconnect failed: %s", shard_id, e)

        logger.error("Upstox shard %d max reconnect attempts reached", shard_id)

    async def _heartbeat_loop(self, shard_id: int) -> None:
        """Send periodic heartbeat to keep WebSocket alive."""
        try:
            while self._connected:
                await asyncio.sleep(15)  # 15s heartbeat
                if self._ws and self._ws.open:
                    await self._ws.send('{"type":"heartbeat"}')
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("Upstox heartbeat error: %s", e)

    async def _watchdog_loop(self, shard_id: int, symbols: list[str]) -> None:
        """Monitor staleness per symbol and trigger alerts."""
        try:
            while self._connected:
                await asyncio.sleep(10)  # check every 10s
                now = time.time()
                stale_symbols = []
                for sym, last_time in list(self._staleness.items()):
                    if now - last_time > self._staleness_threshold_sec:
                        stale_symbols.append(sym)

                if stale_symbols and self._on_staleness_callback:
                    await self._on_staleness_callback(stale_symbols, FeedSource.UPSTOX)
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, message: str, shard_id: int) -> None:
        """Parse Upstox WebSocket message and publish to tick bus."""
        try:
            import json
            data = json.loads(message)
            msg_type = data.get("type", data.get("event_type", ""))

            if msg_type in ("trade", "tick", "quote"):
                # Parse tick data
                tick = self._parse_tick(data)
                if tick and self._on_tick_callback:
                    await self._on_tick_callback(tick)
                # Update staleness
                self._staleness[tick.symbol] = time.time() if tick else time.time()
            elif msg_type in ("depth", "snapshot"):
                depth = self._parse_depth(data)
                if depth and self._on_depth_callback:
                    await self._on_depth_callback(depth)
            elif msg_type in ("error", "connection_error"):
                logger.error("Upstox shard %d error: %s", shard_id, data.get("message", "unknown"))
        except json.JSONDecodeError as e:
            logger.warning("Upstox invalid JSON from shard %d: %s", shard_id, e)
        except Exception as e:
            logger.error("Upstox message parse error: %s", e)

    def _parse_tick(self, data: dict[str, Any]) -> Optional[TickV2]:
        """Parse Upstox tick data → TickV2."""
        symbol = data.get("symbol") or data.get("instrument_token", "")
        if not symbol:
            return None

        # Extract price/qty fields (Upstox field names)
        last_price = data.get("last_price") or data.get("last_trade_price")
        volume = data.get("volume") or data.get("total_traded_volume")
        oi = data.get("oi")

        # Best bid/ask (mode 3 has OHLC but no depth; depth feed has it)
        bid = data.get("bid_price", data.get("bp1"))
        ask = data.get("ask_price", data.get("sp1"))
        bid_qty = data.get("bid_qty", data.get("bp1_qty"))
        ask_qty = data.get("ask_qty", data.get("sp1_qty"))

        # Convert to proper types
        try:
            last_price = float(last_price) if last_price is not None else 0.0
            bid = float(bid) if bid is not None else 0.0
            ask = float(ask) if ask is not None else 0.0
            bid_qty = float(bid_qty) if bid_qty is not None else 0.0
            ask_qty = float(ask_qty) if ask_qty is not None else 0.0
            volume = int(volume) if volume is not None else 0
            oi = int(oi) if oi is not None else 0
        except (TypeError, ValueError):
            return None

        return TickV2(
            symbol=symbol,
            timestamp=data.get("timestamp", data.get("last_updated_time", 0)),
            open=data.get("open") or 0.0,
            high=data.get("high") or 0.0,
            low=data.get("low") or 0.0,
            close=data.get("close") or last_price,
            last_price=last_price,
            volume=volume,
            oi=oi,
            bid=bid,
            ask=ask,
            bid_qty=bid_qty,
            ask_qty=ask_qty,
            segment="NSE_FO" if "FO" in symbol or "CMD" in symbol else "NSE_CM",
            source=FeedSource.UPSTOX,
            exchange=data.get("exchange", "NSE"),
            sequence=data.get("sequence", ""),
            trade_id=data.get("trade_id", ""),
        )

    def _parse_depth(self, data: dict[str, Any]) -> Optional[DepthSnapshot]:
        """Parse Upstox depth data → DepthSnapshot."""
        symbol = data.get("symbol") or data.get("instrument_token", "")
        if not symbol:
            return None

        # Top 5 depth levels
        bids = []
        asks = []
        for i in range(5):
            bid_price = data.get(f"bp{i+1}")
            bid_qty = data.get(f"bp{i+1}_qty")
            ask_price = data.get(f"sp{i+1}")
            ask_qty = data.get(f"sp{i+1}_qty")
            if bid_price is not None:
                bids.append((float(bid_price), float(bid_qty) if bid_qty else 0))
            if ask_price is not None:
                asks.append((float(ask_price), float(ask_qty) if ask_qty else 0))

        return DepthSnapshot(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp=data.get("timestamp", data.get("last_updated_time", 0)),
            source=FeedSource.UPSTOX,
        )

    def _upstox_symbol(self, internal_symbol: str) -> str:
        """Convert internal symbol → Upstox format."""
        mapped = self._mapper.to_vendor_symbol("UPSTOX", internal_symbol)
        if mapped:
            return mapped

        # Upstox format: "NSE:SYMBOL" (e.g., "NSE:NIFTY24OCT30000PE")
        # Most symbols work as-is with prefix
        if ":" not in internal_symbol:
            return f"NSE:{internal_symbol}"
        return internal_symbol

    async def _authorize(self) -> bool:
        """OAuth2 authorization flow with Upstox."""
        if self._auth.is_authorizing:
            return False
        self._auth.is_authorizing = True

        try:
            # If we have an auth code, exchange for tokens
            if self._auth_code:
                import requests
                resp = requests.post(
                    f"{UPSTOX_API_BASE}/v2/login/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": self._api_key,
                        "client_secret": self._api_secret,
                        "code": self._auth_code,
                        "redirect_uri": self._redirect_url,
                    },
                )
                resp.raise_for_status()
                token_data = resp.json()

                self._auth.access_token = token_data.get("access_token")
                self._auth.refresh_token = token_data.get("refresh_token")
                expires_in = token_data.get("expires_in", 3600)
                self._auth.token_expiry = time.time() + expires_in
                self._auth.is_authorizing = False

                logger.info("Upstox authorized (token expires in %ds)", expires_in)
                return True

            # If we have a refresh token, refresh the access token
            if self._auth.refresh_token:
                import requests
                resp = requests.post(
                    f"{UPSTOX_API_BASE}/v2/refresh-token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": self._api_key,
                        "client_secret": self._api_secret,
                        "refresh_token": self._auth.refresh_token,
                    },
                )
                resp.raise_for_status()
                token_data = resp.json()

                self._auth.access_token = token_data.get("access_token")
                self._auth.refresh_token = token_data.get("refresh_token", self._auth.refresh_token)
                expires_in = token_data.get("expires_in", 3600)
                self._auth.token_expiry = time.time() + expires_in
                self._auth.is_authorizing = False

                logger.info("Upstox token refreshed (expires in %ds)", expires_in)
                return True

            logger.warning("Upstox: no auth code or refresh token available")
            return False

        except Exception as e:
            logger.error("Upstox authorization failed: %s", e)
            self._auth.is_authorizing = False
            return False

    async def get_account_data(self) -> dict[str, Any]:
        """Get account data from Upstox API."""
        if not self._auth.access_token or self._auth.is_expired():
            if not await self._authorize():
                return {"source": "UPSTOX", "error": "authorization failed"}

        import requests
        headers = {"Authorization": f"Bearer {self._auth.access_token}"}

        try:
            # Get profile/funds
            resp = requests.get(f"{UPSTOX_API_BASE}/v2/user/profile/market-portfolio", headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return {"source": "UPSTOX", "error": str(e)}

    async def get_positions(self) -> list[dict[str, Any]]:
        """Get positions from Upstox."""
        if not self._auth.access_token or self._auth.is_expired():
            if not await self._authorize():
                return []

        import requests
        headers = {"Authorization": f"Bearer {self._auth.access_token}"}

        try:
            resp = requests.get(f"{UPSTOX_API_BASE}/v2/portfolio/positions", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("day", []) or []
        except Exception as e:
            logger.error("Upstox get_positions failed: %s", e)
            return []

    async def get_funds(self) -> dict[str, Any]:
        """Get funds from Upstox."""
        if not self._auth.access_token or self._auth.is_expired():
            if not await self._authorize():
                return {"source": "UPSTOX", "error": "authorization failed"}

        import requests
        headers = {"Authorization": f"Bearer {self._auth.access_token}"}

        try:
            resp = requests.get(f"{UPSTOX_API_BASE}/v2/portfolio/margin", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return {"source": "UPSTOX", "funds": data.get("data", {})}
        except Exception as e:
            return {"source": "UPSTOX", "error": str(e)}

    async def place_order(self, **kwargs: Any) -> dict[str, Any]:
        """Place order via Upstox API."""
        if not self._auth.access_token or self._auth.is_expired():
            if not await self._authorize():
                return {"error": "authorization failed"}

        import requests
        headers = {
            "Authorization": f"Bearer {self._auth.access_token}",
            "Content-Type": "application/json",
        }

        # Upstox order parameters
        instrument_symbol = kwargs.get("symbol")
        quantity = kwargs.get("quantity", 1)
        price_type = kwargs.get("price_type", "MARKET")
        price = kwargs.get("price")
        transaction_type = kwargs.get("transaction_type", "BUY")
        product_type = kwargs.get("product_type", "MIS")
        order_type = kwargs.get("order_type", "LIMIT")

        payload = {
            "instrument_symbol": instrument_symbol,
            "quantity": quantity,
            "price_type": price_type,
            "transaction_type": transaction_type,
            "product_type": product_type,
            "order_type": order_type,
        }
        if price:
            payload["price"] = str(price)
            payload["stop_loss_price"] = ""

        try:
            resp = requests.post(f"{UPSTOX_API_BASE}/v2/order/place", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return {"source": "UPSTOX", "order_id": data.get("data", {}).get("order_id")}
        except Exception as e:
            return {"error": f"Upstox place_order failed: {e}"}

    async def get_orders(self) -> list[dict[str, Any]]:
        """Get orders from Upstox."""
        if not self._auth.access_token or self._auth.is_expired():
            return []

        import requests
        headers = {"Authorization": f"Bearer {self._auth.access_token}"}

        try:
            resp = requests.get(f"{UPSTOX_API_BASE}/v2/order/orders", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", []) or []
        except Exception as e:
            logger.error("Upstox get_orders failed: %s", e)
            return []

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel order via Upstox."""
        if not self._auth.access_token or self._auth.is_expired():
            return {"error": "authorization failed"}

        import requests
        headers = {"Authorization": f"Bearer {self._auth.access_token}"}

        try:
            resp = requests.delete(
                f"{UPSTOX_API_BASE}/v2/order/{order_id}/cancel",
                headers=headers,
                json={"order_type": "LIMIT"},  # TBD: parameterize
            )
            resp.raise_for_status()
            return {"source": "UPSTOX", "cancelled": order_id}
        except Exception as e:
            return {"error": f"Upstox cancel_order failed: {e}"}

    async def get_history_api(
        self,
        symbol: str,
        interval: str = "3minute",
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> Any:
        """Get historical data from Upstox (candle data)."""
        if not self._auth.access_token or self._auth.is_expired():
            if not await self._authorize():
                return None

        import requests
        headers = {"Authorization": f"Bearer {self._auth.access_token}"}

        upstox_symbol = self._upstox_symbol(symbol)
        params = {
            "from_date": start or "",
            "to_date": end or "",
            "interval": interval,
        }

        try:
            resp = requests.get(
                f"{UPSTOX_API_BASE}/v2/historical-candle/{upstox_symbol}",
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {})  # TBD: convert to Polars DataFrame
        except Exception as e:
            logger.error("Upstox get_history failed: %s", e)
            return None

    async def get_option_chain(
        self,
        underlying: str,
        expiry: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Get full option chain with Greeks from Upstox.

        This is Upstox's key advantage over Angel One — rich options data.
        """
        if not self._auth.access_token or self._auth.is_expired():
            if not await self._authorize():
                return []

        import requests
        headers = {"Authorization": f"Bearer {self._auth.access_token}"}

        try:
            # Upstox option chain API
            resp = requests.get(
                f"{UPSTOX_API_BASE}/v2/options/chain",
                headers=headers,
                params={"underlying": underlying, "expiry": expiry or ""},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [])
        except Exception as e:
            logger.error("Upstox get_option_chain failed: %s", e)
            return []

    async def stop(self) -> None:
        """Disconnect Upstox WebSocket."""
        self._connected = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._watchdog_task:
            self._watchdog_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._subscribed_symbols.clear()
        logger.info("UpstoxDataAdapter disconnected")

    async def health(self) -> dict[str, Any]:
        """Health check."""
        return {
            "healthy": self._connected,
            "source": "UPSTOX",
            "subscribed_symbols": len(self._subscribed_symbols),
            "token_expires_in": max(0, self._auth.token_expiry - time.time()) if self._auth.token_expiry > 0 else 0,
            "staleness": {s: round(time.time() - t, 1) for s, t in self._staleness.items()},
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def create_upstox_adapter(
    settings: Settings,
    symbol_mapper: SymbolMapper | None = None,
) -> UpstoxDataAdapter:
    """Factory: create UpstoxDataAdapter from Settings."""
    if not settings.upstox_api_key:
        raise RuntimeError(
            "UPSTOX_API_KEY not configured — Upstox adapter disabled. "
            "Set UPSTOX_API_KEY and UPSTOX_API_SECRET in environment to enable."
        )
    return UpstoxAdapter(settings=settings, symbol_mapper=symbol_mapper)