from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any

from trading_platform.config import Settings
from trading_platform.domain.models import Instrument, MarketBar


def _rate_limited(err_text: str) -> bool:
    t = err_text.lower()
    return "access rate" in t or "exceeding" in t


class AngelOneHistoricalDataProvider:
    """Historical candles via Angel One SmartConnect.getCandleData()."""

    # Process-wide candle-call pacing. Angel One throttles the candle endpoint
    # hard, and it is hit from MANY paths (decision-pipeline bars, short-vol ATM
    # option prices, previews). Per-caller throttles are not enough — the paths
    # race each other and trip "exceeding access rate", which forces the
    # decision pipeline onto SYNTHETIC data (the cardinal-sin fallback). A single
    # class-level lock + min-interval serialises every candle call across threads
    # so the aggregate stays under the limit; a rate-limit still triggers bounded
    # backoff-retry here rather than propagating to the synthetic fallback.
    _rate_lock = threading.Lock()
    _last_call_ts: float = 0.0

    def __init__(self, settings: Settings, smart_api: Any | None = None):
        self.settings = settings
        self._smart_api = smart_api

    def _throttle(self) -> None:
        interval = float(os.getenv("ANGEL_ONE_CANDLE_MIN_INTERVAL", "0.4"))
        with AngelOneHistoricalDataProvider._rate_lock:
            wait = interval - (time.monotonic() - AngelOneHistoricalDataProvider._last_call_ts)
            if wait > 0:
                time.sleep(wait)
            AngelOneHistoricalDataProvider._last_call_ts = time.monotonic()

    def get_candles(
        self,
        instrument: Instrument,
        from_dt: datetime,
        to_dt: datetime,
        interval: str = "ONE_DAY",
    ) -> list[MarketBar]:
        if not _is_angel_one_token(instrument.token):
            raise RuntimeError(
                f"Angel One candle request skipped for {instrument.symbol}: synthetic/non-numeric token"
            )
        params = {
            "exchange": instrument.exchange.value,
            "symboltoken": instrument.token,
            "interval": interval,
            "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
        }
        last_response = None
        last_exc: Exception | None = None
        retries = max(1, int(os.getenv("ANGEL_ONE_CANDLE_RETRIES", "3")))
        for rl_attempt in range(retries):
            self._throttle()
            # Try with cached session first; re-authenticate once ONLY for auth-like
            # failures. Rate-limit responses must NOT reset the session: the login
            # endpoint has a far stricter quota, so re-login on every rate-limited
            # candle call snowballs into a full API lockout (observed 2026-07-14).
            for attempt in range(2):
                smart_api = self._smart_api or self._login()
                try:
                    last_response = smart_api.getCandleData(params)
                    last_exc = None
                except Exception as exc:
                    last_response = None
                    last_exc = exc
                if last_response and last_response.get("status"):
                    return [_parse_candle(instrument.symbol, candle) for candle in last_response.get("data") or []]
                if _rate_limited(str(last_exc or last_response or "")):
                    break  # rate-limited: do NOT re-login; fall through to backoff
                # Session likely expired — force re-login on next attempt
                self._smart_api = None
            # Rate-limited: back off and retry the whole call (throttle re-paces it)
            if _rate_limited(str(last_exc or last_response or "")):
                if rl_attempt < retries - 1:
                    time.sleep(min(4.0, 0.8 * (2 ** rl_attempt)))
                    continue
                raise RuntimeError(f"rate_limited: {str(last_exc or last_response)[:100]}")
            break  # non-rate-limit failure — stop retrying
        if last_exc is not None:
            raise RuntimeError(
                f"Angel One candle request failed after re-auth for {instrument.symbol}: {last_exc}"
            ) from last_exc
        raise RuntimeError(f"Angel One candle request failed after re-auth: {last_response}")

    def _login(self):
        if not self.settings.angel_one_configured:
            raise RuntimeError("Angel One credentials are required for historical candle downloads")
        try:
            import pyotp
            from SmartApi import SmartConnect
        except ImportError as exc:
            raise RuntimeError("smartapi-python and pyotp are required for Angel One historical data") from exc

        smart_api = SmartConnect(self.settings.angel_one_api_key)
        totp = pyotp.TOTP(self.settings.angel_one_totp_secret).now()
        session = smart_api.generateSession(
            self.settings.angel_one_client_code,
            self.settings.angel_one_pin,
            totp,
        )
        if not session.get("status"):
            raise RuntimeError(f"Angel One login failed: {session}")
        self._smart_api = smart_api
        return smart_api


def _is_angel_one_token(token: str) -> bool:
    return bool(str(token).strip().isdigit())


def _parse_candle(symbol: str, candle: list[Any]) -> MarketBar:
    if len(candle) < 6:
        raise ValueError(f"Unexpected candle payload for {symbol}: {candle}")
    timestamp = datetime.fromisoformat(str(candle[0]).replace("Z", "+00:00"))
    return MarketBar(
        timestamp=timestamp,
        symbol=symbol,
        open=float(candle[1]),
        high=float(candle[2]),
        low=float(candle[3]),
        close=float(candle[4]),
        volume=int(float(candle[5] or 0)),
    )
