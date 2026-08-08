# REDESIGN MASTER ROADMAP

> Evolve the Automated-trading-ml-project into a Profit-Focused, Compliance-Native Multi-Strategy Platform.
> Based on: REDESIGN_PROMPT.md — the brownfield redesign spec.

---

## Design Thesis

The platform's only proven edge is systematic short-vol premium selling. This redesign concentrates capital and engineering there, makes systems A/B feed it as *context* rather than compete with it, fixes operational gaps that actually lose money (data quality, reconciliation, fills), and adds a real RAG/LLM layer with veto-only power. Directional ML returns only when intraday features earn it.

## Ground Truth (Verified)

1. **Three parallel signal systems exist; only one trades** — ShortVol executor (`strategies/short_vol_executor.py`) is the only live path.
2. **Daily-bar TA features have zero OOS edge** — AUC ≈ 0.50 on 2,500 days. Directional daily signals are dead weight.
3. **LLM council is a stub**, quantum is classical fallback, RL/MARL/tournament labs are advisory-only.
4. **Risk framework is real** — RiskEngine, ProfitGuard, EventRiskGuard, ComplianceGuard, EmergencySquareOff all exist.
5. **Blocking gaps identified** — market-data fidelity, broker reconciliation, walk-forward training quality, deployment hardening, live governance.

---

## Build Order & Implementation Status

### ✅ Phase 1: Data Spine + Feed (Weeks 1-2) — COMPLETE

| Component | Status | File |
|---|---|---|
| MarketDataAdapter interface | ✅ Created | `trading_platform/data/market_adapter.py` |
| Tick v2 model (bid/ask/depth/OI) | ✅ Created | `trading_platform/data/tick_v2.py` |
| Depth snapshot model | ✅ Created | `trading_platform/data/depth.py` |
| AngelOneGateway (centralized rate-limit, token bucket) | ✅ Created | `trading_platform/data/angel_gateway.py` |
| AngelOneDataAdapter (sharded WS, 3 sockets) | ✅ Created | `trading_platform/data/angel_one_feed.py` |
| UpstoxDataAdapter (option chain + Greeks) | ✅ Created | `trading_platform/data/upstox_feed.py` |
| TrueDataAdapter (vendor feed, feature-flagged) | ✅ Created | `trading_platform/data/truedata_feed.py` |
| Redis Streams tick bus | ✅ Created | `trading_platform/streaming/tick_bus.py` |
| Tick-to-bar builder (1m → TimescaleDB) | ✅ Created | `trading_platform/data/tick_bar_builder.py` |
| Option chain collector + IV-rank history | ✅ Created | `trading_platform/data/options_chain_collector.py` |
| TimescaleDB phase A (docker-compose) | ✅ Created | `deploy/db/init-timescale.sql` |
| Feast feature store skeleton | ✅ Created | `trading_platform/data/feature_store.py` |
| Polars/DuckDB research layer | ✅ Created | `trading_platform/data/research.py` |
| Staleness guards + watchdog | ✅ Created | `trading_platform/data/staleness.py` |
| TrueData smoke test script | ✅ Created | `scripts/truedata_smoketest.py` |
| TrueData setup docs | ✅ Created | `docs/TRUEDATA_SETUP.md` |

### ✅ Phase 2: Execution Hardening + TCA (Weeks 3-4) — COMPLETE

| Component | Status | File |
|---|---|---|
| Reconciliation loop (30s) | ✅ Created | `trading_platform/execution/reconciliation_service.py` |
| Broker reconciliation engine | ✅ Created | `trading_platform/execution/reconciliation.py` |
| TCA (Transaction Cost Analysis) | ✅ Created | `trading_platform/execution/tca.py` |
| Token automation (TOTP at 08:45) | ✅ Created | `trading_platform/execution/token_automator.py` |
| Smart order router (limit-at-touch, slice) | ✅ Created | `trading_platform/execution/smart_router.py` |

### ✅ Phase 3: Consolidation + Tenancy (Weeks 5-6) — COMPLETE

| Component | Status | File |
|---|---|---|
| Strategy framework (protocol) | ✅ Created | `trading_platform/strategies/strategy_engine.py` |
| Short-vol core strategy | ✅ Created | `trading_platform/strategies/short_vol_core.py` |
| Strategy base class | ✅ Created | `trading_platform/strategies/base.py` |
| BrokerSessionManager (per-tenant) | ✅ Created | `trading_platform/tenancy/broker_session.py` |
| Portfolio ledger (per-tenant) | ✅ Created | `trading_platform/tenancy/portfolio_ledger.py` |
| Runtime decomposition (wiring-only) | ✅ Created | `trading_platform/runtime.py` |

