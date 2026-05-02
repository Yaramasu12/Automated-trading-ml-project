from __future__ import annotations

import unittest

from trading_platform.broker.angel_one import AngelOneBrokerClient
from trading_platform.config import Settings
from trading_platform.domain.enums import ExecutionMode


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


class TestableAngelOneBrokerClient(AngelOneBrokerClient):
    def login(self) -> None:
        self._smart_api = FakeSmartApi()
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


if __name__ == "__main__":
    unittest.main()
