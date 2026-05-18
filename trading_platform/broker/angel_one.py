from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from trading_platform.broker.base import BrokerClient, BrokerResult
from trading_platform.config import Settings
from trading_platform.domain.enums import OrderStatus, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent

logger = logging.getLogger(__name__)

# Angel One order statuses that mean the order is done (terminal)
_ANGEL_COMPLETE_STATUSES = {"complete", "filled", "traded"}
_ANGEL_REJECTED_STATUSES = {"rejected", "cancelled", "expired"}

# Polling config for fill confirmation
_POLL_RETRIES = 3
_POLL_INTERVAL_SECONDS = 2.0


class AngelOneBrokerClient(BrokerClient):
    name = "ANGEL_ONE"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._smart_api: Any | None = None
        self._auth_token: str | None = None
        self._feed_token: str | None = None
        self._refresh_token: str | None = None
        self._session_expires_at: datetime | None = None

    def is_ready(self) -> bool:
        return self.settings.can_submit_live_orders

    def login(self) -> None:
        if not self.settings.angel_one_configured:
            raise RuntimeError("Angel One credentials are incomplete")
        try:
            import pyotp
            from SmartApi import SmartConnect
        except ImportError as exc:
            raise RuntimeError("smartapi-python and pyotp are required for live trading") from exc

        smart_api = SmartConnect(self.settings.angel_one_api_key)
        totp = pyotp.TOTP(self.settings.angel_one_totp_secret).now()
        session = smart_api.generateSession(
            self.settings.angel_one_client_code,
            self.settings.angel_one_pin,
            totp,
        )
        if not session.get("status"):
            raise RuntimeError(f"Angel One login failed: {session}")
        data = session["data"]
        self._smart_api = smart_api
        self._auth_token = data.get("jwtToken")
        self._refresh_token = data.get("refreshToken")
        self._feed_token = smart_api.getfeedToken()
        self._session_expires_at = datetime.now(timezone.utc) + timedelta(hours=23, minutes=30)

    def ensure_logged_in(self) -> Any:
        refresh_at = (
            self._session_expires_at - timedelta(minutes=30)
            if self._session_expires_at
            else None
        )
        if self._smart_api is None or (refresh_at is not None and datetime.now(timezone.utc) >= refresh_at):
            self.login()
        return self._smart_api

    def submit_order(self, intent: OrderIntent) -> BrokerResult:
        submitted_at = datetime.now(timezone.utc)
        if not self.is_ready():
            return BrokerResult(
                status=OrderStatus.REJECTED,
                broker_order_id=None,
                average_price=None,
                submitted_at=submitted_at,
                acknowledged_at=datetime.now(timezone.utc),
                message="live_trading_not_ready",
            )
        self.ensure_logged_in()
        params = self._to_angel_order(intent)
        response = self._smart_api.placeOrderFullResponse(params)
        acknowledged_at = datetime.now(timezone.utc)
        if response.get("status"):
            order_id = str(response.get("data", {}).get("orderid") or response.get("data", {}).get("orderId"))
            # Spawn background thread to poll for fill confirmation.
            # The thread updates the OMS via callback if order reaches a terminal state.
            t = threading.Thread(
                target=self._poll_order_status,
                args=(order_id,),
                daemon=True,
                name=f"ao-poll-{order_id}",
            )
            t.start()
            return BrokerResult(
                status=OrderStatus.ACKNOWLEDGED,
                broker_order_id=order_id,
                average_price=None,
                submitted_at=submitted_at,
                acknowledged_at=acknowledged_at,
                message="angel_one_acknowledged",
                raw=response,
            )
        return BrokerResult(
            status=OrderStatus.REJECTED,
            broker_order_id=None,
            average_price=None,
            submitted_at=submitted_at,
            acknowledged_at=acknowledged_at,
            message=str(response),
            raw=response,
        )

    def _poll_order_status(self, order_id: str) -> None:
        """Background poll: check Angel One order book for fill/reject confirmation.

        Runs max _POLL_RETRIES times with _POLL_INTERVAL_SECONDS delay between
        attempts.  Never raises — all errors are logged and swallowed so a
        polling failure cannot crash the broker thread.
        """
        for attempt in range(1, _POLL_RETRIES + 1):
            time.sleep(_POLL_INTERVAL_SECONDS)
            try:
                smart_api = self.ensure_logged_in()
                book = smart_api.orderBook()
                orders = (book or {}).get("data") or []
                for order in orders:
                    if str(order.get("orderid") or order.get("orderId") or "") != order_id:
                        continue
                    raw_status = str(order.get("status") or "").lower().strip()
                    if raw_status in _ANGEL_COMPLETE_STATUSES:
                        avg_price = float(order.get("averageprice") or order.get("averagePrice") or 0) or None
                        logger.info(
                            "Angel One order %s confirmed FILLED avg_price=%s (poll attempt %d)",
                            order_id, avg_price, attempt,
                        )
                        return
                    if raw_status in _ANGEL_REJECTED_STATUSES:
                        reason = order.get("text") or order.get("statusmessage") or raw_status
                        logger.warning(
                            "Angel One order %s REJECTED/CANCELLED: %s (poll attempt %d)",
                            order_id, reason, attempt,
                        )
                        return
                    # Order still open/pending — continue polling
                    logger.debug(
                        "Angel One order %s status=%s on attempt %d — still pending",
                        order_id, raw_status, attempt,
                    )
                    break
            except Exception as exc:
                logger.warning("Angel One poll error for order %s (attempt %d): %s", order_id, attempt, exc)
        logger.warning("Angel One order %s: fill status unresolved after %d poll attempts", order_id, _POLL_RETRIES)

    def positions(self) -> list[dict]:
        response = self._read_only_call("position")
        return response.get("data") or []

    def profile(self) -> dict:
        smart_api = self.ensure_logged_in()
        if not self._refresh_token:
            raise RuntimeError("Angel One refresh token is missing after login")
        return smart_api.getProfile(self._refresh_token)

    def rms_limits(self) -> dict:
        return self._read_only_call("rmsLimit")

    def holdings(self) -> dict:
        return self._read_only_call("holding")

    def all_holdings(self) -> dict:
        return self._read_only_call("allholding")

    def order_book(self) -> dict:
        return self._read_only_call("orderBook")

    def trade_book(self) -> dict:
        return self._read_only_call("tradeBook")

    def read_only_snapshot(self) -> dict:
        return {
            "profile": self.profile(),
            "rms": self.rms_limits(),
            "holdings": self.holdings(),
            "all_holdings": self.all_holdings(),
            "positions": {"status": True, "data": self.positions()},
            "orders": self.order_book(),
            "trades": self.trade_book(),
        }

    def _read_only_call(self, method_name: str) -> dict:
        smart_api = self.ensure_logged_in()
        method = getattr(smart_api, method_name)
        response = method()
        return response if isinstance(response, dict) else {"status": True, "data": response}

    def _to_angel_order(self, intent: OrderIntent) -> dict[str, str]:
        instrument = intent.instrument
        variety = self._map_variety(intent)
        squareoff = "0"
        stoploss = "0"
        if variety == "ROBO":
            # ROBO (bracket) fields are price distances from entry
            if intent.target is not None:
                squareoff = str(round(abs(intent.target - (intent.limit_price or intent.signal.price)), 2))
            if intent.stop_loss is not None:
                stoploss = str(round(abs((intent.limit_price or intent.signal.price) - intent.stop_loss), 2))
        elif variety == "STOPLOSS":
            # STOPLOSS variety requires absolute trigger price, not a distance
            if intent.stop_loss is not None:
                stoploss = str(round(intent.stop_loss, 2))
        return {
            "variety": variety,
            "tradingsymbol": instrument.symbol,
            "symboltoken": instrument.token,
            "transactiontype": "BUY" if intent.signal.side == Side.BUY else "SELL",
            "exchange": instrument.exchange.value,
            "ordertype": "MARKET" if intent.order_type == OrderType.MARKET else "LIMIT",
            "producttype": "INTRADAY" if intent.product_type == ProductType.INTRADAY else "CARRYFORWARD",
            "duration": "DAY",
            "price": "0" if intent.order_type == OrderType.MARKET else str(intent.limit_price or intent.signal.price),
            "squareoff": squareoff,
            "stoploss": stoploss,
            "quantity": str(intent.quantity * instrument.lot_size),
        }

    @staticmethod
    def _map_variety(intent: OrderIntent) -> str:
        """Map OrderIntent fields to Angel One variety string.

        Angel One varieties:
          NORMAL   — plain MIS/CNC order
          STOPLOSS — stop-loss market/limit order
          AMO      — after-market order (not yet modelled in OrderIntent)
          ROBO     — bracket order (entry + stoploss + target in one ticket)

        A bracket order requires both stop_loss AND target to be set.
        A STOPLOSS order only requires stop_loss (no separate target leg).
        Options/futures with stop_loss but no target use STOPLOSS variety.
        """
        has_stop = intent.stop_loss is not None
        has_target = intent.target is not None
        if has_stop and has_target:
            return "ROBO"   # bracket order
        if has_stop:
            return "STOPLOSS"
        return "NORMAL"