### ✅ Phase 4: Validation Lab + MLOps (Weeks 7-8) — COMPLETE

| Component | Status | File |
|---|---|---|
| CPCV (combinatorial purged CV) | ✅ Created | `trading_platform/validation/cpcv.py` |
| MLflow registry integration | ✅ Created | `trading_platform/validation/mlflow_registry.py` |
| Promotion gates | ✅ Created | `trading_platform/validation/gates.py` |

### ✅ Phase 5: Short-Vol Suite Expansion + Vol Science (Weeks 9-10) — COMPLETE

| Component | Status | File |
|---|---|---|
| Strangle/jade-lizard/calendar variants | ✅ Created | `trading_platform/strategies/short_vol_variants.py` |
| HAR-RV forecaster | ✅ Created | `trading_platform/neural/har_rv.py` |
| SVI surface fitting | ✅ Created | `trading_platform/strategies/svi_surface.py` |
| Portfolio Greeks caps + VaR | ✅ Created | `trading_platform/portfolio/greeks.py` |

### ✅ Phase 6: Intelligence + RAG Pipeline (Weeks 11-13) — COMPLETE

| Component | Status | File |
|---|---|---|
| Adaptive RAG router | ✅ Created | `trading_platform/ai/rag/router.py` |
| GraphRAG layer | ✅ Created | `trading_platform/ai/rag/graph_rag.py` |
| RAG ingestion pipeline | ✅ Created | `trading_platform/ai/rag/ingestion.py` |
| RAG eval harness (RAGAS) | ✅ Created | `trading_platform/ai/rag/eval.py` |
| RAG __init__ exports | ✅ Created | `trading_platform/ai/rag/__init__.py` |
| Agent base class | ✅ Created | `trading_platform/ai/agents/base.py` |
| Regime analyst agent | ✅ Created | `trading_platform/ai/agents/regime.py` |
| Signal veto agent | ✅ Created | `trading_platform/ai/agents/veto.py` |
| Trade journalist agent | ✅ Created | `trading_platform/ai/agents/journalist.py` |
| Copilot chat agent | ✅ Created | `trading_platform/ai/agents/copilot.py` |
| Compliance watcher agent | ✅ Created | `trading_platform/ai/agents/compliance.py` |
| Agent __init__ exports | ✅ Created | `trading_platform/ai/agents/__init__.py` |
| RAG implementation summary | ✅ Created | `docs/PHASE6_RAG_IMPLEMENTATION_SUMMARY.md` |

### ✅ Phase 7: Advanced ML Features (Weeks 14+) — COMPLETE

| Component | Status | File |
|---|---|---|
| Triple-barrier labeling | ✅ Created | `trading_platform/ai/labeling/triple_barrier.py` |
| Intraday feature extractor | ✅ Created | `trading_platform/ai/features/intraday_extractor.py` |
| Foundation RV models (Kronos/Chronos-2/TimesFM) | ✅ Created | `trading_platform/neural/foundation_rv.py` |
| Change-point detection (Bayesian) | ✅ Created | `trading_platform/risk/change_point_detection.py` |
| CPCV validation | ✅ Created | `trading_platform/validation/cpcv.py` |
| Conformal prediction | ✅ Created | `trading_platform/ai/features/conformal_prediction.py` |
| Meta-labeling | ✅ Created | `trading_platform/ai/features/meta_labeling.py` |
| Fractional differentiation | ✅ Created | `trading_platform/ai/features/fractional_diff.py` |
| Drift monitoring (Evidently) | ✅ Created | `trading_platform/monitoring/drift_monitor.py` |

### 🔄 Phase 8: Frontend Redesign (React/Vite — 17 → 8 screens) — IN PROGRESS

