from __future__ import annotations

import functools
import json
import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from trading_platform.agent.market_hours import is_entry_allowed, is_mcx_entry_allowed
from trading_platform.data.feed_staleness import FeedStalenessTracker
from trading_platform.data.tick_v2 import make_tick_v2
from trading_platform.streaming.tick_bus import TickBus

logger = logging.getLogger(__name__)


class RawAngelOneWebSocket:
    """Drop-in replacement for SmartWebSocketV2 that actually delivers ticks.

    PROVEN 2026-08-10. SmartWebSocketV2 opens the socket and then delivers ZERO
    ticks — "Connection closed due to max retry attempts reached" — while a raw
    websocket-client connection using the SAME credentials, headers, URL and
    subscribe payload streams live ticks from the same container seconds later.

    Everything else was eliminated first, so none of these are worth retesting:
    credentials (REST login SUCCESS), DNS/TCP/TLS (clean, TLSv1.3), stale daily
    session, socket leaks, the 3-sockets-per-client cap, the "Bearer " prefix,
    market hours (MCX failed at 21:00 while MCX was OPEN), rate limiting,
    ping_interval/ping_timeout (patched and retested — did NOT help), and the
    subscribe payload (byte-identical to the library's).

    The binary tick format is NOT reimplemented — `_parse_binary_data` is
    borrowed off an uninitialised SmartWebSocketV2 instance, since it is pure
    byte-decoding with no dependency on the socket. That keeps the one genuinely
    valuable part of the library and discards only its broken connect path.

    Interface mirrors SmartWebSocketV2 exactly (`on_open`/`on_data`/`on_error`/
    `on_close`, `connect()`, `subscribe()`, `close_connection()`), so
    `_run_shard` needs no changes.
    """

    ROOT_URI = "wss://smartapisocket.angelone.in/smart-stream"
    HEART_BEAT_INTERVAL = 20
    PING_TIMEOUT = 10
    SUBSCRIBE_ACTION = 1

    def __init__(self, auth_token, api_key, client_code, feed_token, **_kw):
        self.auth_token = _strip_bearer(auth_token)
        self.api_key = api_key
        self.client_code = client_code
        self.feed_token = str(feed_token)
        self.wsapp = None
        self.on_open = None
        self.on_data = None
        self.on_error = None
        self.on_close = None
        self._parser = None

    def _parse(self, payload):
        """Decode one binary frame using the library's own parser."""
        if self._parser is None:
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2  # type: ignore[import]
            # __new__, not __init__: we want ONLY the decoding methods, and
            # __init__ would set up the connection state we are replacing.
            self._parser = SmartWebSocketV2.__new__(SmartWebSocketV2)
        return self._parser._parse_binary_data(payload)

    def subscribe(self, correlation_id, mode, token_list):
        if self.wsapp is None:
            raise RuntimeError("subscribe() before connect()")
        self.wsapp.send(json.dumps({
            "correlationID": correlation_id,
            "action": self.SUBSCRIBE_ACTION,
            "params": {"mode": mode, "tokenList": token_list},
        }))

    def close_connection(self):
        try:
            if self.wsapp is not None:
                self.wsapp.close()
        except Exception as exc:
            note_swallowed("live_feed.raw_ws_close", exc)

    def connect(self):
        """Blocks until the socket closes — same contract as the library."""
        import websocket  # websocket-client, already a pinned dependency

        headers = {
            "Authorization": self.auth_token,
            "x-api-key": self.api_key,
            "x-client-code": self.client_code,
            "x-feed-token": self.feed_token,
        }

        def _on_open(_w):
            if self.on_open:
                self.on_open(self)

        def _on_message(_w, message):
            # Text frames are heartbeats ("pong"); only binary carries ticks.
            if isinstance(message, (bytes, bytearray)):
                try:
                    parsed = self._parse(message)
                except Exception as exc:
                    note_swallowed("live_feed.raw_ws_parse", exc)
                    return
                if parsed and self.on_data:
                    self.on_data(self, parsed)

        def _on_error(_w, err):
            if self.on_error:
                self.on_error(self, err)

        def _on_close(_w, code=None, reason=None):
            if self.on_close:
                self.on_close(self, code, reason)

        self.wsapp = websocket.WebSocketApp(
            self.ROOT_URI, header=headers,
            on_open=_on_open, on_message=_on_message,
            on_error=_on_error, on_close=_on_close,
        )
        # BOTH ping params — and real certificate verification, unlike the
        # library's sslopt={"cert_reqs": ssl.CERT_NONE}.
        self.wsapp.run_forever(
            ping_interval=self.HEART_BEAT_INTERVAL,
            ping_timeout=self.PING_TIMEOUT,
        )


