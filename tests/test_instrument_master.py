from __future__ import annotations

import unittest
from datetime import date

from trading_platform.data.instrument_master import build_default_universe
from trading_platform.domain.enums import OptionType, Segment


class InstrumentMasterTests(unittest.TestCase):
    def test_builds_expiry_aware_index_derivatives(self):
        master = build_default_universe(date(2026, 1, 5))
        future = master.select_future("NIFTY", date(2026, 1, 5))
        option = master.select_option("BANKNIFTY", date(2026, 1, 5), 48520, OptionType.CE)

        self.assertEqual(future.segment, Segment.FUTURES)
        self.assertEqual(future.underlying, "NIFTY")
        self.assertEqual(option.segment, Segment.OPTIONS)
        self.assertEqual(option.option_type, OptionType.CE)
        self.assertGreaterEqual(option.expiry, date(2026, 1, 5))

    def test_includes_equity_cash_symbols(self):
        master = build_default_universe(date(2026, 1, 5))
        reliance = master.get("RELIANCE")

        self.assertEqual(reliance.segment, Segment.CASH)
        self.assertEqual(reliance.lot_size, 1)

    def test_includes_stock_futures_and_options(self):
        master = build_default_universe(date(2026, 1, 5))
        future = master.select_future("RELIANCE", date(2026, 1, 5))
        option = master.select_option("TCS", date(2026, 1, 5), 3500, OptionType.PE)

        self.assertEqual(future.segment, Segment.FUTURES)
        self.assertEqual(future.underlying, "RELIANCE")
        self.assertEqual(option.segment, Segment.OPTIONS)
        self.assertEqual(option.option_type, OptionType.PE)


if __name__ == "__main__":
    unittest.main()
