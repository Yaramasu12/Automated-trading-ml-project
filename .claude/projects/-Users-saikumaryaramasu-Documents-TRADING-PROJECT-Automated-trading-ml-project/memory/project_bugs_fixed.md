---
name: Critical bugs fixed — May 2026 architecture review
description: Six bugs found and fixed during the deep architecture review of the automated trading platform
type: project
---

All six bugs were found and fixed in the same session (2026-05-04).

**Bug 1 — NameError on first fill**
`runtime.py` called `logger.warning()` inside `_on_fill()` without importing `logging`.
Fixed: added `import logging` + `logger = logging.getLogger(__name__)` at the top of `runtime.py`.

**Bug 2 — EOD squareoff crash**
`_eod_routine()` in `trading_agent.py` called `square_off_manager.square_off_all()` (method does not exist) wrapped in `asyncio.to_thread()` (which expects a sync callable — `square_off()` is async).
Fixed: replaced with `await self._runtime.square_off_manager.square_off()`.

**Bug 3 — Premarket routine skipped on day 2+**
`AgentState.premarket_done` was reset to `False` only in `start()`, never when a new trading day began. Added `premarket_date: date | None` field; `_tick()` now resets `premarket_done` whenever `today != premarket_date`.

**Bug 4 — RegimeClassifier trained but never consulted**
`DecisionPipeline` always used the rule-based `MarketRegimeAgent` and never queried `RegimeClassifier` (even after it had been trained on forward-return labels). Fixed: pipeline now accepts `regime_classifier` param and uses ML prediction when `is_trained`.

**Bug 5 — MetaModel feedback loop broken**
MetaModel accumulated per-fill P&L scores but `DecisionPipeline.scan()` never used them — strategy selection was always the fixed `StrategySelectionAgent` mapping. Fixed: pipeline accepts `meta_model` param and ranks strategies through `meta_model.rank(regime, base_strategies)` each scan.

**Bug 6 — LIVE mode blocked all autonomous orders**
`_requires_manual_approval()` returned `True` for every order with `execution_mode == LIVE`, making the autonomous agent unable to trade without a human clicking approve. Fixed: agent-generated orders carry `metadata["agent_auto"] = True`; the approval gate skips the blanket LIVE check for these while still routing large-notional and event-risk orders through manual approval.

**Why:** All six bugs were silent — the system appeared to start but either crashed on first real fill, skipped the premarket feed restart permanently after day 1, or trained ML models that were never actually used.

**How to apply:** If extending the pipeline or adding new execution modes, keep the `agent_auto` flag pattern for distinguishing robot-sourced from human-sourced orders.