def _make_patched_ws_class(base):
    """SmartWebSocketV2 with certificate verification restored.

    HONEST STATUS: this does NOT fix the dead feed. It was written to test the
    hypothesis that the wrapper's `run_forever(ping_interval=10)` call, which
    omits `ping_timeout`, was why the stock wrapper wedges. **Tested
    2026-08-10: adding ping_timeout did not help** — the patched class opens
    the socket and still receives zero ticks, exactly like the stock one. The
    hypothesis is disproven; keep it disproven rather than re-testing it.

    What IS still true and still unexplained: a RAW websocket-client
    connection with the identical credentials, headers and URL opens and
    streams live ticks, while any path through this wrapper delivers none.
    Since ping config is now ruled out, the remaining difference is the
    SUBSCRIBE path — the raw test sent its own JSON frame in on_open, whereas
    the wrapper builds and sends its own via .subscribe(). That is the next
    thing to bisect. See memory websocket-wrapper-is-the-bug for the full list
    of theories already eliminated (credentials, DNS/TCP/TLS, stale session,
    socket leaks, the 3-socket cap, Bearer prefix, market hours, rate limits).

    Retained despite not fixing the bug because it does restore TLS
    certificate verification: the stock wrapper passes
    `sslopt={"cert_reqs": ssl.CERT_NONE}`, disabling verification for the
    entire market-data feed. That is worth keeping on its own merits.
    """
    import ssl as _ssl
    import websocket as _ws

    class _PatchedSmartWebSocketV2(base):  # type: ignore[misc,valid-type]
        PING_TIMEOUT_SECONDS = 10

        def connect(self):  # noqa: D102 - mirrors the base signature
            headers = {
                "Authorization": self.auth_token,
                "x-api-key": self.api_key,
                "x-client-code": self.client_code,
                "x-feed-token": self.feed_token,
            }
            try:
                self.wsapp = _ws.WebSocketApp(
                    self.ROOT_URI, header=headers,
                    on_open=self._on_open, on_error=self._on_error,
                    on_close=self._on_close, on_data=self._on_data,
                    on_ping=self._on_ping, on_pong=self._on_pong,
                )
                # ping_timeout MUST be < ping_interval, else websocket-client
                # raises rather than connecting.
                interval = max(2, int(getattr(self, "HEART_BEAT_INTERVAL", 10)))
                timeout = min(self.PING_TIMEOUT_SECONDS, max(1, interval - 1))
                self.wsapp.run_forever(
                    sslopt={"cert_reqs": _ssl.CERT_REQUIRED},
                    ping_interval=interval,
                    ping_timeout=timeout,
                )
            except Exception as exc:
                logger.error("LiveTickFeed websocket connect failed: %s", exc)
                raise

    return _PatchedSmartWebSocketV2


def _env_flag(name: str, default: bool = False) -> bool:
    import os
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _strip_bearer(token: str) -> str:
    """Angel One hands back 'Bearer <jwt>'; the socket wants the bare JWT."""
    t = str(token or "")
    return t[7:] if t.startswith("Bearer ") else t


# MCX runs far later than NSE (09:00-23:30 IST vs 09:15-15:30). The feed
# subscribes MCX contracts too, so gating reconnects on NSE hours alone would
# silence the feed for the entire 15:30-23:30 commodity session — caught
# 2026-08-10 when an MCX-hours test was proposed as a way to check the socket
# outside equity hours.
_MCX_OPEN_MINUTES = 9 * 60          # 09:00 IST
_MCX_CLOSE_MINUTES = 23 * 60 + 30   # 23:30 IST


def _market_is_open_now() -> bool:
    """True during NSE **or** MCX trading hours. Fails OPEN (returns True) if
    the check itself errors — a broken clock must not permanently silence a
    feed that open positions depend on."""
    try:
        from trading_platform.agent.market_hours import is_market_open, now_ist
        now = now_ist()
        if is_market_open(now):
            return True
        # Weekday MCX session (commodities do not trade at weekends either).
        if now.weekday() >= 5:
            return False
        minutes = now.hour * 60 + now.minute
        return _MCX_OPEN_MINUTES <= minutes <= _MCX_CLOSE_MINUTES
    except Exception:
        return True

# Exchange-mode constants for SmartWebSocketV2
# Source: Angel One SmartWebSocketV2 documentation
NSE_CM = 1    # NSE Cash Market
NSE_FO = 2    # NSE Futures & Options
BSE_CM = 3    # BSE Cash Market
BSE_FO = 4    # BSE Futures & Options
MCX_FO = 5    # MCX Futures & Options

