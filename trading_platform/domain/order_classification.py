"""Derive whether an order intent actually opens/grows exposure, instead of
trusting the caller-supplied `opens_position` signal metadata.

Found in the 2026-09-13 audit (audit_results/2026-09-13/AUDIT_REPORT.md,
finding #1 — critical): every enforcement point in the money path (the
kill-switch check in execution/scheduler.py, RiskEngine.evaluate, the final
execution gate in api/runtime.py) trusted
`intent.signal.metadata.get("opens_position", True)` as-is. Reproduced live
(audit_results/2026-09-13/probes.py): on a FLAT account with the kill switch
active, a BUY of 1,000 RELIANCE tagged `opens_position=False` and TARGET
priority was queued and filled, growing the position from zero while the
kill switch stayed active. An ordinary (honestly-tagged) entry was correctly
rejected — the bypass is specifically about trusting the flag over reality.

This module is the single source of truth for that derivation. Every call
site must use it instead of reading the metadata flag directly, so the
classification can never drift out of sync between the scheduler, the risk
engine and the final gate the way the untrusted flag did.

Policy: caller metadata can only make an intent MORE conservative, never
less. If the caller claims `opens_position=True` (or omits it — the
existing safe default), that is trusted immediately; a strategy has no
reason to lie in the direction of "this is riskier than it is." Only a
caller's claim of `opens_position=False` is verified — that is the
direction an attacker or a buggy caller would need to lie in to defeat a
protective check, and it is exactly what the audit's reproduction did.
"""
from __future__ import annotations

from trading_platform.domain.models import OrderIntent


def derive_true_opens_position(intent: OrderIntent, held_quantity: int) -> bool:
    """Returns True if `intent`, submitted against a position currently
    holding `held_quantity` (signed: positive long, negative short, zero
    flat), must be treated as opening/growing exposure for every safety
    check — regardless of what `intent.signal.metadata["opens_position"]`
    claims.

    - Caller already claims True (or omits the field): trust it as-is.
    - Flat account (held_quantity == 0): nothing exists to reduce, so ANY
      order is 100% opening. This is the audit's exact reproduction case.
    - Same-direction addition (e.g. already long, intent also nets long):
      this grows exposure, it does not reduce it.
    - Opposite-direction but larger than the held quantity: closes the
      existing position and would REVERSE into a new one in the other
      direction. The reversal remainder is a fresh entry with no
      established risk basis, so the whole order is treated as opening
      here — callers that actually need "close everything, then open a
      new position the other way" must submit that as two separate,
      independently-gated intents, not one intent wearing a closing label.
    - Otherwise (opposite direction, quantity <= held quantity): a
      genuine, fully-covered reduction — trusted as non-opening.
    """
    if bool(intent.signal.metadata.get("opens_position", True)):
        return True

    if held_quantity == 0:
        return True

    signed_quantity = intent.signal.side.sign * intent.quantity
    same_direction = (held_quantity > 0) == (signed_quantity > 0)
    if same_direction:
        return True

    if abs(signed_quantity) > abs(held_quantity):
        return True

    return False
