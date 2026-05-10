# High-End Quantum AI Multi-Agent Trading Platform

## Important Disclaimers

- **No profit is guaranteed.** This system optimizes risk-adjusted decision quality.
- The 5 crore/year figure is an aspirational target used only by the Goal Governor to calibrate research intensity.
- Quantum optimization is experimental and optional. It uses a classical fallback by default.
- LLM agents (Gemma-compatible) never submit orders directly. They produce advisory JSON only.
- RL policies are simulation-only until promoted through shadow → paper → canary → manual approval.
- Live trading requires canary mode, manual approval, and explicit risk confirmation.

---

## Architecture Overview

```
TradingAgent._scan_and_execute
  -> current DecisionPipeline.scan          (unchanged baseline)
  -> AgentCouncilSupervisor                 (advisory only, flag-gated)
  -> NeuralPredictionService                (advisory only, flag-gated)
  -> QuantumOptimizationService             (advisory only, flag-gated)
  -> DecisionBlackboard
  -> EnsembleDecisionEngine
  -> GoalGovernor                           (advisory only, cannot raise risk limits)
  -> existing RiskEngine + controls         (FINAL, deterministic, unchanged)
  -> existing _enqueue_intent_with_controls (FINAL order path)
```

## Feature Flags

All new features are **disabled by default**. Set environment variables to enable:

| Flag | Default | Description |
|------|---------|-------------|
| `ENABLE_AI_COUNCIL` | `false` | Local Gemma agent council |
| `ENABLE_NEURAL_LAB` | `false` | Neural prediction service |
| `ENABLE_QUANTUM_LAB` | `false` | Quantum portfolio optimizer |
| `ENABLE_MARL_LAB` | `false` | RL simulation lab |
| `ENABLE_GOAL_GOVERNOR` | `false` | Monte Carlo goal tracker |

## How to Disable Everything

Set all flags to `false` in `.env`. Behavior is identical to the baseline.

## Safety Rules (Non-Negotiable)

1. LLM agents never submit orders.
2. Quantum service never submits orders.
3. Neural models never submit orders.
4. RL policies cannot submit live orders until promoted through all gates.
5. The existing deterministic risk/execution path is always final.
6. Any new component failure falls back to baseline or no-trade.
7. Goal Governor can never automatically raise hard risk limits.
8. No secrets in traces, prompts, logs, or dashboards.

## Phase Summary

| Phase | Module | Description |
|-------|--------|-------------|
| 1 | `trading_platform/trace/` | Decision trace store (JSONL) |
| 2 | `trading_platform/agents/` | Local Gemma agent council |
| 3 | `trading_platform/neural/` | Neural prediction lab |
| 4 | `trading_platform/quantum/` | Quantum optimizer (classical + optional IBM/D-Wave) |
| 5 | `trading_platform/rl/` | RL simulation environment and policies |
| 6 | `trading_platform/decision_fusion/` | Ensemble engine + goal governor |
| 7 | `trading_platform/api/runtime.py` | Integration + `high_end_signal_scan()` |
| 8 | `trading_platform/api/app.py` | New REST endpoints |
| 9 | `trading_platform/validation/` | Walk-forward, Monte Carlo, tournament, promotion |
