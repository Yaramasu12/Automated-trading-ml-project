from __future__ import annotations

import dataclasses
import unittest
from datetime import datetime, timezone

from trading_platform.broker.angel_one import AngelOneBrokerClient
from trading_platform.config import Settings
from trading_platform.domain.enums import (
    AssetClass, Exchange, ExecutionMode, InstrumentType, OrderType, ProductType, Segment, Side,
)
from trading_platform.domain.models import Instrument, OrderIntent, Signal


class FakeSmartApi:
    def getProfile(self, refresh_token):
        return {"status": True, "data": {"clientcode": "V000000", "refresh": refresh_token}}

    def rmsLimit(self):
        return {"status": True, "data": {"availablecash": "100000"}}

    def holding(self):
        return {"status": True, "data": [{"tradingsymbol": "SBIN-EQ", "quantity": 1}]}

    def allholding(self):
        return {"status": True, "data": {"holdings": [{"tradingsymbol": "SBIN-EQ"}]}}

    def position(self):
        return {"status": True, "data": [{"tradingsymbol": "NIFTY30JUN26FUT", "netqty": "0"}]}

    def orderBook(self):
        return {"status": True, "data": []}

    def tradeBook(self):
        return {"status": True, "data": []}

    def cancelOrder(self, order_id, variety):
        return {"status": True, "data": {"orderid": order_id}}


class _RaisingCancelSmartApi(FakeSmartApi):
    def cancelOrder(self, order_id, variety):
        raise RuntimeError("network error")


class _RejectingCancelSmartApi(FakeSmartApi):
    def cancelOrder(self, order_id, variety):
        return {"status": False, "message": "order already complete"}


class TestableAngelOneBrokerClient(AngelOneBrokerClient):
    _smart_api_cls = FakeSmartApi

    def login(self) -> None:
        self._smart_api = self._smart_api_cls()
        self._auth_token = "jwt"
        self._feed_token = "feed"
        self._refresh_token = "refresh"


class AngelOneBrokerTests(unittest.TestCase):
    def test_read_only_snapshot_uses_portfolio_and_account_methods(self):
        broker = TestableAngelOneBrokerClient(_settings())

        snapshot = broker.read_only_snapshot()

        self.assertEqual(snapshot["profile"]["data"]["clientcode"], "V000000")
        self.assertEqual(snapshot["rms"]["data"]["availablecash"], "100000")
        self.assertEqual(snapshot["holdings"]["data"][0]["tradingsymbol"], "SBIN-EQ")
        self.assertEqual(snapshot["positions"]["data"][0]["tradingsymbol"], "NIFTY30JUN26FUT")
        self.assertEqual(snapshot["orders"]["data"], [])
        self.assertEqual(snapshot["trades"]["data"], [])

    def test_live_order_gate_remains_closed_without_confirmation(self):
        broker = TestableAngelOneBrokerClient(_settings())

        self.assertFalse(broker.is_ready())

    def test_algo_id_omitted_from_order_payload_by_default(self):
        broker = TestableAngelOneBrokerClient(_settings())
        params = broker._to_angel_order(_intent())
        self.assertNotIn("algoID", params)

    def test_configured_algo_id_included_in_order_payload(self):
        broker = TestableAngelOneBrokerClient(dataclasses.replace(_settings(), angel_one_algo_id="ALGO123"))
        params = broker._to_angel_order(_intent())
        self.assertEqual(params["algoID"], "ALGO123")

    def test_cancel_order_returns_true_on_broker_success(self):
        broker = TestableAngelOneBrokerClient(_settings())
        self.assertTrue(broker.cancel_order("AO-123"))

    def test_cancel_order_returns_false_on_broker_rejection(self):
        class _Broker(TestableAngelOneBrokerClient):
            _smart_api_cls = _RejectingCancelSmartApi
        broker = _Broker(_settings())
        self.assertFalse(broker.cancel_order("AO-123"))

    def test_cancel_order_returns_false_rather_than_raising_on_error(self):
        """Chase-to-market must always be able to proceed to the MARKET
        resubmit regardless of what the cancel call does -- it must never
        propagate an exception that would abort the chase."""
        class _Broker(TestableAngelOneBrokerClient):
            _smart_api_cls = _RaisingCancelSmartApi
        broker = _Broker(_settings())
        self.assertFalse(broker.cancel_order("AO-123"))


def _settings() -> Settings:
    return Settings(
        execution_mode=ExecutionMode.LIVE,
        broker="ANGEL_ONE",
        live_trading_enabled=True,
        initial_capital=1_000_000,
        max_drawdown=0.10,
        max_daily_loss=0.02,
        max_position_pct=0.05,
        max_margin_utilization=0.60,
        live_order_confirmation="",
        angel_one_api_key="api-key",
        angel_one_api_secret="secret-key",
        angel_one_client_code="client",
        angel_one_pin="1234",
        angel_one_totp_secret="ABCDEF",
        angel_one_instrument_master_url="https://example.invalid/OpenAPIScripMaster.json",
        angel_one_instrument_cache_path="data/processed/test_angel_instruments.json",
        aws_region="ap-south-1",
    )


def _intent() -> OrderIntent:
    instrument = Instrument(
        symbol="RELIANCE", name="RELIANCE", exchange=Exchange.NSE, segment=Segment.CASH,
        asset_class=AssetClass.EQUITY, instrument_type=InstrumentType.EQUITY, token="2885", lot_size=1,
    )
    signal = Signal(
        strategy_name="test", symbol="RELIANCE", side=Side.BUY, confidence=1.0,
        price=2800.0, reason="test", created_at=datetime.now(timezone.utc),
    )
    return OrderIntent(
        signal=signal, instrument=instrument, quantity=1,
        order_type=OrderType.MARKET, product_type=ProductType.INTRADAY,
    )


if __name__ == "__main__":
    unittest.main()
