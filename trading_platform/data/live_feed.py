from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

logger = logging.getLogger(__name__)

# Exchange-mode constants for SmartWebSocketV2
# Source: Angel One SmartWebSocketV2 documentation
NSE_CM = 1    # NSE Cash Market
NSE_FO = 2    # NSE Futures & Options
BSE_CM = 3    # BSE Cash Market
BSE_FO = 4    # BSE Futures & Options

_EXCHANGE_MODE_MAP = {
    "NSE": NSE_CM,
    "BSE": BSE_CM,
    "NFO": NSE_FO,
    "BFO": BSE_FO,
}


@dataclass
class Tick:
    """Normalised real-time tick from Angel One SmartWebSocketV2."""

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
        }


TickHandler = Callable[[Tick], None]


class LiveTickFeed:
    """Angel One SmartWebSocketV2 wrapper.

    Usage:
        feed = LiveTickFeed(settings)
        feed.subscribe(["NIFTY", "RELIANCE"], handler=my_handler)
        feed.start()
        ...
        feed.stop()

    The feed runs in a background thread.  Each tick delivered by the
    WebSocket is parsed and forwarded to all registered handlers.
    """

    def __init__(self, settings) -> None:
        self._settings = settings
        self._handlers: list[TickHandler] = []
        self._token_map: dict[str, str] = {}   # symbol -> token
        self._exchange_map: dict[str, str] = {}  # symbol -> exchange string
        self._ws = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_ticks: dict[str, Tick] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_instruments(self, instruments: list) -> None:
        """Register Instrument objects so the feed knows symbol→token mappings."""
        for inst in instruments:
            if inst.token:
                self._token_map[inst.symbol] = str(inst.token)
                self._exchange_map[inst.symbol] = inst.exchange.value

    def add_handler(self, handler: TickHandler) -> None:
        self._handlers.append(handler)

    def subscribe(self, symbols: list[str], handler: TickHandler | None = None) -> None:
        if handler:
            self.add_handler(handler)
        self._subscribed_symbols = [s.upper() for s in symbols]

    def start(self) -> None:
        if self._running:
            return
        if not self._settings.angel_one_configured:
            logger.warning("LiveTickFeed: Angel One credentials not configured — feed will not start")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="live-tick-feed")
        self._thread.start()
        logger.info("LiveTickFeed started")

    def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                self._ws.close_connection()
            except Exception:
                pass
        logger.info("LiveTickFeed stopped")

    def latest_tick(self, symbol: str) -> Tick | None:
        with self._lock:
            return self._last_ticks.get(symbol.upper())

    def latest_price(self, symbol: str) -> float | None:
        tick = self.latest_tick(symbol)
        return tick.last_price if tick else None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "subscribed_symbols": list(self._last_ticks.keys()),
                "tick_count": len(self._last_ticks),
            }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2  # type: ignore[import]
        except ImportError:
            logger.error("smartapi-python not installed — install with: pip install smartapi-python")
            self._running = False
            return

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
            client_code = self._settings.angel_one_client_code

            self._ws = SmartWebSocketV2(
                session["data"]["jwtToken"],
                self._settings.angel_one_api_key,
                client_code,
                feed_token,
            )

            self._ws.on_open = self._on_open
            self._ws.on_data = self._on_data
            self._ws.on_error = self._on_error
            self._ws.on_close = self._on_close

            self._ws.connect()
        except Exception as exc:
            logger.error("LiveTickFeed connection error: %s", exc)
            self._running = False

    def _on_open(self, ws) -> None:
        logger.info("LiveTickFeed WebSocket connected")
        token_list = self._build_token_list()
        if token_list:
            self._ws.subscribe("abc123", 3, token_list)  # mode 3 = full snap quote

    def _build_token_list(self) -> list[dict]:
        token_list = []
        symbols = getattr(self, "_subscribed_symbols", list(self._token_map.keys()))
        for symbol in symbols:
            token = self._token_map.get(symbol)
            exchange = self._exchange_map.get(symbol, "NSE")
            if token:
                exchange_type = _EXCHANGE_MODE_MAP.get(exchange, NSE_CM)
                token_list.append({"exchangeType": exchange_type, "tokens": [token]})
        return token_list

    def _on_data(self, ws, message) -> None:
        try:
            tick = self._parse(message)
            if tick:
                with self._lock:
                    self._last_ticks[tick.symbol] = tick
                for handler in self._handlers:
                    try:
                        handler(tick)
                    except Exception as exc:
                        logger.warning("Tick handler error: %s", exc)
        except Exception as exc:
            logger.warning("LiveTickFeed parse error: %s", exc)

    def _on_error(self, ws, error) -> None:
        logger.error("LiveTickFeed WebSocket error: %s", error)

    def _on_close(self, ws) -> None:
        logger.warning("LiveTickFeed WebSocket closed")
        self._running = False

    def _parse(self, message) -> Tick | None:
        if isinstance(message, (bytes, bytearray)):
            try:
                message = json.loads(message.decode("utf-8"))
            except Exception:
                return None
        if not isinstance(message, dict):
            return None

        token = str(message.get("token", ""))
        # Reverse-lookup symbol from token
        symbol = next((s for s, t in self._token_map.items() if t == token), token)
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
