# Claude Implementation Prompt: High-End Quantum AI Multi-Agent Trading Platform

You are Claude, acting as a senior quant systems architect, Python/FastAPI engineer, ML engineer, and trading-platform safety reviewer.

You are working inside this repository:

```text
Automated-trading-ml-project/
```

Target architecture:

```text
TradingPlatform_QuantumMultiAgent_Architecture.canvas
```

The user wants a high-end automated trading platform using:

- quantum-assisted calculations on market data
- many specialist agents making independent decisions
- local Gemma-compatible LLM agents
- neural networks and deep learning
- multi-agent reinforcement learning in simulation
- strategy tournament and goal governance
- a yearly profit target around 5 crore as an aspirational goal

Do not claim that this system can guarantee 5 crore/year or any profit. Implement the architecture so it optimizes risk-adjusted expected return, blocks low-quality trades, and measures target probability with Monte Carlo and drawdown-aware governance.

## Absolute Safety Rules

1. LLM agents must never submit orders.
2. Quantum services must never submit orders.
3. Neural models must never submit orders.
4. RL policies must never submit live orders until they pass shadow, paper, canary, and manual approval.
5. The existing deterministic trading path remains final:
   - `RiskEngine`
   - `ComplianceGuard`
   - `CapitalProtection`
   - `EventRiskGuard`
   - `ManualApprovalGate`
   - `LiveReadinessAggregator`
   - kill switch
   - `ExecutionScheduler`
   - `OMSEventStore`
   - broker adapters
6. If any new component fails, times out, is unavailable, or disagrees with safety controls, fall back to the current baseline or no trade.
7. New features are disabled by default.
8. Existing tests must not be weakened or removed.
9. No network calls in unit tests.
10. No broker credentials or secrets may enter LLM prompts, traces, vector memory, dashboards, or logs.
11. Phase 2 must add a local Gemma-compatible model gateway. Do not create a custom foundation model in this architecture.

## First Read These Files

Before changing code, inspect:

```text
README.md
ARCHITECTURE.md
trading_platform/api/runtime.py
trading_platform/api/app.py
trading_platform/agent/trading_agent.py
trading_platform/decision/pipeline.py
trading_platform/ai/agents.py
trading_platform/ai/models.py
trading_platform/ai/features.py
trading_platform/ai/feature_store.py
trading_platform/risk/engine.py
trading_platform/risk/compliance.py
trading_platform/risk/capital_protection.py
trading_platform/execution/scheduler.py
trading_platform/domain/models.py
trading_platform/domain/enums.py
tests/
```

Run tests after each phase:

```bash
python -m unittest discover -s tests
npm --prefix hft_frontend test
```

If frontend dependencies are unavailable, document that and continue backend work.

## Phase 1: Data Trace Foundation

Goal: make every AI/quantum decision replayable.

Add:

```text
trading_platform/trace/
  __init__.py
  ids.py
  models.py
  store.py
```

Implement:

- `new_trace_id(prefix: str = "scan") -> str`
- `DecisionTrace`
- `TraceEvent`
- `TraceStore`

Trace store can start as SQLite or JSONL under `data/decision_traces/`.

Trace each scan/order path with:

- trace id
- timestamp
- execution mode
- input symbol universe
- feature snapshot ids
- agent outputs
- neural model versions
- quantum result id
- risk decisions
- order intents
- broker result id

Do not include secrets or full prompts.

Add config flags in `trading_platform/config.py`:

```text
ENABLE_AI_COUNCIL=false
ENABLE_NEURAL_LAB=false
ENABLE_QUANTUM_LAB=false
ENABLE_MARL_LAB=false
ENABLE_GOAL_GOVERNOR=false
LOCAL_LLM_GATEWAY=disabled
LOCAL_LLM_RUNTIME=stub
LOCAL_LLM_PRIMARY_MODEL=gemma4-31b
LOCAL_LLM_FAST_MODEL=gemma4-e4b
LOCAL_LLM_COORDINATOR_MODEL=gemma4-26b-moe
LOCAL_LLM_BASE_URL=http://localhost:11434
LOCAL_LLM_TIMEOUT_SECONDS=15
LOCAL_LLM_MAX_OUTPUT_TOKENS=2048
QUANTUM_BACKEND=classical
QUANTUM_TIMEOUT_SECONDS=3
QUANTUM_MAX_CANDIDATES=12
QUANTUM_RISK_AVERSION=1.0
QUANTUM_CARDINALITY_LIMIT=4
QUANTUM_MIN_BASELINE_IMPROVEMENT=0.0
YEARLY_PROFIT_TARGET=50000000
```

