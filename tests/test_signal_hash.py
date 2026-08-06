"""Unit tests for compute_signal_hash — OMS audit-trail integrity fingerprint
(REDESIGN_PROMPT.md §6.2, SEBI retail-algo compliance groundwork)."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from trading_platform.domain.enums import Side
from trading_platform.domain.models import Signal, compute_signal_hash


def _signal(**overrides) -> Signal:
    defaults = dict(
        strategy_name="short_vol_condor", symbol="NIFTY24000CE", side=Side.SELL,
        confidence=0.9, price=150.0, reason="VRP rich",
        created_at=datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Signal(**defaults)


class ComputeSignalHashTests(unittest.TestCase):
    def test_deterministic_for_identical_signals(self):
        self.assertEqual(compute_signal_hash(_signal()), compute_signal_hash(_signal()))

    def test_changes_when_price_changes(self):
        self.assertNotEqual(compute_signal_hash(_signal()), compute_signal_hash(_signal(price=151.0)))

    def test_changes_when_side_changes(self):
        self.assertNotEqual(compute_signal_hash(_signal()), compute_signal_hash(_signal(side=Side.BUY)))

    def test_changes_when_strategy_name_changes(self):
        self.assertNotEqual(
            compute_signal_hash(_signal()), compute_signal_hash(_signal(strategy_name="other"))
        )

    def test_changes_when_created_at_changes(self):
        self.assertNotEqual(
            compute_signal_hash(_signal()),
            compute_signal_hash(_signal(created_at=datetime(2026, 8, 6, 10, 0, 1, tzinfo=timezone.utc))),
        )

    def test_ignores_metadata_and_confidence(self):
        # metadata/confidence aren't part of "what the signal said" for audit
        # purposes (metadata is a mutable scratch dict elsewhere in the
        # codebase, e.g. execution_mode gets stamped onto it after creation).
        self.assertEqual(
            compute_signal_hash(_signal(metadata={"trace_id": "abc"}, confidence=0.5)),
            compute_signal_hash(_signal()),
        )

    def test_returns_a_short_stable_length_string(self):
        h = compute_signal_hash(_signal())
        self.assertEqual(len(h), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))


if __name__ == "__main__":
    unittest.main()