_EXCHANGE_MODE_MAP = {
    "NSE": NSE_CM,
    "BSE": BSE_CM,
    "NFO": NSE_FO,
    "BFO": BSE_FO,   # BSE Futures & Options — used for SENSEX/BANKEX derivatives
    "MCX": MCX_FO,
}


@dataclass
class Tick:
    """Normalised real-time tick (Tick v2, REDESIGN_PROMPT.md §3.2).
    Angel One's SmartWebSocketV2 mode-3 quotes never populate bid/ask/oi,
    so those fields default to None; a future Depth-20 subscription would
    populate them."""

    symbol: str
    token: str
    exchange: str
    last_price: float
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    oi: int | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "token": self.token,
            "exchange": self.exchange,
            "last_price": self.last_price,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "timestamp": self.timestamp.isoformat(),
            "bid": self.bid,
            "ask": self.ask,
            "bid_qty": self.bid_qty,
            "ask_qty": self.ask_qty,
            "oi": self.oi,
        }


TickHandler = Callable[[Tick], None]


@dataclass
class _Shard:
    """One SmartWebSocketV2 connection and the symbols routed to it.

    Angel One caps a single connection at 1000 tokens and a client code at
    3 concurrent connections (REDESIGN_PROMPT.md §3.2) — LiveTickFeed shards
    subscriptions across up to 3 of these instead of silently truncating at
    1000 symbols. Each shard reconnects independently: a drop on one socket
    must not affect the others.
    """

    index: int
    correlation_id: str
    symbols: list[str] = field(default_factory=list)
    ws: object = None
    thread: threading.Thread | None = None
    # Per-socket heartbeat (REDESIGN_PROMPT.md §3.2's "silent half-open
    # sockets are a real Angel One failure mode" — a socket can stay open
    # without ever firing on_close while delivering nothing). connected_at
    # gives a freshly-opened socket a grace period before on_data ever
    # fires; last_message_at then takes over once it does.
    connected_at: datetime | None = None
    last_message_at: datetime | None = None


