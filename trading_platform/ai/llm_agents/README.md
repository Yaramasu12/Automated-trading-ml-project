# RETIRED — not integrated, do not build on this

**Status as of 2026-08-29: retired.** This package was built per REDESIGN_PROMPT
§8 as a complete, independent AI-council implementation — RegimeAnalyst,
SignalVetoAgent, TradeJournalist, CopilotAgent, ComplianceWatcherAgent, plus
its own guardrail (`base.py`'s `AgentDirectionGuard`) and Pydantic-validated
output schemas (`veto.py`'s `VetoAction`, with a real `size_multiplier` field
clamped `ge=0.0, le=1.0`).

**It has zero callers anywhere in the codebase.** Confirmed 2026-08-28: not
one of these classes is imported outside this package's own `__init__.py`.
The AI council that actually runs is `trading_platform/agents/`
(`specialists.py`, `supervisor.py`, `voting.py`) — a separate, simpler
implementation with its own closed-enum safety contract
(`agents/voting.py`'s `aggregate_to_action` returns only
`PROCEED|REDUCE|HALT|NO_TRADE`, with no numeric upsize field for a
prompt-injection attack to hijack — see `trading_platform/ai/guardrails.py`
for why `AgentDirectionGuard` turned out to be defending a field the live
path doesn't have).

## Why retired instead of migrated-to

This package is arguably more disciplined in places (per-agent Pydantic
schemas with field-level validators, an explicit guardrail class) than the
framework actually running. The recommendation, made explicitly and
confirmed by the user 2026-08-29, was still to retire rather than migrate:
the live `agents/` council works, is tested, and has a verified
advisory-only contract — replacing it with a second, never-integration-tested
framework this close to a live-trading readiness push is real risk for
unproven benefit.

## If a future session wants to revive pieces of this

Don't wholesale-integrate the package. The individual specialist concepts
(regime disagreement detection, structured trade postmortems, a compliance-
circular watcher) are reasonable ideas — port whichever one is actually
wanted as a **new specialist inside `agents/specialists.py`**, using that
framework's existing `AgentVote`/`_safe_vote()` contract, not this package's
`VetoAction`/Pydantic contract. Two parallel agent frameworks is exactly the
confusion this retirement exists to end — reintroducing pieces of the
retired one without folding them into the live contract would recreate it.
