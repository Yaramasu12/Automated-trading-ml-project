"""P4 anti-churn: the short-vol executor must stop re-attempting an entry that
was refused for a DAY-STABLE reason (compliance / high-impact-event block), but
must KEEP retrying transient or market-dependent refusals (VRP not rich, tail
cap, rate-limit, data errors). See short_vol_executor._is_day_stable_block.
"""
from __future__ import annotations

import unittest

from trading_platform.strategies.short_vol_executor import (
    _is_day_stable_block,
    _block_reason_text,
)


class DayStableBlockClassificationTests(unittest.TestCase):
    def test_high_impact_event_block_is_day_stable(self):
        res = {"submitted": True, "execution": {
            "status": "rejected",
            "rejection_reason": "Trading blocked: high-impact event on 2026-10-08 (offset -1d)",
        }}
        self.assertTrue(_is_day_stable_block(res))

    def test_compliance_rejected_is_day_stable(self):
        res = {"submitted": True, "execution": "compliance_rejected: event risk window"}
        self.assertTrue(_is_day_stable_block(res))

    def test_vrp_not_rich_is_NOT_day_stable(self):
        # VRP changes through the day — must keep retrying.
        res = {"submitted": False, "enter": False, "reason": "vrp 0.9 < min 2.0 (premium not rich)"}
        self.assertFalse(_is_day_stable_block(res))

    def test_tail_cap_is_NOT_day_stable(self):
        # Equity / per-lot risk changes — must keep retrying.
        res = {"submitted": False, "reason": "tail cap: one lot's max loss 14,834 exceeds 1.50% of equity"}
        self.assertFalse(_is_day_stable_block(res))

    def test_rate_limit_error_is_NOT_day_stable(self):
        res = {"submitted": False, "reason": "error: rate_limited: exceeding access rate"}
        self.assertFalse(_is_day_stable_block(res))

    def test_no_spot_price_is_NOT_day_stable(self):
        res = {"submitted": False, "reason": "no spot price"}
        self.assertFalse(_is_day_stable_block(res))

    def test_accepted_submission_is_not_a_block(self):
        res = {"submitted": True, "plan": {"vrp": 5.1}, "execution": {"status": "accepted", "order_id": "x"}}
        self.assertFalse(_is_day_stable_block(res))

    def test_block_reason_text_merges_sources(self):
        res = {"reason": "top", "execution": "exec-detail", "plan": {"reason": "plan-detail"}}
        txt = _block_reason_text(res)
        self.assertIn("top", txt)
        self.assertIn("exec-detail", txt)
        self.assertIn("plan-detail", txt)

    def test_non_dict_is_safe(self):
        self.assertFalse(_is_day_stable_block(None))  # type: ignore[arg-type]
        self.assertFalse(_is_day_stable_block("oops"))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
