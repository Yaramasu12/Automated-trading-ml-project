"""Regression 2026-07-29: TATAMOTORS demerged into TMPV/TMCV (Oct 2025) and
LTIM was confirmed absent from Angel One's instrument master entirely — both
were still hardcoded into the live scan universe, silently degrading every
scan cycle to synthetic-data-only decisions for those two symbols. Verifies
the scan universe and its supporting lookup tables were updated consistently,
rather than fixed in one place and left stale in another."""
from __future__ import annotations

import unittest

from trading_platform.agent.trading_agent import EQUITY_UNDERLYINGS
from trading_platform.data.instrument_master import EQUITY_FO_UNDERLYINGS, LIQUID_EQUITIES
from trading_platform.data.market_data import SyntheticDataProvider


class SymbolUniverseConsistencyTests(unittest.TestCase):
    def test_stale_symbols_removed_from_scan_universe(self):
        self.assertNotIn("TATAMOTORS", EQUITY_UNDERLYINGS)
        self.assertNotIn("LTIM", EQUITY_UNDERLYINGS)

    def test_tmpv_present_in_scan_universe(self):
        self.assertIn("TMPV", EQUITY_UNDERLYINGS)

    def test_no_scan_universe_symbol_missing_fo_fallback_config(self):
        # Every scanned equity underlying must have a synthetic-fallback entry
        # (lot_size/strike_step/base) so a real-data gap degrades to a sane
        # synthetic chain instead of a crash or a nonsensical price anchor.
        indices_and_commodities = {
            "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX",
        }
        for sym in EQUITY_UNDERLYINGS:
            if sym in indices_and_commodities:
                continue
            self.assertIn(sym, EQUITY_FO_UNDERLYINGS, f"{sym} missing from EQUITY_FO_UNDERLYINGS")
            self.assertIn(sym, LIQUID_EQUITIES, f"{sym} missing from LIQUID_EQUITIES")
            self.assertIn(sym, SyntheticDataProvider._BASE_PRICES, f"{sym} missing from _BASE_PRICES")

    def test_stale_symbols_removed_from_lookup_tables(self):
        for stale in ("TATAMOTORS", "LTIM"):
            self.assertNotIn(stale, LIQUID_EQUITIES)
            self.assertNotIn(stale, EQUITY_FO_UNDERLYINGS)
            self.assertNotIn(stale, SyntheticDataProvider._BASE_PRICES)


if __name__ == "__main__":
    unittest.main()
