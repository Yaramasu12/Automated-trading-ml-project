from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from trading_platform.broker.base import BrokerClient, BrokerResult
from trading_platform.config import Settings
from trading_platform.domain.enums import OrderStatus, OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent


class AngelOneBrokerClient(BrokerClient):
    name = "ANGEL_ONE"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._smart_api: Any | None = None
        self._auth_token: str | None = None
        self._feed_token: str | None = None

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
        self._feed_token = smart_api.getfeedToken()

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
        if self._smart_api is None:
            self.login()
        params = self._to_angel_order(intent)
        response = self._smart_api.placeOrderFullResponse(params)
        acknowledged_at = datetime.now(timezone.utc)
        if response.get("status"):
            order_id = str(response.get("data", {}).get("orderid") or response.get("data", {}).get("orderId"))
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

    def positions(self) -> list[dict]:
        if self._smart_api is None:
            return []
        response = self._smart_api.position()
        return response.get("data") or []

    def _to_angel_order(self, intent: OrderIntent) -> dict[str, str]:
        instrument = intent.instrument
        return {
            "variety": "NORMAL",
            "tradingsymbol": instrument.symbol,
            "symboltoken": instrument.token,
            "transactiontype": "BUY" if intent.signal.side == Side.BUY else "SELL",
            "exchange": instrument.exchange.value,
            "ordertype": "MARKET" if intent.order_type == OrderType.MARKET else "LIMIT",
            "producttype": "INTRADAY" if intent.product_type == ProductType.INTRADAY else "CARRYFORWARD",
            "duration": "DAY",
            "price": "0" if intent.order_type == OrderType.MARKET else str(intent.limit_price or intent.signal.price),
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(intent.quantity * instrument.lot_size),
        }