class LiveTickFeed:
    """Angel One SmartWebSocketV2 wrapper.

    Usage:
        feed = LiveTickFeed(settings)
        feed.subscribe(["NIFTY", "RELIANCE"], handler=my_handler)
        feed.start()
        ...
        feed.stop()

    The feed runs in a background thread. Each tick delivered by the
    WebSocket is parsed and forwarded to all registered handlers.

    Subscriptions are sharded across up to `_MAX_SOCKETS` WebSocket
    connections, `_MAX_TOKENS_PER_SOCKET` tokens each (Angel One's own
    limits) — see `_Shard`. With today's default `live_feed_max_symbols`
    (well under 1000) this is inert: everything runs on shard 0 exactly as
    a single-socket feed always has. Sharding only activates once
    subscriptions actually exceed 1000 tokens (e.g. a full option chain).
    """

    def __init__(
        self,
        settings,
        staleness_tracker: FeedStalenessTracker | None = None,
        get_protected_symbols: Callable[[], set] | None = None,
    ) -> None:
        self._settings = settings
        self._handlers: list[TickHandler] = []
        self._token_map: dict[str, str] = {}          # symbol -> token
        self._reverse_token_map: dict[str, str] = {}  # token  -> symbol (O(1) lookup on every tick)
        self._exchange_map: dict[str, str] = {}       # symbol -> exchange string
        self._thread: threading.Thread | None = None
        self._running = False
        self._reconnect_pending = False
        self._last_ticks: dict[str, Tick] = {}
        self._subscribed_symbols: list[str] = []
        self._lock = threading.Lock()
        # NEW: per-connection UUID for debuggability (replaces hardcoded correlation)
        self._connection_id: str = str(uuid.uuid4())[:12]
        # NEW: Redis Streams tick bus (publishes normalized ticks)
        self._tick_bus = TickBus.get_instance()
        # Sharding state — see _Shard's docstring. Populated lazily: symbols
        # added via subscribe()/add_subscriptions() before start() just sit
        # in _subscribed_symbols until _run() assigns them to shards.
        self._shards: list[_Shard] = []
        self._shard_of_symbol: dict[str, int] = {}
        self._smart_ws_cls = None          # resolved once in _run(), reused per shard
        self._session: dict | None = None  # shared login, reused across all shards
        self._feed_token: str | None = None
        self._watchdog_thread: threading.Thread | None = None
        # Per-symbol last-tick tracker — answers "did THIS symbol tick recently?"
        self.staleness_tracker = staleness_tracker or FeedStalenessTracker()
        self._max_symbols = getattr(settings, "live_feed_max_symbols", None)
        # Called fresh on every add_subscriptions() to get the CURRENT open
        # positions — symbols in this set always get subscribed even past
        # the nominal cap; dropping a live subscription for an open position
        # is exactly the 2026-08-05 bug this exists to prevent. Injected
        # rather than referencing a portfolio directly, so this module stays
        # free of a runtime/portfolio dependency.
        self._get_protected_symbols = get_protected_symbols or (lambda: set())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_instruments(self, instruments: list) -> None:
        """Register Instrument objects so the feed knows symbol→token mappings."""
        for inst in instruments:
            if inst.token:
                token = str(inst.token)
                self._token_map[inst.symbol] = token
                self._reverse_token_map[token] = inst.symbol
                self._exchange_map[inst.symbol] = inst.exchange.value

    def add_handler(self, handler: TickHandler) -> None:
        with self._lock:
            self._handlers.append(handler)

    def subscribe(self, symbols: list[str], handler: TickHandler | None = None) -> None:
        if handler:
            self.add_handler(handler)
        upper = [s.upper() for s in symbols]
        with self._lock:
            self._subscribed_symbols = upper
        self._subscribe_symbols(upper)

    def add_subscriptions(self, symbols: list[str]) -> None:
        """Add symbols to the live subscription list, respecting
        live_feed_max_symbols — except for symbols backing an open position,
        which are always subscribed regardless of the cap (see __init__'s
        get_protected_symbols)."""
        with self._lock:
            existing = set(self._subscribed_symbols)
            additions = [s.upper() for s in symbols if s and s.upper() not in existing]
            if not additions:
                return
            if self._max_symbols:
                try:
                    protected = {s.upper() for s in self._get_protected_symbols()}
                except Exception as exc:
                    logger.warning("LiveTickFeed: get_protected_symbols failed: %s", exc)
                    protected = set()
                protected_additions = [s for s in additions if s in protected]
                optional_additions = [s for s in additions if s not in protected]
                room = max(0, self._max_symbols - len(existing) - len(protected_additions))
                if len(optional_additions) > room:
                    dropped = optional_additions[room:]
                    optional_additions = optional_additions[:room]
                    logger.warning(
                        "LiveTickFeed: subscription cap (%d) reached — not subscribing %s",
                        self._max_symbols, dropped,
                    )
                additions = protected_additions + optional_additions
                if not additions:
                    return
                if len(existing) + len(additions) > self._max_symbols:
                    logger.warning(
                        "LiveTickFeed: %d open-position symbol(s) pushed subscriptions past "
                        "the nominal cap (%d) — subscribing them anyway",
                        len(protected_additions), self._max_symbols,
                    )
            self._subscribed_symbols.extend(additions)
        self._subscribe_symbols(additions)

    def start(self) -> None:
        if self._running:
            return
        if not self._settings.angel_one_configured:
            logger.warning("LiveTickFeed: Angel One credentials not configured — feed will not start")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="live-tick-feed")
        self._thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="live-tick-feed-watchdog",
        )
        self._watchdog_thread.start()
        logger.info("LiveTickFeed started")

    def stop(self) -> None:
        self._running = False
        for shard in list(self._shards):
            if shard.ws is not None:
                try:
                    shard.ws.close_connection()
                except Exception:
                    pass
        logger.info("LiveTickFeed stopped")

    def latest_tick(self, symbol: str) -> Tick | None:
        with self._lock:
            return self._last_ticks.get(symbol.upper())

    def latest_price(self, symbol: str) -> float | None:
        tick = self.latest_tick(symbol)
        return tick.last_price if tick else None

    @property
    def is_running(self) -> bool:
        return self._running

    def subscribed_symbols(self) -> list[str]:
        with self._lock:
            return list(self._subscribed_symbols)

    def inject_tick(self, tick: Tick) -> None:
        """Test/replay hook — record a tick as if it arrived from the
        WebSocket. Used by the paper engine and unit tests so the
        staleness tracker doesn't see PAPER mode as "no ticks ever."

        NEW: also publishes to the tick bus so replay exercises the full pipeline.
        """
        with self._lock:
            self._last_ticks[tick.symbol] = tick
            handlers = list(self._handlers)
        self.staleness_tracker.record(tick.symbol, tick.timestamp)
        # NEW: publish TickV2 to tick bus (replay exercises full pipeline)
        tick_v2 = make_tick_v2(tick)
        if tick_v2 is not None:
            self._tick_bus.publish_tick_threadsafe(tick_v2)
        for handler in handlers:
            try:
                handler(tick)
            except Exception as exc:
                logger.warning("Tick handler error (inject): %s", exc)

    def snapshot(self) -> dict:
        with self._lock:
            running = self._running
            subscribed = list(self._subscribed_symbols)
            available = list(self._last_ticks.keys())
            count = len(self._last_ticks)
        # Per-symbol staleness restricted to currently subscribed symbols
        # so the snapshot reflects "are the things we care about live?"
        staleness = self.staleness_tracker.snapshot(subscribed) if subscribed else self.staleness_tracker.snapshot()
        now = datetime.now(timezone.utc)
        shards = []
        for s in self._shards:
            reference = s.last_message_at or s.connected_at
            shards.append({
                "index": s.index,
                "connected": s.ws is not None,
                "symbol_count": len(s.symbols),
                "last_message_age_seconds": (now - reference).total_seconds() if reference else None,
            })
        return {
            "running": running,
            "subscribed_symbols": subscribed,
            "available_symbols": available,
            "tick_count": count,
            "staleness": staleness,
            "shards": shards,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    _MAX_RETRIES = 10
    _CLOSED_MARKET_POLL_SECONDS = 60      # how often to re-check for the open
    _BASE_BACKOFF = 5      # seconds before first retry
    _MAX_BACKOFF = 300     # cap at 5 minutes
    _MAX_TOKENS_PER_SOCKET = 1000  # Angel One SmartWebSocketV2 limit per connection
    _MAX_SOCKETS = 3               # Angel One limit per client code
    _SOCKET_HEARTBEAT_TIMEOUT_SECONDS = 30    # silent this long during market hours -> force reconnect
    _SOCKET_HEARTBEAT_CHECK_INTERVAL_SECONDS = 10

    def _login(self) -> tuple[dict, str] | None:
        """One Angel One login, returning (session, feed_token) or None on failure.

        Separated from _run's reconnect loop on purpose: a WebSocket drop does
        NOT invalidate the JWT, so re-running this on every reconnect burns
        Angel One's login quota for no reason — and that quota is far stricter
        than the socket's own limits (confirmed 2026-07-28: repeated container
        restarts during testing tripped a login-level rate-limit stop well
        before any data-rate issue). _run() logs in once per feed lifecycle and
        only calls this again if the cached token itself starts failing.
        """
        try:
            from SmartApi import SmartConnect  # type: ignore[import]
            import pyotp

            connect = SmartConnect(api_key=self._settings.angel_one_api_key)
            totp = pyotp.TOTP(self._settings.angel_one_totp_secret).now()
            session = connect.generateSession(
                self._settings.angel_one_client_code,
                self._settings.angel_one_pin,
                totp,
            )
            feed_token = connect.getfeedToken()
            return session, feed_token
        except Exception as exc:
            if _is_rate_limit_error(exc):
                logger.warning("LiveTickFeed login rate-limited by Angel One; feed stopped until manually restarted")
            else:
                logger.error("LiveTickFeed login error: %s", exc)
            return None

    def _shard_with_room(self) -> "_Shard | None":
        for shard in self._shards:
            if len(shard.symbols) < self._MAX_TOKENS_PER_SOCKET:
                return shard
        return None

    def _assign_symbols_to_shards(self, symbols: list[str]) -> dict[int, list[str]]:
        """Distribute `symbols` across shards, filling existing shards
        before creating new ones, capped at `_MAX_SOCKETS` sockets.

        Returns {shard_index: [symbols newly assigned to it]} — callers use
        this to tell a brand-new shard (needs a connection spun up) apart
        from an already-connected one (just needs an incremental subscribe).
        """
        newly_assigned: dict[int, list[str]] = {}
        with self._lock:
            for symbol in symbols:
                if symbol in self._shard_of_symbol:
                    continue
                shard = self._shard_with_room()
                if shard is None:
                    if len(self._shards) >= self._MAX_SOCKETS:
                        logger.warning(
                            "LiveTickFeed: all %d sockets full (%d tokens each) — "
                            "dropping subscription for %s",
                            self._MAX_SOCKETS, self._MAX_TOKENS_PER_SOCKET, symbol,
                        )
                        continue
                    shard = _Shard(index=len(self._shards), correlation_id=str(uuid.uuid4()))
                    self._shards.append(shard)
                shard.symbols.append(symbol)
                self._shard_of_symbol[symbol] = shard.index
                newly_assigned.setdefault(shard.index, []).append(symbol)
        return newly_assigned

    def _run(self) -> None:
        try:
            import SmartApi.smartWebSocketV2 as _smart_ws_module  # type: ignore[import]
        except ImportError:
            logger.error("smartapi-python not installed — install with: pip install smartapi-python")
            self._running = False
            return

        # Default RAW (proven 2026-08-10 — see RawAngelOneWebSocket's
        # docstring): SmartWebSocketV2's own connect() delivers zero ticks
        # while a raw websocket-client connection with identical credentials
        # streams live data from the same box. Set
        # LIVE_FEED_USE_LIBRARY_WEBSOCKET=true to fall back to the stock
        # library (e.g. if a smartapi-python upgrade fixes it upstream).
        #
        # Resolved through the MODULE attribute, not imported directly, so
        # `mock.patch("SmartApi.smartWebSocketV2.SmartWebSocketV2", ...)`
        # (the existing test seam) still intercepts the library path — tests
        # never touch a real socket regardless of which branch runs live.
        if _env_flag("LIVE_FEED_USE_LIBRARY_WEBSOCKET", False):
            self._smart_ws_cls = _smart_ws_module.SmartWebSocketV2
        else:
            self._smart_ws_cls = RawAngelOneWebSocket

        logged_in = self._login()
        if logged_in is None:
            self._running = False
            return
        self._session, self._feed_token = logged_in

        with self._lock:
            pending_symbols = list(self._subscribed_symbols)
        self._assign_symbols_to_shards(pending_symbols)

        with self._lock:
            shards = list(self._shards)
        if not shards:
            # No symbols yet — still stand up shard 0 so add_subscriptions()
            # has a connection to route new symbols onto once they arrive.
            shard = _Shard(index=0, correlation_id=str(uuid.uuid4()))
            with self._lock:
                self._shards.append(shard)
            shards = [shard]

        # Extra shards (index >= 1) reconnect independently on their own
        # thread. Shard 0 runs on THIS thread so _run() behaves exactly like
        # the old single-socket feed whenever only one shard is needed —
        # which is every deployment today (live_feed_max_symbols defaults
        # well under 1000); sharding only engages past 1000 tokens.
        for shard in shards[1:]:
            self._spawn_shard_thread(shard)
        self._run_shard(shards[0])

    def _spawn_shard_thread(self, shard: "_Shard") -> None:
        thread = threading.Thread(
            target=self._run_shard, args=(shard,), daemon=True,
            name=f"live-tick-feed-shard-{shard.index}",
        )
        shard.thread = thread
        thread.start()

    def _watchdog_loop(self) -> None:
        """Runs for the feed's whole lifetime, independent of any shard's
        own connect/reconnect loop — see `_check_shard_heartbeats`."""
        while self._running:
            time.sleep(self._SOCKET_HEARTBEAT_CHECK_INTERVAL_SECONDS)
            if not self._running:
                break
            try:
                self._check_shard_heartbeats()
            except Exception as exc:
                logger.warning("LiveTickFeed watchdog error: %s", exc)

    def _check_shard_heartbeats(self) -> None:
        """A socket can sit open without ever firing on_close while
        delivering nothing — Angel One's real "silent half-open socket"
        failure mode (REDESIGN_PROMPT.md §3.2). A quiet socket outside
        market hours is normal, not a failure, so this only judges shards
        while a session is actually live."""
        if not (is_entry_allowed() or is_mcx_entry_allowed()):
            return
        now = datetime.now(timezone.utc)
        for shard in list(self._shards):
            if shard.ws is None or not shard.symbols:
                continue  # not connected yet, or nothing to tick — not this watchdog's job
            reference = shard.last_message_at or shard.connected_at
            if reference is None:
                continue  # hasn't finished its initial connect yet
            age = (now - reference).total_seconds()
            if age > self._SOCKET_HEARTBEAT_TIMEOUT_SECONDS:
                logger.warning(
                    "LiveTickFeed shard %d silent for %.1fs during market hours — forcing reconnect",
                    shard.index, age,
                )
                self._force_reconnect_shard(shard)

    def _force_reconnect_shard(self, shard: "_Shard") -> None:
        """Closes one shard's socket so its own _run_shard loop's blocking
        `connect()` returns and goes through the normal backoff/reconnect —
        other shards are untouched. Clears the heartbeat clocks so the
        freshly (re)connected socket gets a full grace period again instead
        of being judged stale before it's had a chance to reconnect."""
        shard.connected_at = None
        shard.last_message_at = None
        if shard.ws is None:
            return
        try:
            shard.ws.close_connection()
        except Exception as exc:
            logger.warning("LiveTickFeed shard %d force-reconnect close failed: %s", shard.index, exc)

    def _run_shard(self, shard: "_Shard") -> None:
        """Connect/reconnect loop for one socket — the same logic that used
        to be the entire `_run()`, now scoped to a single shard's token
        subset with its own independent retry/backoff state."""
        client_code = self._settings.angel_one_client_code

        # Confirmed 2026-08-05: this used to stop trying after _MAX_RETRIES and
        # stay dead until the (market-hours-gated) scan-loop watchdog noticed
        # and restarted it — unacceptable for a feed with open positions
        # depending on it. Past _MAX_RETRIES this keeps retrying forever at
        # the capped backoff instead of giving up; the retry counter still
        # governs backoff growth and how often a fresh re-login is attempted.
        retries = 0
        gave_up_logged = False
        closed_wait_logged = False
        while self._running:
            # Never reconnect outside market hours. There is nothing to stream
            # when the exchange is shut, so every attempt is pure waste — and
            # not free: at the capped ~5-min backoff this burned ~720 handshakes
            # across the 2026-08-08/09 weekend, after which Angel One refused
            # the socket entirely (109 logged "max retry" failures, and the
            # block only cleared after ~10h of genuine idleness). Waiting for
            # the open is both correct and the thing that protects the quota.
            if not _market_is_open_now():
                if not closed_wait_logged:
                    logger.info(
                        "LiveTickFeed shard %d: market closed — pausing reconnects until open",
                        shard.index,
                    )
                    closed_wait_logged = True
                time.sleep(self._CLOSED_MARKET_POLL_SECONDS)
                retries = 0            # start the session with a clean budget
                continue
            closed_wait_logged = False
            if retries > 0:
                backoff_retries = min(retries, self._MAX_RETRIES)
                base_backoff = min(self._BASE_BACKOFF * (2 ** (backoff_retries - 1)), self._MAX_BACKOFF)
                backoff = base_backoff + random.uniform(0, min(5.0, base_backoff * 0.25))
                if retries >= self._MAX_RETRIES and not gave_up_logged:
                    logger.critical(
                        "LiveTickFeed shard %d has failed to reconnect %d times — will keep "
                        "retrying every ~%ds indefinitely rather than staying dead",
                        shard.index, retries, self._MAX_BACKOFF,
                    )
                    gave_up_logged = True
                logger.warning(
                    "LiveTickFeed shard %d reconnect attempt %d — waiting %.1fs",
                    shard.index, retries, backoff,
                )
                time.sleep(backoff)
                if not self._running:
                    break

            try:
                # Angel One returns jwtToken as "Bearer eyJ..." (verified
                # 2026-08-10). SmartWebSocketV2 wants the RAW token — passing
                # the prefixed string makes the server reject the handshake,
                # which the library then surfaces only as the useless
                # "max retry attempts reached". Strip defensively: harmless if
                # a future API version drops the prefix.
                shard.ws = self._smart_ws_cls(
                    _strip_bearer(self._session["data"]["jwtToken"]),
                    self._settings.angel_one_api_key,
                    client_code,
                    self._feed_token,
                )

                shard.ws.on_open = functools.partial(self._on_open, shard=shard)
                shard.ws.on_data = functools.partial(self._on_data, shard=shard)
                shard.ws.on_error = functools.partial(self._on_error, shard=shard)
                shard.ws.on_close = functools.partial(self._on_close, shard=shard)

                self._reconnect_pending = False
                shard.ws.connect()  # blocks until disconnect, using the cached token
                # If connect() returns and _running is still True, reconnect
                retries += 1
                if retries < self._MAX_RETRIES:
                    gave_up_logged = False  # a connection that held resets the "gave up" notice
            except Exception as exc:
                logger.error("LiveTickFeed shard %d connection error: %s", shard.index, exc)
                retries += 1
                # The cached token itself may have gone stale (very long-running
                # feed outliving the JWT) rather than this being a plain socket
                # drop. Re-login periodically (not every attempt — login has a
                # stricter rate limit than the socket, see _login's docstring)
                # instead of assuming a bad token forever; a failed re-login no
                # longer stops the loop, it just keeps retrying with whatever
                # session it has at the capped backoff. Shared across shards
                # since one client-code login token serves all connections.
                if retries % self._MAX_RETRIES == 0:
                    refreshed = self._login()
                    if refreshed is not None:
                        self._session, self._feed_token = refreshed

        if shard.index == 0:
            self._running = False

    def _on_open(self, ws, shard: "_Shard | None" = None) -> None:
        index = shard.index if shard else 0
        logger.info(
            "LiveTickFeed WebSocket connected (shard %d) [%s]", index, self._connection_id,
        )
        if shard is not None:
            # Fresh grace period for the heartbeat watchdog: judge silence
            # from this connection moment until the first on_data fires.
            shard.connected_at = datetime.now(timezone.utc)
            shard.last_message_at = None
        symbols = shard.symbols if shard is not None else None
        token_list = self._build_token_list(symbols)
        if token_list:
            correlation_id = shard.correlation_id if shard else str(uuid.uuid4())
            ws.subscribe(correlation_id, 3, token_list)  # mode 3 = full snap quote

    def _build_token_list(self, symbols: list[str] | None = None) -> list[dict]:
        token_list = []
        symbols = symbols or getattr(self, "_subscribed_symbols", list(self._token_map.keys()))
        for symbol in symbols:
            token = self._token_map.get(symbol)
            exchange = self._exchange_map.get(symbol, "NSE")
            if token:
                exchange_type = _EXCHANGE_MODE_MAP.get(exchange, NSE_CM)
                token_list.append({"exchangeType": exchange_type, "tokens": [token]})
        return token_list

    def _subscribe_symbols(self, symbols: list[str]) -> None:
        if not self._running:
            return
        newly_assigned = self._assign_symbols_to_shards(symbols)
        for shard_index, shard_symbols in newly_assigned.items():
            shard = self._shards[shard_index]
            if shard.ws is None:
                # Brand new shard (either first-ever or all others were full)
                # — its own _on_open subscribes shard.symbols once connected.
                self._spawn_shard_thread(shard)
                continue
            token_list = self._build_token_list(shard_symbols)
            if not token_list:
                continue
            try:
                shard.ws.subscribe(shard.correlation_id, 3, token_list)
            except Exception as exc:
                logger.warning("LiveTickFeed shard %d subscribe update failed: %s", shard_index, exc)

    def _on_data(self, ws, message, shard: "_Shard | None" = None) -> None:
        if shard is not None:
            # Any message at all proves this socket is alive, independent of
            # whether it parses into a usable tick — that's what the
            # heartbeat watchdog checks, not staleness_tracker (which is
            # per-symbol and only updated below on a successful parse).
            shard.last_message_at = datetime.now(timezone.utc)
        try:
            tick = self._parse(message)
            if tick:
                with self._lock:
                    self._last_ticks[tick.symbol] = tick
                    handlers = list(self._handlers)
                # Record tick freshness per-symbol so the readiness gate
                # can answer "did this exact symbol tick in the last 15s?"
                self.staleness_tracker.record(tick.symbol, tick.timestamp)
                # NEW: normalize → TickV2 → publish to tick bus
                tick_v2 = make_tick_v2(tick)
                if tick_v2 is not None:
                    self._tick_bus.publish_tick_threadsafe(tick_v2)
                for handler in handlers:
                    try:
                        handler(tick)
                    except Exception as exc:
                        logger.warning("Tick handler error: %s", exc)
        except Exception as exc:
            logger.warning("LiveTickFeed parse error: %s", exc)

    def _on_error(self, ws, error, shard: "_Shard | None" = None) -> None:
        logger.error("LiveTickFeed WebSocket error (shard %d): %s", shard.index if shard else 0, error)

    def _on_close(self, ws, shard: "_Shard | None" = None) -> None:
        logger.warning(
            "LiveTickFeed WebSocket closed (shard %d) [%s] — will reconnect if still running",
            shard.index if shard else 0, self._connection_id,
        )

    def _parse(self, message) -> Tick | None:
        if isinstance(message, (bytes, bytearray)):
            try:
                message = json.loads(message.decode("utf-8"))
            except Exception:
                return None
        if not isinstance(message, dict):
            return None

        token = str(message.get("token", ""))
        # O(1) reverse lookup; falls back to raw token string if unmapped
        symbol = self._reverse_token_map.get(token, token)
        exchange = self._exchange_map.get(symbol, "NSE")

        ltp = float(message.get("last_traded_price", 0)) / 100  # Angel One sends paise
        open_ = float(message.get("open_price_of_the_day", 0)) / 100
        high = float(message.get("high_price_of_the_day", 0)) / 100
        low = float(message.get("low_price_of_the_day", 0)) / 100
        close = float(message.get("closed_price", ltp)) / 100
        volume = int(message.get("volume_trade_for_the_day", 0))

        if ltp <= 0:
            return None

        return Tick(
            symbol=symbol,
            token=token,
            exchange=exchange,
            last_price=ltp,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
        )


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "exceeding access rate" in text or "rate limit" in text or "too many requests" in text