Optional env names:

```text
IBM_QUANTUM_TOKEN
DWAVE_API_TOKEN
LLM_API_KEY
```

Tests:

- `tests/test_trace_store.py`
- trace id uniqueness
- trace persistence
- no secrets persisted

## Phase 2: Local Gemma-Compatible Model Gateway and Agent Council

Goal: build a multi-agent trading council that uses local Gemma-compatible models and produces typed proposals/vetoes, never direct orders.

Add:

```text
trading_platform/agents/
  __init__.py
  schemas.py
  model_gateway.py
  supervisor.py
  specialists.py
  voting.py
```

Implement schemas:

- `AgentInputContext`
- `EvidenceRef`
- `AgentVote`
- `StrategyProposal`
- `RiskCritique`
- `PortfolioProposal`
- `ExecutionAdvice`
- `AgentCouncilDecision`

Implement `LocalModelGateway` in `model_gateway.py`:

- support runtimes:
  - `stub` for tests
  - `ollama` local HTTP API
  - `llama_cpp` local server-compatible API
  - `vllm` OpenAI-compatible local API
- route names:
  - `gemma4-31b` for chief analyst/reasoner
  - `gemma4-26b-moe` for coordinator
  - `gemma4-e4b` or `gemma4-e2b` for fast micro-agents
- structured JSON only
- deterministic stub responses for tests
- timeout and retry controls
- no broker tools
- no secrets in prompt context
- RAG evidence ids included in output

Implement specialist agents:

- `NewsMacroAgent`
- `QuantResearchAgent`
- `TrendMomentumAgent`
- `MeanReversionAgent`
- `BreakoutAgent`
- `GapEventAgent`
- `PairsStatArbAgent`
- `OptionsVolatilityAgent`
- `FuturesCarryAgent`
- `HedgeBuilderAgent`
- `RiskCriticAgent`
- `ExecutionAnalystAgent`
- `PortfolioManagerAgent`

Implement `AgentCouncilSupervisor`:

- runs independent agents in parallel where safe
- aggregates votes
- creates a final typed `AgentCouncilDecision`
- supports debate mode for high-conviction trades:
  - bull case
  - bear case
  - risk critic
  - PM adjudication
- rejects if disagreement/uncertainty is high
- writes trace events
- never creates `OrderIntent`

Tests:

- `tests/test_agent_council.py`
- `tests/test_local_model_gateway.py`
- structured output
- no order creation
- high disagreement causes no-trade
- stale/bad data causes HALT or REDUCE
- stub gateway works without external LLM

## Phase 3: Neural Network and Deep Learning Lab

Goal: create an extensible neural prediction layer that is advisory until validated.

Add:

```text
trading_platform/neural/
  __init__.py
  schemas.py
  tensor_features.py
  forecasting.py
  volatility.py
  graph_model.py
  sentiment.py
  calibration.py
  serving.py
```

Typed outputs:

- `NeuralFeatureBatch`
- `ForecastPrediction`
- `VolatilityPrediction`
- `CorrelationRiskPrediction`
- `TailRiskPrediction`
- `NeuralPredictionBundle`

Implement deterministic baselines first:

- moving-average/linear baseline for forecasts
- current GARCH/volatility wrapper
- simple rolling correlation graph
- sentiment wrapper around existing sentiment analyzer
- conformal/calibration placeholder

Leave optional deep packages behind lazy imports:

- PyTorch
- PyTorch Forecasting / Temporal Fusion Transformer
- transformers time-series models
- ONNX Runtime

Recommended future models:

- Temporal Fusion Transformer
- PatchTST or time-series transformer
- N-BEATS/N-HiTS
- LSTM/GRU
- GNN for cross-asset graph
- autoencoder anomaly detector
- quantile/tail-risk network

Tests:

- `tests/test_neural_lab.py`
- deterministic baseline output
- bundle schema
- calibration/no-trade threshold behavior
- lazy imports do not fail when optional packages are missing

## Phase 4: Quantum Compute and Optimization Lab

Goal: implement quantum-assisted calculation as a portfolio/candidate optimizer with classical fallback.

Add:

```text
trading_platform/quantum/
  __init__.py
  schemas.py
  qubo.py
  service.py
  quantum_kernel.py
  backends/
    __init__.py
    classical.py
    qiskit_backend.py
    dwave_backend.py
```

Schemas:

- `QuantumCandidate`
- `PortfolioOptimizationRequest`
- `QuboProblem`
- `QuantumOptimizationResult`
- `QuantumBackendStatus`
- `QuantumKernelResult`

Build `QuboBuilder`:

Inputs:

- strategy candidates
- expected edge vector
- covariance/risk matrix
- transaction costs
- liquidity score
- margin and lot estimates
- target run-rate pressure
- max basket/cardinality
- concentration limits

Objective:

```text
maximize expected_edge
       - risk_aversion * covariance_risk
       - transaction_cost
       - concentration_penalty
       - liquidity_penalty
```

Constraints:

- budget
- cardinality
- no duplicate underlying exposure
- max single-symbol exposure
- min liquidity
- max drawdown heat
- strategy-family concentration

Implement `ClassicalOptimizerBackend` first:

- exact enumeration for small N
- greedy/local search for larger N
- deterministic
- always available
- validates constraints

Implement optional `QiskitBackend` and `DWaveBackend`:

- lazy imports
- unavailable status when package/token is missing
- no hard dependency
- never fail scan if unavailable

Implement `QuantumOptimizationService`:

- always runs classical baseline
- optionally runs configured quantum backend
- enforces timeout
- compares quantum result to classical baseline
- rejects worse/unstable/invalid/timed-out quantum result
- returns baseline or no-trade safely
- writes trace event
- never creates `OrderIntent`

Implement `QuantumKernelResearchService`:

- shadow-only
- compares quantum-kernel classifier against current regime classifier
- no live influence until promotion

Tests:

- `tests/test_quantum_qubo.py`
- `tests/test_quantum_service.py`
- constraints respected
- fallback works
- missing Qiskit/D-Wave does not fail
- no order creation
- quantum result must beat baseline threshold before accepted

## Phase 5: Multi-Agent RL Simulation Lab

Goal: train and evaluate RL policies only in simulated market environments.

Add:

```text
trading_platform/rl/
  __init__.py
  env.py
  policies.py
  trainer.py
  evaluator.py
```

Implement local environment first:

- observations:
  - features
  - portfolio state
  - open positions
  - volatility
  - liquidity
- actions:
  - no-op
  - propose entry
  - propose exit
  - propose hedge
  - propose size multiplier
- reward:
  - PnL
  - drawdown penalty
  - turnover/slippage penalty
  - tail-risk penalty

Policy roles:

- entry policy
- exit policy
- sizing policy
- hedge policy
- execution timing policy
- adversarial slippage policy

RLlib/FinRL integration must be optional and lazy-loaded. Local tests use deterministic mock policies.

Tests:

- `tests/test_rl_lab.py`
- environment reset/step
- reward penalizes drawdown
- mock policy cannot place live orders
- RL output remains advisory unless promoted

## Phase 6: Ensemble Decision Engine and Goal Governor

Add:

```text
trading_platform/decision_fusion/
  __init__.py
  schemas.py
  fusion.py
  goal_governor.py
```

`DecisionBlackboard` holds:

- existing `DecisionPipeline` candidates
- local Gemma agent council decision
- neural prediction bundle
- quantum optimization result
- RL advisory output
- portfolio state
- goal governor state
- risk precheck

`EnsembleDecisionEngine` combines:

- rule strategies
- neural forecasts
- local Gemma agent votes
- quantum optimizer
- RL advisory output
- current portfolio state

Methods:

- weighted voting by regime
- uncertainty penalty
- no-trade threshold
- champion/challenger policy routing

`GoalGovernor`:

- tracks yearly target, monthly run-rate, drawdown budget, and target probability
- computes target-achievement probability using Monte Carlo results
- can recommend research priority or lower risk
- must never automatically raise hard risk limits

Tests:

- `tests/test_decision_fusion.py`
- high uncertainty -> no trade
- risk critic veto -> no trade
- goal pressure does not raise risk limits
- disabled flags preserve current behavior

## Phase 7: Runtime Integration

Integrate conservatively.

Preferred API in `TradingRuntime`:

```python
def high_end_signal_scan(self, payload: dict) -> dict:
    ...
```

Flow:

```text
TradingAgent._scan_and_execute
  -> current DecisionPipeline.scan
  -> AgentCouncilSupervisor
  -> NeuralPredictionService
  -> QuantumOptimizationService
  -> RL advisory service
  -> DecisionBlackboard
  -> EnsembleDecisionEngine
  -> GoalGovernor
  -> existing RiskEngine and controls
  -> existing _enqueue_intent_with_controls
```

Important:

- when all new flags are disabled, behavior must be identical to current baseline
- if any new service fails, use baseline or no trade
- final order creation must use existing methods
- attach trace metadata to signal/order intent

Tests:

- `tests/test_high_end_integration.py`
- disabled flags preserve current scan
- enabled path returns trace metadata
- risk veto blocks
- optimizer cannot bypass RiskEngine
- agent council cannot enqueue

## Phase 8: API and Frontend

Add backend endpoints:

```text
GET  /ai-council/status
GET  /ai-council/decisions
POST /ai-council/preview
GET  /neural/status
POST /neural/predict-preview
GET  /quantum/status
POST /quantum/optimize-preview
GET  /quantum/results
GET  /goal-governor/status
GET  /policies
POST /policies/promote
POST /policies/rollback
GET  /traces/{trace_id}
```

Mutating endpoints require existing bearer auth.

Frontend additions should follow existing React/Zustand style:

- AI Council view
- Neural Lab view
- Quantum Lab view
- Goal Governor view
- Strategy Tournament view
- Trace Replay view
- Policy Promotion view

Do not redesign the whole app. Add focused operational screens.

## Phase 9: Validation Factory and Promotion

Add:

```text
trading_platform/validation/
  __init__.py
  purged_walk_forward.py
  monte_carlo.py
  tournament.py
  promotion.py
  postmortem.py
```

Promotion statuses:

```text
research
shadow
paper
live_canary
live_approved
disabled
```

Promotion requires:

- tests pass
- out-of-sample improvement
- max drawdown pass
- slippage sensitivity pass
- target probability report
- shadow/paper results
- manual approval

## Documentation

Update/create:

```text
docs/high_end_quantum_ai_architecture.md
docs/local_gemma_gateway.md
docs/ai_council.md
docs/neural_lab.md
docs/quantum_lab.md
docs/goal_governor.md
docs/policy_promotion.md
.env.example
```

Docs must state:

- no profit guarantee
- quantum is optional and experimental
- LLM agents do not trade directly
- local Gemma gateway can run with stubs, Ollama, llama.cpp, or vLLM
- RL is simulation-only until promoted
- live trading requires canary and manual approval
- how to disable every new system

## Acceptance Criteria

The work is complete when:

1. All Python tests pass.
2. Current behavior is unchanged with flags disabled.
3. Local Gemma gateway exists with stub and local-runtime adapters.
4. AI council produces structured advisory decisions only.
5. Neural lab produces typed prediction bundles with deterministic fallback.
6. Quantum lab produces optimizer results with classical fallback.
7. RL lab is simulated/advisory only.
8. Goal governor tracks target without increasing hard risk limits.
9. Final risk/execution path remains deterministic and unchanged.
10. API exposes status/preview endpoints.
11. Docs explain setup, safety, and rollback.

## Reference Architecture Sources

- Gemma 4 official Google post: https://blog.google/innovation-and-ai/technology/developers-tools/gemma-4/
- OpenAI Agents orchestration: https://openai.github.io/openai-agents-python/multi_agent/
- Microsoft Agent Framework orchestration: https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/
- Ray RLlib: https://docs.ray.io/en/master/rllib/index.html
- FinRL-Meta overview: https://finrl.readthedocs.io/en/latest/finrl_meta/overview.html
- PyTorch Forecasting TFT: https://pytorch-forecasting.readthedocs.io/en/v0.5.1/api/pytorch_forecasting.models.temporal_fusion_transformer.TemporalFusionTransformer.html
- IBM Quantum Portfolio Optimizer: https://quantum.cloud.ibm.com/docs/en/guides/global-data-quantum-optimizer
- D-Wave hybrid solvers: https://docs.dwavequantum.com/en/latest/industrial_optimization/leap_hybrid.html
- Qiskit quantum kernels: https://qiskit-community.github.io/qiskit-machine-learning/apidocs/qiskit_machine_learning.kernels.html

## Final Response Required From Claude

When complete, report:

- files changed
- phases implemented
- tests run
- flags added
- optional dependencies intentionally left lazy
- local model runtime setup
- live-trading risks still remaining
- exact command to run the platform
