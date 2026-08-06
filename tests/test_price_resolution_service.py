"""Unit tests for PriceResolutionService's fallback chain (live -> model ->
sticky -> entry), extracted 2026-08-05 after a FINNIFTY condor's stop-loss
went unnoticed because the exit-management code path and the display path
disagreed about what "current price" meant."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from trading_platform.api.price_service import PriceResolutionService


class PriceResolutionServiceTests(unittest.TestCase):
    def setUp(self):
        self.live_feed = mock.Mock()
        self.live_feed.staleness_tracker = mock.Mock()
        self.live_feed.staleness_tracker.is_stale.return_value = False
        self.exit_manager = mock.Mock()
        self.theoretical_mark = mock.Mock()
        self.svc = PriceResolutionService(
            live_feed=self.live_feed,
            exit_manager=self.exit_manager,
            theoretical_option_mark=self.theoretical_mark,
        )

    def test_prefers_live_tick(self):
        self.live_feed.latest_tick.return_value = SimpleNamespace(last_price=417.95)
        r = self.svc.resolve("FINNIFTY25AUG2628400CE", instrument=object(), entry_price=83.77)
        self.assertEqual(r.price, 417.95)
        self.assertEqual(r.source, "live")
        self.assertFalse(r.is_stale)
        self.theoretical_mark.assert_not_called()
        self.exit_manager.current_mark.assert_not_called()

    def test_live_tick_flagged_stale_via_tracker(self):
        self.live_feed.latest_tick.return_value = SimpleNamespace(last_price=100.0)
        self.live_feed.staleness_tracker.is_stale.return_value = True
        r = self.svc.resolve("NIFTY", entry_price=90.0)
        self.assertEqual(r.source, "live")
        self.assertTrue(r.is_stale)

    def test_falls_back_to_theoretical_model_when_no_tick(self):
        self.live_feed.latest_tick.return_value = None
        self.theoretical_mark.return_value = 44.4
        inst = object()
        r = self.svc.resolve("FINNIFTY25AUG2628400CE", instrument=inst, entry_price=83.77)
        self.assertEqual(r.price, 44.4)
        self.assertEqual(r.source, "model")
        self.assertFalse(r.is_stale)
        self.theoretical_mark.assert_called_once()
        self.exit_manager.current_mark.assert_not_called()

    def test_falls_back_to_sticky_when_no_tick_and_no_instrument(self):
        self.live_feed.latest_tick.return_value = None
        self.exit_manager.current_mark.return_value = 51.0
        r = self.svc.resolve("SOMESYMBOL", entry_price=50.0)
        self.assertEqual(r.price, 51.0)
        self.assertEqual(r.source, "sticky")
        self.assertTrue(r.is_stale)

    def test_falls_back_to_sticky_when_model_returns_none(self):
        self.live_feed.latest_tick.return_value = None
        self.theoretical_mark.return_value = None
        self.exit_manager.current_mark.return_value = 51.0
        r = self.svc.resolve("SYM", instrument=object(), entry_price=50.0)
        self.assertEqual(r.source, "sticky")

    def test_falls_back_to_entry_price_as_last_resort(self):
        self.live_feed.latest_tick.return_value = None
        self.exit_manager.current_mark.return_value = None
        r = self.svc.resolve("SYM", entry_price=20.0)
        self.assertEqual(r.price, 20.0)
        self.assertEqual(r.source, "entry")
        self.assertTrue(r.is_stale)

    def test_returns_none_price_when_nothing_available(self):
        self.live_feed.latest_tick.return_value = None
        self.exit_manager.current_mark.return_value = None
        r = self.svc.resolve("SYM")
        self.assertIsNone(r.price)
        self.assertIsNone(r.source)
        self.assertTrue(r.is_stale)

    def test_live_tick_exception_does_not_raise(self):
        self.live_feed.latest_tick.side_effect = RuntimeError("boom")
        self.exit_manager.current_mark.return_value = None
        r = self.svc.resolve("SYM", entry_price=10.0)
        self.assertEqual(r.source, "entry")

    def test_zero_or_negative_live_price_is_not_used(self):
        self.live_feed.latest_tick.return_value = SimpleNamespace(last_price=0.0)
        self.exit_manager.current_mark.return_value = None
        r = self.svc.resolve("SYM", entry_price=10.0)
        self.assertEqual(r.source, "entry")


if __name__ == "__main__":
    unittest.main()
