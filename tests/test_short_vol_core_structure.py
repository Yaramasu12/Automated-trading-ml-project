"""short_vol_core structure selection must stay defined-risk (audit #4).

The core strategy used to pick a naked short STRANGLE in very-high-IV regimes
(iv_rank >= 70) — selling unlimited-risk premium exactly when a vol spike is
most likely. Under require_defined_risk (default) that becomes an iron condor.
"""
from __future__ import annotations

import unittest

from trading_platform.strategies.short_vol_core import (
    ShortVolConfig,
    ShortVolStrategy,
    SignalStructure,
)


class DefinedRiskStructureTests(unittest.TestCase):
    def _strategy(self, require_defined_risk: bool = True) -> ShortVolStrategy:
        return ShortVolStrategy(config=ShortVolConfig(require_defined_risk=require_defined_risk))

    def test_very_high_iv_uses_defined_risk_condor_by_default(self):
        s = self._strategy()
        self.assertEqual(s._select_structure("NIFTY", iv_rank=85.0), SignalStructure.IRON_CONDOR)

    def test_naked_strangle_only_when_explicitly_allowed(self):
        s = self._strategy(require_defined_risk=False)
        self.assertEqual(s._select_structure("NIFTY", iv_rank=85.0), SignalStructure.STRANGLE)

    def test_high_iv_still_condor(self):
        s = self._strategy()
        self.assertEqual(s._select_structure("NIFTY", iv_rank=75.0), SignalStructure.IRON_CONDOR)


if __name__ == "__main__":
    unittest.main()
