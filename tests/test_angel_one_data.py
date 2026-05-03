from __future__ import annotations

import unittest
from datetime import datetime

from trading_platform.config import Settings
from trading_platform.data.angel_one_history import AngelOneHistoricalDataProvider
from trading_platform.data.angel_one_instruments import AngelOneInstrumentMasterProvider
from trading_platform.domain.enums import AssetClass, Exchange, ExecutionMode, InstrumentType, OptionType, Segment


class FakeSmartApi:
    def __init__(self):
        self.last_params = None

    def getCandleData(self, params):
        self.last_params = params
        return {
            "status": True,
            "data": [
                ["2026-01-01T09:15:00+05:30", 100, 110, 95, 105, 1000],
                ["2026-01-02T09:15:00+05:30", 105, 112, 101, 108, 1200],
            ],
        }


class AngelOneDataTests(unittest.TestCase):
    def test_parses_openapi_instrument_rows(self):
        provider = AngelOneInstrumentMasterProvider(_settings())
        master = provider.parse_rows(
            [
                {
                    "token": "3045",
                    "symbol": "SBIN-EQ",
                    "name": "SBIN",
                    "expiry": "",
                    "strike": "0.000000",
                    "lotsize": "1",
                    "instrumenttype": "",
                    "exch_seg": "NSE",
                    "tick_size": "5.000000",
                },
                {
                    "token": "35014",
                    "symbol": "BANKNIFTY29JAN26FUT",
                    "name": "BANKNIFTY",
                    "expiry": "29JAN2026",
                    "strike": "0.000000",
                    "lotsize": "15",
                    "instrumenttype": "FUTIDX",
                    "exch_seg": "NFO",
                    "tick_size": "5.000000",
                },
                {
                    "token": "99999",
                    "symbol": "NIFTY29JAN2622500CE",
                    "name": "NIFTY",
                    "expiry": "29JAN2026",
                    "strike": "2250000.000000",
                    "lotsize": "50",
                    "instrumenttype": "OPTIDX",
                    "exch_seg": "NFO",
                    "tick_size": "5.000000",
                },
            ]
        )

        equity = master.get("SBIN-EQ")
        future = master.get("BANKNIFTY29JAN26FUT")
        option = master.get("NIFTY29JAN2622500CE")

        self.assertEqual(equity.exchange, Exchange.NSE)
        self.assertEqual(equity.segment, Segment.CASH)
        self.assertEqual(equity.tick_size, 0.05)
        self.assertEqual(future.instrument_type, InstrumentType.FUTURE)
        self.assertEqual(future.asset_class, AssetClass.INDEX)
        self.assertEqual(future.expiry.isoformat(), "2026-01-29")
        self.assertEqual(option.option_type, OptionType.CE)
        self.assertEqual(option.strike, 22500)

    def test_historical_provider_maps_candle_response(self):
        provider = AngelOneInstrumentMasterProvider(_settings())
        master = provider.parse_rows(
            [
                {
                    "token": "3045",
                    "symbol": "SBIN-EQ",
                    "name": "SBIN",
                    "expiry": "",
                    "strike": "0",
                    "lotsize": "1",
                    "instrumenttype": "",
                    "exch_seg": "NSE",
                    "tick_size": "5",
                }
            ]
        )
        fake_api = FakeSmartApi()
        history = AngelOneHistoricalDataProvider(_settings(), smart_api=fake_api)
        bars = history.get_candles(
            master.get("SBIN-EQ"),
            datetime(2026, 1, 1, 9, 15),
            datetime(2026, 1, 2, 15, 30),
            "ONE_DAY",
        )

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].symbol, "SBIN-EQ")
        self.assertEqual(bars[1].close, 108)
        self.assertEqual(fake_api.last_params["symboltoken"], "3045")
        self.assertEqual(fake_api.last_params["exchange"], "NSE")


def _settings() -> Settings:
    return Settings(
        execution_mode=ExecutionMode.BACKTEST,
        broker="ANGEL_ONE",
        live_trading_enabled=False,
        initial_capital=1_000_000,
        max_drawdown=0.10,
        max_daily_loss=0.02,
        max_position_pct=0.05,
        max_margin_utilization=0.60,
        live_order_confirmation="",
        angel_one_api_key="",
        angel_one_api_secret="",
        angel_one_client_code="",
        angel_one_pin="",
        angel_one_totp_secret="",
        angel_one_instrument_master_url="https://example.invalid/OpenAPIScripMaster.json",
        angel_one_instrument_cache_path="data/processed/test_angel_instruments.json",
        aws_region="ap-south-1",
    )


if __name__ == "__main__":
    unittest.main()