| Component | Status | Detail |
|---|---|---|
| Shared dependencies | 🟡 Installed | TanStack Query, Virtual, Lightweight Charts, clsx/tailwind-merge |
| Shared layout + dark-first theme | ⏳ Next | globals.css, layout components |
| Command Center screen | ⏳ Pending | Live P&L, equity curve, positions, Greeks, kill switch |
| Options Desk screen | ⏳ Pending | Chain heatmap, IV-rank, PCR, payoff diagrams |
| Strategy Studio screen | ⏳ Pending | Strategy config, promotion ladder, allocator |
| Backtest Lab screen | ⏳ Pending | Config builder, tearsheets, gate results |
| Risk Console screen | ⏳ Pending | Limits editor, VaR, Greeks, blackouts |
| Intelligence screen | ⏳ Pending | Morning brief, sentiment, Copilot chat |
| Journal & Analytics screen | ⏳ Pending | Calendar P&L, attribution, cost breakdown |
| Ops screen | ⏳ Pending | Broker health, OMS search, TCA, labs |
| WebSocket manager | ⏳ Pending | Shared, rAF batching, virtualization |
| Kill switch mobile responsive | ⏳ Pending | Always reachable, colorblind-safe |

### ⏳ Phase 9: DevOps + Deployment (All local, free) — PARTIAL

| Component | Status |
|---|---|
| Docker Compose profiles | ✅ Updated |
| DB init scripts | ✅ Created |
| Observability (Prometheus/Grafana/Loki) | ✅ Configured |
| Telegram alerts | Configured in existing deploy scripts |
| CI/CD (640 tests + golden-backtest) | Existing, extended |

### ⏳ Phase 10: Documentation + Master Roadmap — IN PROGRESS

| Component | Status |
|---|---|
| Master roadmap (this doc) | 🟡 In progress |
| Phase 1 summary | ✅ Created |
| Phase 6 summary | ✅ Created |
| TRUEDATA_SETUP.md | ✅ Created |

---

## Frontend Screen Specifications

### Screen 1: Command Center (merge Dashboard/Account/Engine)
- **Live P&L**: Realized/unrealized, day/total, cost drag
- **Equity curve**: Interactive with regime badges
- **Open positions**: Per-position Greeks (delta, gamma, vega, theta), margin usage
- **Strategy status grid**: Mini equity curves, running/stopped, health
- **Regime badge**: Current regime (trending/ranging/volatile/calm)
- **Margin gauge**: Utilization vs. limit
- **Reconciliation status**: Last sync, mismatches
- **Kill switch**: Always visible, colorblind-safe red, mobile responsive

### Screen 2: Options Desk (grow ShortVolPanel)
- **Chain heatmap**: OI/IV per strike, color-coded
- **IV-rank history**: Chart with entry/exit markers
- **PCR/max-pain**: Summary stats
- **Payoff diagrams**: Live for each position with Greeks
- **Margin preview**: Before-order simulation
- **One-click enter**: Preview → confirm flow

### Screen 3: Strategy Studio (merge Strategies/Signals/Policies/Tournament)
- **Enable/param-edit**: Schema-driven forms per strategy
- **Promotion-ladder**: Backtest → paper → live status per strategy
- **Live-vs-backtest overlay**: Equity curve comparison
- **Allocator weights**: Current allocation with drag-to-adjust

### Screen 4: Backtest Lab (grow Backtest)
- **Config builder**: Copilot chat or form-based
- **Run queue**: Progress indicators for active backtests
- **Tearsheets**: Quantstats charts with Sharpe/Sortino/MaxDD
- **Parameter-sweep heatmaps**: PBO/DSR results
- **Walk-forward**: Rolling window visualization
- **Promote button**: Disabled until gates pass

### Screen 5: Risk Console (grow Risk)
- **Limits editor**: With two-step confirmation
- **VaR & exposure**: Historical simulation breakdown
- **Portfolio Greeks totals**: Aggregated delta, vega, gamma
- **Risk-event log**: All risk triggers with timestamps
- **Blackout calendar**: EventRiskGuard calendar
- **Compliance/OTR**: Exchange OTR monitoring status

### Screen 6: Intelligence (merge AICouncil/Intelligence/NeuralLab)
- **Morning brief**: Structured summary from RAG
- **Per-ticker sentiment**: Score + source citations
- **Agent decisions**: With reasoning traces
- **Copilot chat**: "Explain this trade" deep links

### Screen 7: Journal & Analytics (new)
- **Calendar P&L heatmap**: Daily/monthly view
- **Attribution**: By strategy/regime/underlying/time-of-day
- **Cost breakdown**: Brokerage/STT/slippage vs gross
- **Journalist postmortems**: AI-generated trade analysis
- **Mined patterns**: Weekly insights from RAG

### Screen 8: Ops (merge Execution/Models/TraceReplay/AILab)
- **Broker/token/feed health**: Status indicators
- **OMS event search**: Filterable, drill-down
- **Reconciliation diffs**: Side-by-side comparison
- **Trace replay**: Replay any trading day
- **Experimental labs**: Behind feature flag

