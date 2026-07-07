from __future__ import annotations

import logging
import threading
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

class AngelOneBrokerClient(BrokerClient):
    name = "ANGEL_ONE"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._smart_api: Any | None = None
        self._auth_token: str | None = None
        self._feed_token: str | None = None
        self._refresh_token: str | None = None
        self._session_expires_at: datetime | None = None
        # Serializes login/refresh across the scheduler executor thread and any
        # status-poll callers — concurrent logins clobber _smart_api mid-call.
        self._session_lock = threading.Lock()

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
        with self._session_lock:
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
            # Fill confirmation is owned by ExecutionScheduler._track_order_until_terminal,
            # which polls order_status() and books the fill into the ledger (audit fix C1).
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

    def order_status(self, order_id: str) -> dict | None:
        """Look up one order in the Angel One order book (for scheduler fill tracking).

        Returns a normalized dict:
          state          "complete" | "rejected" | "cancelled" | "open" | raw status
          average_price  float (0.0 if unknown)
          filled_units   int, in exchange units (quantity * lot_size)
          message        broker text for rejections
        or None if the order is not in the book / the call failed.
        """
        try:
            smart_api = self.ensure_logged_in()
            book = smart_api.orderBook()
        except Exception as exc:
            logger.warning("Angel One order_status error for %s: %s", order_id, exc)
            return None
        for order in (book or {}).get("data") or []:
            if str(order.get("orderid") or order.get("orderId") or "") != order_id:
                continue
            raw_status = str(order.get("status") or "").lower().strip()
            if raw_status in _ANGEL_COMPLETE_STATUSES:
                state = "complete"
            elif raw_status in _ANGEL_REJECTED_STATUSES:
                state = "cancelled" if raw_status == "cancelled" else "rejected"
            else:
                state = raw_status or "open"
            return {
                "state": state,
                "average_price": float(order.get("averageprice") or order.get("averagePrice") or 0.0),
                "filled_units": int(float(order.get("filledshares") or order.get("filledShares") or 0)),
                "message": str(order.get("text") or order.get("statusmessage") or ""),
            }
        return None

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
        # System-managed exits (design decision, audit fix H6): every order goes
        # out as plain NORMAL — no ROBO/STOPLOSS broker-side legs. The ExitManager
        # is the single owner of stop-loss/target execution; broker-side legs
        # would double-exit (broker fires AND ExitManager fires → unintended
        # reverse position). intent.stop_loss/target stay on the intent for the
        # ExitPlan built after the fill.
        squareoff = "0"
        stoploss = "0"
        return {
            "variety": "NORMAL",
            "tradingsymbol": instrument.symbol,
            "symboltoken": instrument.token,
            "transactiontype": "BUY" if intent.signal.side == Side.BUY else "SELL",
            "exchange": instrument.exchange.value,
            "ordertype": "MARKET" if intent.order_type == OrderType.MARKET else "LIMIT",
            # Intraday-only (design decision): nothing in the system creates
            # CARRYFORWARD intents today. If one ever appears, surface it loudly —
            # closing a CARRYFORWARD position with an INTRADAY exit does not net
            # out at the broker.
            "producttype": "INTRADAY" if intent.product_type == ProductType.INTRADAY else "CARRYFORWARD",
            "duration": "DAY",
            "price": "0" if intent.order_type == OrderType.MARKET else str(intent.limit_price or intent.signal.price),
            "squareoff": squareoff,
            "stoploss": stoploss,
            "quantity": str(intent.quantity * instrument.lot_size),
        }