---

## Realtime Architecture (Frontend)

```
┌─────────────────────────────────────────────────────┐
│                   Browser Client                    │
│  ┌─────────────────────────────────────────────────┐│
│  │         Shared WebSocket Manager                ││
│  │  - Validates messages: {type, source, timestamp}││
│  │  - Buffers in ref, flushes on rAF (~60fps)      ││
│  │  - Per-tenant channels (tenant_id scoped)       ││
│  └──────────────────────┬──────────────────────────┘│
│                         │                            │
│  ┌──────────────────────┴──────────────────────────┐│
│  │              Zustand Store                       ││
│  │  - Typed slices: positions, pnl, strategies     ││
│  │  - Never setState per tick                       ││
│  └──────────────────────┬──────────────────────────┘│
│                         │                            │
│  ┌──────────────────────┴──────────────────────────┐│
│  │         Virtualized Lists/Charts                 ││
│  │  - TradingView Lightweight Charts (price)        ││
│  │  - Apache ECharts (heatmaps, payoff)             ││
│  │  - @tanstack/react-virtual (long lists)          ││
│  └─────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────┘
         │  REST + WS  │
         ▼
┌─────────────────────────────────────────────────────┐
│              FastAPI Server                         │
│  ┌─────────────────────────────────────────────────┐│
│  │           Redis Streams Bus                     ││
│  │  tick.*, signal.*, order.*, fill.*, risk.*      ││
│  └─────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────┘
```

## Key Design Decisions

1. **Dark-first, information-dense UI** — Terminal aesthetic, not marketing site
2. **Every number drill-downable** — Click any P&L number to see constituent trades
3. **Kill switch always reachable** — Including mobile-width, colorblind-safe red
4. **P&L colors with colorblind-safe option** — Blue/orange, not just red/green
5. **WebSocket topics mirror event bus** — Same naming, same semantics
6. **Feed-staleness indicator always visible** — Top bar, green/yellow/red

## Acceptance Criteria (Platform-Wide)

- [ ] One signal pipeline; zero orders originate outside Strategy framework → Risk Service → OMS
- [ ] Strategy can go idea → backtest → gates → paper → live entirely through UI
- [ ] Every gate enforced, every order carries Algo-ID + full audit trail
- [ ] Reconciliation mismatch halts entries within 60s, visible in UI
- [ ] Every closed trade has attribution + cost breakdown + journalist postmortem
- [ ] `api/ai_capabilities.py` startup report accurately describes every AI component's real status
- [ ] Kill switch works from a phone
- [ ] Multi-tenant isolation: tenant A cannot see B's data via REST, WS, or Copilot

## Cost Model (India)

All backtests include:
- Brokerage (per-order + percentage)
- STT (options sell-side on premium)
- Exchange transaction charge
- GST (18% on brokerage + charges)
- Stamp duty
- SEBI turnover fee
- Transaction benchmark: "gross vs net after costs" surfaced everywhere

## Multi-Tenant Architecture

- `tenant_id` on every row, enforced by Postgres RLS
- Per-tenant broker sessions (BrokerSessionManager)
- Per-tenant simulated brokers for paper trading
- Bring-your-own-account model (each user's own broker credentials)
- Market data: shared where legal (TrueData vendor), per-user where required

## LLM Architecture (100% Local, Free)

| Tier | Model | Memory | Used For |
|---|---|---|---|
| Deep reasoner | Qwen3-72B / Llama-3.3-70B | ~40-45 GB | Regime Analyst, Trade Journalist |
| Fast worker | Qwen3-14B / Gemma-3-12B | ~8-10 GB | Signal Veto, sentiment, Copilot |
| Embeddings | nomic-embed-text-v1.5 | <2 GB | RAG pipeline |
| Reranker | bge-reranker-v2-m3 | <2 GB | RAG top-k precision |

All inference via LM Studio OpenAI-compatible endpoint + LiteLLM abstraction.

## Deployment (All Local, Free)

- Docker Compose profiles: `research`, `paper`, `live`
- Everything runs on local Mac (Apple Silicon, 128 GB unified memory)
- LM Studio native (not Docker) for Metal GPU acceleration
- SEBI static IP requirement: fixed-IP add-on from ISP
- Observability: Prometheus + Grafana + Loki (all Docker)
- Alerts: Telegram bot (free)

---

*This roadmap is the single source of truth for the redesign. Each phase has its own summary doc.*