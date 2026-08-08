# Redesign Prompt: Evolve the Automated-trading-ml-project into a Profit-Focused, Compliance-Native Multi-Strategy Platform

> Paste this whole document into Claude Code, run from the repo root of `Automated-trading-ml-project`.
> This is a **brownfield redesign** — the repo already has a strong spine (FastAPI runtime, Angel One adapter, RiskEngine, backtester with realistic slippage, React/Vite frontend, 640 tests). Do NOT rewrite from scratch. Refactor, consolidate, and extend per the Keep/Merge/Replace map below.
> Honor the repo's existing "honesty discipline" (CLAUDE.md): models must earn deployment, advisory ≠ safety, no silent exception swallowing. Every claim of edge must survive walk-forward validation before it touches order flow.
>
> **Infrastructure constraint (hard rule): 100% free, open-source, self-hosted.** Everything runs locally on the developer's Mac (Apple Silicon, 128 GB unified memory). No cloud APIs, no paid services, no AWS/GCP/Azure. All LLM inference goes through **LM Studio's local OpenAI-compatible server** (the repo's `LOCAL_LLM_*` env vars already point there). Postgres, TimescaleDB, Redis (open-source, runs in local Docker — not AWS ElastiCache), Qdrant, Prometheus, Grafana, and Loki are all free OSS run via Docker Compose. Remove the vestigial `AWS_REGION` from `.env.example`.

---

## 0. Ground truth about the current system (verified against the working tree)

1. **Three parallel signal systems exist; only one trades.**
   - A — `decision/pipeline.py` legacy regime→strategy engine: manual API only.
   - B — `orchestrator/master_orchestrator.py` 9-node graph (market_intelligence → specialist_crew → neural_forecast → quantum_portfolio → risk_critic → profit_guard → consensus_fusion → goal_governor → execution_plan): runs every cycle, then `AGENT_DIRECTIONAL_ENABLED=false` discards everything.
   - C — `strategies/short_vol_executor.py` iron-condor/put-spread engine: **the only live, enqueue-capable strategy** (`SHORTVOL_AUTO_ENABLED=true`).
2. **The one validated empirical finding: daily-bar TA features have zero OOS edge** (AUC ≈ 0.50 on 2,500 days). The repo correctly refuses to ship a return forecaster. Directional daily signals are dead weight until better features exist.
3. LLM council is a stub (`LOCAL_LLM_RUNTIME=stub`), quantum is a classical fallback, RL/MARL/tournament labs are advisory-only.
4. Storage is SQLite + JSONL feature store. Broker is **Angel One SmartAPI** (aggressive rate limits, daily TOTP token). Frontend has 17 views with a Zustand store and WS dashboard.
5. Risk framework is real and good: RiskEngine (drawdown, daily loss, margin, kill switch, naked-option ban, gamma/expiry cutoffs), ProfitGuard, EventRiskGuard, ComplianceGuard (200 orders/day), EmergencySquareOff, manual-approval live mode.
6. `docs/implementation_review.md` already names the blocking gaps: market-data fidelity, broker reconciliation, walk-forward training quality, deployment hardening, live governance.
7. `YEARLY_PROFIT_TARGET=50000000` on ₹10L capital is a 5000%/yr target — replace with defensible goal governance (see §9).

**Design thesis:** the platform's only proven edge is systematic short-vol premium selling. The redesign concentrates capital and engineering there, makes systems A/B feed it as *context* rather than compete with it, fixes the operational gaps that actually lose money (data quality, reconciliation, fills), and adds a real RAG/LLM layer with veto-only power. Directional ML returns only when intraday features earn it.

---

## 1. Keep / Merge / Replace map

| Current component | Verdict | Action |
|---|---|---|
| `strategies/short_vol*.py` (C) | **KEEP — promote to core** | Generalize into the Strategy framework (§4); add strangle, jade lizard, calendar variants; IV-rank entry gating; delta-band management |
| `risk/engine.py`, ProfitGuard, EventRiskGuard, ComplianceGuard, kill switch | **KEEP** | Extract into a standalone Risk Service boundary; add portfolio-Greeks caps and VaR (§6) |
| `backtesting/` (next-bar-open fills, √-impact slippage) | **KEEP** | Add walk-forward + deflated-Sharpe/PBO gates and India cost model incl. STT (§5) |
| `orchestrator/master_orchestrator.py` (B) | **MERGE** | Retire as an order path. Keep 3 nodes as *context providers* for C and the allocator: market_intelligence, risk_critic, consensus scoring. Delete quantum_portfolio (classical fallback adds latency, no alpha); goal_governor moves to allocator |
| `decision/pipeline.py` (A) | **RETIRE** | Port its regime classification + feature computation into the shared FeatureService; delete the rest |
| `agents/` council (specialists, supervisor, voting, model_gateway, vector_memory) | **REPLACE** | Rebuild on LangGraph with real models + structured outputs, veto-only (§8). Keep `vector_memory` concept, back it with a real vector store |
| `neural/` (vol_forecaster GOOD; return_forecaster empty by design) | **KEEP vol / PAUSE return** | VolatilityForecaster feeds short-vol sizing. Return forecasting returns only via intraday meta-labeling (§4.3) |
| `rl/`, MARL lab, quantum lab, tournament | **FREEZE** | Move behind `EXPERIMENTAL=true`, exclude from money path and from UI default nav. Do not delete (research value), do not run in prod cycles |
| SQLite + JSONL feature store | **REPLACE (phased)** | Postgres 16 + TimescaleDB for ticks/bars/chain/features; SQLite stays only as embedded fallback for pure-local backtests (§7) |
| Angel One adapter | **KEEP + harden** | Reconciliation loop, token automation, rate-limit token bucket (already partially in ShortVolExecutor — centralize it) (§6.3) |
| React 17-view frontend | **CONSOLIDATE** | 17 views → 8 screens, information-dense redesign (§10) |
| `news/` (feed, calendar, intelligence) | **KEEP — feed the RAG engine** | Becomes the ingestion tier of §8's RAG pipeline; calendar drives EventRiskGuard blackouts |
| `TradingRuntime` god-object (~3k lines) | **CONTINUE decomposition** | Finish extracting `api/*_service.py`; target: runtime = wiring only, <500 lines |

---

## 2. Target architecture & technology stack (2026-current)

```
┌────────────────────────── React UI (8 screens, dark-first) ──────────────────────────┐
└──────────────────────────────┬───────────────────────────────────────────────────────┘
                               │ REST + WS (FastAPI, unchanged contract style)
┌──────────────────────────────┴───────────────────────────────────────────────────────┐
│                         FastAPI app (thin; services injected)                        │
├──────────────┬──────────────┬──────────────┬──────────────┬──────────────────────────┤
│ MarketData   │ Strategy     │ Risk         │ Execution    │ Intelligence             │
│ Service      │ Engine       │ Service      │ Service(OMS) │ (RAG + LLM agents)       │
│ ticks→bars,  │ short-vol    │ pre-trade    │ Angel One    │ ingest→embed→retrieve,   │
│ chain snaps, │ core + swing │ gates, VaR,  │ adapter,     │ regime & veto agents,    │
│ Greeks, VIX  │ + allocator  │ Greeks caps, │ reconcile,   │ trade journalist,        │
│              │              │ kill switch  │ Algo-ID tag  │ copilot                  │
├──────────────┴──────────────┴──────────────┴──────────────┴──────────────────────────┤
│ Event bus: Redis Streams (tick.*, signal.*, order.*, fill.*, risk.*)                 │
├──────────────────────────────────────────────────────────────────────────────────────┤
│ Postgres16+TimescaleDB (ticks/bars/chain/features) │ Postgres OLTP (orders/trades/   │
│ audit) │ Redis (hot state, rate-limit buckets) │ Qdrant (RAG vectors) │ object store │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12+ (`asyncio` + `uvloop`), keep monorepo | already the stack; services as processes under Docker Compose |
| API | FastAPI + Pydantic v2 | keep; contract tests already exist |
| Event bus | Redis Streams (upgrade path: Redpanda) | consumer groups, at-least-once, near-zero ops |
| Market data source | **Broker data APIs first** — Angel One (integrated) + **Upstox** (free, option chain w/ Greeks, expired F&O history). Authorized vendor (TrueData) optional/later for redistribution. All behind `MarketDataAdapter` | free + individual-KYC-only to start; no company KYC; vendor only when broker feed limits or multi-user redistribution force it |
| Time-series DB | TimescaleDB (Postgres extension) | ticks/bars/chain; continuous aggregates 1m→1D |
| OLTP DB | PostgreSQL 16 | orders, trades, audit, strategies |
| Cache/state | Redis 7 | LTP cache, positions, rate-limit tokens, locks |
| Vector DB | Qdrant (pgvector acceptable to start) | RAG hybrid dense+sparse search |
| Dataframe/compute | **Polars** (primary) + **DuckDB** (SQL-on-files), Arrow zero-copy interop; pandas only at library edges | Polars ~50× faster than pandas on large frames; DuckDB queries Parquet/Timescale directly. On 128 GB this keeps years of tick/chain data in-memory. Retire pandas from hot paths |
| Feature store | **Feast** (OSS) with Redis online + Parquet/DuckDB offline; point-in-time-correct joins | kills train/serve skew — the #1 cause of silent model rot. Streaming feature views from the Redis tick bus |
| ML | LightGBM, scikit-learn; `tsfresh`/`tsfel` features; existing GARCH vol code; foundation models (§4.4a) | tabular ML wins on OHLCV/microstructure; avoid HistGradientBoosting (known deadlock landmine) |
| Experiment tracking | **MLflow** (OSS, local) — model registry, params, metrics, artifacts; **Optuna** for tuning | every promoted model is versioned & reproducible; registry is the source of truth for what's live |
| Backtesting | existing event-driven engine + **vectorbt** for wide screening + **nautilus_trader** (OSS, Rust core) evaluated as the event-driven upgrade path | two-tier: screen wide, validate deep; nautilus gives the same engine for backtest→live |
| LLM | **LM Studio local server only** (OpenAI-compatible API at `LOCAL_LLM_BASE_URL`); two-tier local models (see §8.1) via LiteLLM abstraction so models swap by config | replaces stub gateway; zero cost, zero data leaves the machine |
| Agent orchestration | LangGraph (open-source) | stateful graphs, checkpoints, structured outputs |
| Embeddings | local via LM Studio: `nomic-embed-text-v1.5` (already configured) or `bge-m3` | free, fast on Apple Silicon |
| Reranker | local cross-encoder `bge-reranker-v2-m3` (runs via sentence-transformers, MPS-accelerated) | free, precision on top-k |
| UI | keep React + TypeScript + Vite + Zustand; add Tailwind + shadcn/ui, TradingView Lightweight Charts, TanStack Query | consolidate 17 views → 8 screens |
| Observability | Prometheus + Grafana + Loki (all free OSS, local Docker); Telegram alerts (free bot API) | tick lag, order latency, reconciliation, LLM latency |
| Deploy | Docker Compose profiles `research|paper|live`, everything on the local Mac; for LIVE, a **static IP** is a SEBI requirement — get one from your ISP (fixed-IP add-on) so the whole stack stays home-hosted | compliance-native, zero cloud spend |

---

## 3. Market Data Service (fixes blocking gap #1)

- Harden `data/live_feed.py`: SmartWebSocketV2 auto-reconnect with exponential backoff, gap backfill from candle API on reconnect, per-symbol staleness monitor (stale >10s in market hours → `risk.data_degraded` event → strategies stop opening, exits still allowed).
- In-process 1m bar builder from ticks → TimescaleDB hypertable; continuous aggregates for 5m/15m/1h/1D. Tick retention 30 days, bars 5 years.
- Option-chain snapshots every 30–60s for NIFTY/BANKNIFTY/FINNIFTY/SENSEX + top stocks: OI, ΔOI, IV per strike (reuse `derivatives/` IV engine), ATM IV, IV rank/percentile history, PCR, max pain. **This is the fuel for the short-vol edge — build it first.**
- India VIX, FII/DII flows (daily), expiry calendar (exists in `derivatives/`).
- Centralize the Angel One rate-limit discipline that `ShortVolExecutor` implements privately (serialized fetches, negative-TTL cache) into one shared `AngelOneGateway` with a token bucket — every fetch in the codebase goes through it.
- Market replay: record ticks to Timescale; a replay driver feeds the SAME pipeline for deterministic re-runs of any trading day.

### 3.1 Feature platform (latest: streaming feature store + columnar compute)
- **Feast feature store** replaces the ad-hoc JSONL feature store. Define feature views once; the same definitions serve backtest (offline, point-in-time-correct from Parquet/DuckDB) and live (online, from Redis). This structurally eliminates train/serve skew, which is the most common reason a model that backtested well quietly loses money live.
- **Polars + DuckDB** for all feature engineering and research queries — Arrow zero-copy means no serialization tax moving between them, Timescale, and the ML layer. With 128 GB you can hold multi-year 1m bars and full option-chain history resident for instant research.
- **Microstructure & option-flow feature set** (the real 2026 edge for intraday): order-book imbalance and depth slope (from Angel One depth), trade-sign runs, realized-vol-of-vol, ΔOI velocity per strike, IV-skew slope changes, PCR momentum, cross-underlying lead-lag (NIFTY↔BANKNIFTY). These features — not daily TA — are where directional edge, if any, lives.
- All features carry lineage metadata (source, transform, version) so the Journal can attribute P&L to feature families and drift monitoring (§5) can watch each one.

### 3.0 Data source strategy: broker data APIs first (free, no vendor KYC), vendor optional later

**Decision (updated):** the immediate data plane is **broker data APIs** — free, and they need only the *individual* KYC you already did to open a broker account (no company/business KYC that authorized vendors like TrueData demand). Everything sits behind a `MarketDataAdapter` so sources are swappable.

- **Primary now: Angel One** (already integrated, free) — start here.
- **Add for richer options data: Upstox** — free, full option chain **with Greeks**, and crucially serves **expired F&O historical data** (needed for backtesting + IV-rank history). Individual account only.
- **Avoid for now: Dhan** data API (gated behind 25 trades/30 days — awkward while paper trading) and **Fyers** (active option symbols only, no expired history → weak for backtests).
- **Vendor (TrueData / Global Datafeeds): OPTIONAL, later.** Only needed if (a) broker feed quality/throttling becomes the bottleneck for heavy chains, or (b) you go multi-user and need *licensed redistribution*. TrueData requires PAN/business KYC and ~₹2,599/mo — not required to start. The `TrueDataAdapter` (below) stays in the design, feature-flagged off (`TRUEDATA_ENABLED=false`), ready to switch on if you ever license it. For multi-user, "bring-your-own-account" (each user's own broker credentials) avoids both the vendor and its KYC entirely.

The `TrueDataAdapter` spec below remains valid for whenever/if you enable a vendor; build the **Angel One + Upstox adapters first** to the same `MarketDataAdapter` contract.

- **Credentials** (already in `.env`, gitignored — placeholders in `.env.example`): `TRUEDATA_LOGIN`, `TRUEDATA_PASSWORD`, `TRUEDATA_ENABLED`, `TRUEDATA_REALTIME_PORT` (default 8082), `TRUEDATA_HISTORICAL`. Parse these into the `Settings` dataclass in `config.py` alongside the existing Angel One keys. Never hardcode; never log the password.
- **Library**: `truedata-ws` (`pip install truedata-ws`). `TD(login, password, historical_api=True)` gives both live and historical; `start_live_data([...symbols...])` streams tick + 1-min bars; the library auto-reconnects and re-subscribes on drop (so some reconnect logic you hand-built for Angel is provided here — still keep your own staleness watchdog on top).
- **Symbol format differs from Angel One** (e.g. `NIFTY-I` for the near-month future, option symbols in TrueData's own format) — the `MarketDataAdapter` owns the symbol-format translation so strategy code stays broker/vendor-agnostic. Map TrueData symbols ↔ your internal instrument IDs in the instrument master.
- **Plan sizing**: default is 200 symbols; full option chains across NIFTY/BANKNIFTY/FINNIFTY/SENSEX need more — subscribe to the symbol count your chain collector requires, and have the collector subscribe only the strikes within a moneyness band (e.g. ±10% of spot) to stay within the plan.
- **Both adapters normalize to the same `Tick v2`** (§3.2) and publish to the **same Redis Streams tick bus** — nothing downstream (bar builder, strategy, UI) knows or cares which source is live. Flipping TrueData↔Angel is a config change.
- **Account data still comes from the broker** (funds/margin/orders/fills) — TrueData is market data only (§16.7 split-source rules).

**Concrete `TrueDataAdapter` build spec (Phase 1 first task):**
```
trading_platform/data/truedata_feed.py
  class TrueDataAdapter(MarketDataAdapter):
    - __init__(settings): TD(login, password, historical_api=settings.truedata_historical)
    - start_live(symbols): td.start_live_data(internal→truedata symbol map)  → on each tick,
        normalize to Tick v2 (add bid/ask/qty/oi/depth where provided) → publish tick.<segment> on Redis Streams
    - get_history(symbol, tf, start, end): td.get_historic_data(...) → DataFrame (Polars) for backtest/IV-rank
    - symbol map: internal instrument_id ↔ TrueData symbol (NIFTY-I near-month fut, option format);
        own the translation here so nothing upstream sees vendor symbols
    - staleness watchdog on top of the lib's auto-reconnect (force reconnect on silent socket in market hours)
    - resolve chain subscription via options_chain_collector: only strikes within ±10% of spot to fit the symbol plan
```
Feature-flag with `TRUEDATA_ENABLED`: when true, `TrueDataAdapter` is the live `MarketDataAdapter`; when false, fall back to the Angel One sharded feed (§3.2). Ship the adapter behind the flag so PAPER trading and tests keep running on the simulated/Angel path until the TrueData trial is verified live.
- **First smoke test in-repo**: a `scripts/truedata_smoketest.py` that connects, streams `NIFTY-I`/`BANKNIFTY-I` for 60s, prints tick rate + a sample Tick v2, and pulls 5 days of history — run this before wiring the adapter into the runtime.

### 3.2 Live feed & WebSocket redesign (accurate to the current code)

**What the current `data/live_feed.py` does well — KEEP:** background-thread SmartWebSocketV2 wrapper; reconnect with exponential backoff + jitter, capped at 5 min, retrying **indefinitely** past `_MAX_RETRIES` (correct — a feed with open positions must never go permanently dead); login separated from reconnect so a socket drop doesn't burn Angel One's stricter login quota, with periodic re-login only if the cached JWT itself starts failing; O(1) reverse token→symbol map; per-symbol `FeedStalenessTracker`; protected-symbols set so open-position subscriptions survive past the token cap; `inject_tick` replay hook; paise→rupee normalization. This is genuinely well-engineered — do not rewrite it, refactor around it.

**Accurate constraints (verified against Angel One SmartAPI, 2026):**
- **Max 3 WebSocket connections per client code; max 1000 tokens per connection.** The current single-socket design silently caps you at 1000 instruments. To track NIFTY+BANKNIFTY+FINNIFTY+SENSEX full option chains simultaneously you WILL exceed 1000 tokens — you must **shard subscriptions across up to 3 sockets** (3000 tokens total) with a connection pool that assigns tokens round-robin and tracks per-socket health. This is the single most important live-feed fix.
- Login rate-limit is stricter than the data rate-limit (the code already learned this the hard way — keep that discipline; centralize it in the `AngelOneGateway`, §3).
- Mode 3 (snap quote) gives OHLC-of-day + volume but **no bid/ask/depth**. For the microstructure/execution features (§3.1, §6.4) you need the **Depth-20 feed** (SmartWebSocket 2.0 depth beta) or at least mode-appropriate best-bid/ask. Extend the `Tick` model to v2: add `bid`, `ask`, `bid_qty`, `ask_qty`, top-5 depth, and `oi` for F&O — without these, order-book-imbalance and queue-position signals cannot exist.

**Architecture upgrade (latest — decouple ingestion from consumption):**
```
Angel One WS sockets (≤3, sharded)  ─┐
                                     ├─► normalize → Tick v2 → Redis Streams (tick.<segment>)
Depth-20 socket (microstructure)  ──┘                         │
                                        ┌────────────────────┼────────────────────┐
                                        ▼                    ▼                     ▼
                                  1m bar builder      Strategy Engine        UI WS gateway
                                  (→ Timescale)       (Feast online feats)   (per-tenant fan-out)
```
- Move from raw thread callbacks to **publishing every normalized tick onto Redis Streams**. Everything downstream (bar builder, strategy engine, staleness monitor, UI gateway) becomes an independent consumer group. This is what makes the feed reusable by many consumers and, later, many users — the current design hard-wires handlers into the feed object, which doesn't scale past one process.
- **Gap backfill on reconnect**: on every `_on_open`, fetch the candle history for the reconnect gap window and replay it into the bar builder before resuming live — so a 30s drop doesn't leave a hole in the 1m bars. The candle API already exists (`angel_one_history.py`).
- **Per-socket heartbeat & staleness**: track last-message time per socket, not just per symbol; a socket silent >N seconds in market hours is force-reconnected even if it never fired `on_close` (silent half-open sockets are a real Angel One failure mode).
- Replace the hardcoded `"abc123"` correlation ID with a per-connection UUID for debuggability.
- Keep it thread-based internally if you like (it works), but the **Redis Streams boundary** is the real fix — it turns the feed from a library into a service.

**UI WebSocket gateway (the `/ws/dashboard` side):** one server-side gateway subscribes to the Redis tick/portfolio streams and fans out to browser clients over authenticated, **per-tenant** channels (see §16). Apply the frontend realtime rules from §10 (rAF batching, virtualization). Never let the browser connect to the broker feed directly, and never send one tenant's portfolio/order data onto another tenant's channel — the `/ws/dashboard` auth gate that already exists must become tenant-scoped.

## 4. Strategy Engine

### 4.1 Framework
One `Strategy` protocol (signals only — never sizes, never orders): `on_bar/on_tick → list[Signal{instrument, direction/structure, conviction, features, ttl}]`. Port ShortVolStrategy, equity/derivatives templates from `strategies/factory.py` into it. Every signal persists with full feature snapshot for attribution.

### 4.2 The profit core: short-vol suite (extend what works)
- Iron condor + put credit spread (current) → add: short strangle with delta bands (only where margin allows), jade lizard, calendar when term-structure favorable.
- Entry gating: **VRP-rich condition** (§4.4a — ATM IV minus HAR-RV/GARCH forecast in its top historical quintile; IV rank > 50 as secondary confirm), no entry within EventRiskGuard blackout (RBI, budget, expiry-day gamma cutoff already enforced), strike selection guided by the fitted vol surface (§4.4a).
- Management: exit at 50% max profit; stop at 2× credit; delta-band re-hedge; expiry-morning square-off (EmergencySquareOff exists).
- Sizing: margin-aware (Angel One margin API), fractional-Kelly capped at 0.25×, per-underlying and portfolio-vega caps.
- Expand underlyings gradually: NIFTY → BANKNIFTY → SENSEX → FINNIFTY, each promoted only after its own 30-day paper record.

### 4.3 Directional revival — only via intraday features
Daily bars are proven dead. New attempt uses: 1m/5m bars, microstructure (spread, tick-run, relative volume), option-flow features (ΔOI, PCR shifts, IV skew changes) from the chain snapshots. **Meta-labeling** (López de Prado): base rules (ORB, VWAP reversion) propose; LightGBM filters/sizes. Same deployment law as now: walk-forward AUC must beat `0.5 + max(0.02, 2·SE_null)` or the model is not saved. If it never passes, the platform remains a pure short-vol shop — that is success, not failure.

### 4.4 State-of-the-art technique toolbox (2026 — all free, all local, all validation-gated)

These are the current best-practice techniques used by serious quant desks, adapted to what runs free on Apple Silicon. **Every one of them enters the money path only through the §5 validation gates** — they are candidate edges, not guaranteed ones.

**a) Volatility science (directly powers the proven short-vol edge — build first):**
- **HAR-RV realized-volatility forecaster** (heterogeneous autoregression on 1m realized vol) — the strongest simple RV baseline in the literature; runs alongside the existing GARCH `VolatilityForecaster`.
- **Variance Risk Premium (VRP) signal**: `VRP = ATM implied vol − forecast realized vol`. This is the *formal* reason short-vol makes money. Enter premium-selling only when VRP is rich (top-quintile of its own history), size proportional to VRP z-score. This converts the current "sell when IV rank > 50" heuristic into a measured edge with a tracked hit rate.
- **Vol-surface fitting (SVI/SABR)** per expiry from chain snapshots: rich/cheap strikes vs the fitted surface → better strike selection for condors/strangles; skew and term-structure slopes as regime features.
- **Time-series foundation models as RV forecasters**: Chronos-2 (Amazon, open), TimesFM (Google, open), and **Kronos** (MIT-licensed, pretrained specifically on OHLCV candles from 45+ exchanges, AAAI 2026) — run locally, zero-shot. Use them as *challenger* forecasters vs HAR-RV/GARCH in walk-forward; promote whichever wins OOS. Never as direct trade signals.

**b) ML discipline (López de Prado AFML suite — industry standard):**
- **Triple-barrier labeling** (profit-take/stop/time barriers) instead of fixed-horizon returns for all supervised labels.
- **Meta-labeling** (§4.3) with **sample-uniqueness weights** to de-bias overlapping labels.
- **Fractional differentiation** of price series — stationarity without memory loss — as a feature transform.
- **CPCV (combinatorial purged cross-validation)** replacing plain walk-forward for model selection; purged+embargoed folds everywhere.
- **Optuna** (free) for hyperparameter search *inside* CPCV, never on the full sample.
- **Probability calibration** (isotonic — `neural/calibration.py` exists, use it everywhere) + **conformal prediction** for honest uncertainty intervals: a signal whose conformal interval spans zero is auto-abstained, and size scales with interval width.

**c) Regime & monitoring science:**
- **Bayesian online change-point detection** (`ruptures`/BOCPD, free) on realized vol and breadth → faster regime-shift detection than the HMM alone; a detected change-point temporarily halves new-entry size platform-wide.
- **Drift monitoring** (Evidently OSS) on every deployed model's feature distributions; drift breach → auto-demote to baseline, alert.
- **Streaming anomaly detection** (`river` OSS) on the tick feed: price/volume anomalies gate entries (bad ticks are a real loss source with Angel One data).

**d) NLP tier below the LLM (cheap, fast, local):**
- A small finance-tuned sentiment transformer (FinBERT-class, free, MPS-accelerated) scores ALL news headlines in bulk; only high-impact/ambiguous items escalate to the LM Studio deep model. This keeps LLM queue depth low and the veto agent fast.

**e) Sizing science:**
- **Volatility targeting** at portfolio level (target annualized vol, e.g. 15%, scale gross exposure daily) layered under the existing drawdown limits.
- **Drawdown-constrained fractional Kelly** (cap 0.25×, floor at zero on regime change-points) — replaces ad-hoc position percentages.

**f) Explicitly rejected as hype (do not build):** on-chain/DeFi integrations, "quantum" optimizers (the repo already proved this is a classical fallback with no alpha), end-to-end deep RL for signal generation (kept frozen in labs; the literature and this repo's own results support rules+meta-labeling over RL for retail-scale data), and any paid signal/data subscription.

### 4.5 Portfolio allocator (replaces goal_governor as capital brain)
Rolling risk-adjusted allocation across strategy instances; correlation-aware (short-vol variants are highly correlated — cap combined vega, not just per-strategy notional); regime input from the retained market_intelligence node + HMM on realized vol/breadth.

## 5. Backtest & Validation Lab (fixes blocking gap #3)

Keep the event-driven engine and slippage model. Add, as enforced promotion gates stored in DB:
- Walk-forward optimization; **CPCV (combinatorial purged CV) with embargo** for any ML component; Optuna tuning only inside folds (§4.4b).
- Deflated Sharpe + PBO (probability of backtest overfitting) on every parameter sweep; reject PBO > 0.4.
- Monte Carlo trade-reshuffle → 95% max-DD estimate; must fit within risk limits.
- Full India cost model in every backtest: brokerage, STT (options sell-side on premium), exchange txn, GST, stamp, SEBI fees. Surface "gross vs net after costs" everywhere — premium selling lives or dies on this.
- Promotion ladder (enforced by code, visible in UI): backtest gates → ≥30 paper days → live at min size → scale per allocator. Live-vs-paper slippage delta monitored; >threshold auto-demotes.

### 5.1 MLOps discipline (latest — makes "models must earn deployment" mechanical)
- **MLflow registry is the single source of truth** for every model that touches the money path: version, training data hash, CPCV metrics, DSR/PBO, calibration curve, and the exact gate results are logged. Serving loads only `Production`-stage models from the registry; nothing hand-copied into `models/`.
- **Champion/challenger, automated**: new models enter as challengers, shadow-score live alongside the champion for N days, and only promote if they beat it OOS *and* on live shadow. The repo already has champion/challenger hooks — wire them to the registry.
- **Drift & decay monitoring** (Evidently OSS): feature drift, prediction drift, and realized-edge decay tracked per model; breach → auto-demote to baseline + alert. A model whose live edge decays below its OOS lower bound is retired automatically.
- **Backtest reproducibility**: every backtest is a logged MLflow run (config + data snapshot + git SHA) so any equity curve in the UI can be regenerated bit-for-bit. Golden-backtest regression in CI.
- **Data versioning**: raw candle/chain snapshots written immutably (Parquet, date-partitioned); DuckDB views over them. No silent rewrites of history.

## 6. Risk & Execution

### 6.1 Risk Service
Extract RiskEngine + guards behind one boundary; the ONLY path to the broker. Add: portfolio Greeks caps (net delta, vega, gamma-near-expiry), historical-simulation VaR on the options book, auto-demotion (halve size) when live performance drops below OOS baseline band. Everything else (kill switch, daily loss, margin ceiling, naked-option ban) already exists — keep tests green.

### 6.2 SEBI retail-algo compliance (mandatory since 1 Apr 2026)
- Every automated order carries its exchange-issued **Algo-ID** via Angel One's registered-algo flow; tag stored on every `order_events` row.
- All broker API traffic from one whitelisted **static IP** (deploy profile pins egress).
- Immutable order-lifecycle audit (OMS event store exists — add Algo-ID, strategy, signal hash, risk-check results to each event).
- ComplianceGuard's 200 orders/day stays; add exchange OTR monitoring.

### 6.3 Execution Service (fixes blocking gap #2)
- **Reconciliation loop**: every 30s in market hours, compare broker positions/orders/funds vs internal ledger; mismatch → alert + halt new entries + surface diff in UI. Orphan-order detection, rejected-order classification, partial-fill handling.
- Multi-leg options routing: hedge-first leg sequencing (buy protective legs before selling), basket margin preview before commit.
- Token automation: TOTP login at 08:45 IST with CRITICAL alert on failure.
- Smart orders: limit-at-touch with 2-tick chase then market on urgency; slice if size > x% of top-5 depth.

### 6.4 Execution alpha (latest — 10–20 bps/trade is real money on premium selling)
Execution quality is a measurable, controllable edge — the difference between good and bad fills is routinely 10–20 bps, which on high-frequency condor adjustments compounds into a meaningful chunk of annual return. Build execution as a first-class, measured discipline:
- **Order-book microstructure signals** drive placement: queue position estimate, depth imbalance, and short-horizon fill-probability model decide limit vs. cross-the-spread, chase aggression, and slice timing. Trained/validated offline on recorded depth (§3.1 features), same deployment law.
- **Almgren-Chriss-style scheduling** for any multi-lot options basket: minimize expected impact + timing risk given the leg's ADV and urgency; front-load hedge legs.
- **Transaction Cost Analysis (TCA) loop**: every fill scored vs. arrival price and vs. a VWAP benchmark; implementation shortfall attributed to spread/impact/timing and fed back into the placement model and the backtest slippage calibration. TCA dashboard in Ops (§10).
- **RL for execution is allowed here (and only here)**: the literature shows RL improving execution efficiency materially over naive submit-and-leave, and execution is a well-posed, low-dimensional RL problem (unlike signal generation). Keep it in the frozen-labs tier until TCA proves the rule-based scheduler is the bottleneck; promote via the same gates. Never let RL touch *what* to trade — only *how* to fill an already-approved order.

## 7. Database migration (phased, non-destructive)

Phase A: stand up Postgres+Timescale alongside SQLite; new writes dual-write; Timescale owns ticks/bars/chain/features. Phase B: migrate trades/orders/OMS events/exit plans with a one-shot script + checksum verification; SQLite becomes read-only archive. Keep the existing exit-plan restore-on-restart semantics.

Core OLTP tables: `instruments`, `strategies` (+version history), `signals`, `orders`, `order_events` (immutable, Algo-ID), `trades` (attribution: strategy, regime, conviction, agent votes, slippage, costs), `positions_snapshot`, `risk_limits`, `risk_events`, `backtests` (+gate results), `promotions`, `event_calendar`, `agent_decisions`, `daily_pnl`.
Timescale hypertables: `ticks`, `bars_1m` (+continuous aggregates), `option_chain_snapshots`, `greeks_ts`, `features`.
Qdrant collections: `documents` (RAG corpus), `trade_journal`.

## 8. Intelligence layer: real RAG + LLM (replaces stubs)

**RAG engine** (upgrade `news/` into it):
- Ingest: NSE/BSE announcements & filings, earnings transcripts, Moneycontrol/ET/Reuters RSS, RBI/SEBI circulars, macro calendar; internal corpus: trade journal, backtest reports, `decision_traces` (already on disk).
- Pipeline (2026 production patterns): **contextual retrieval** (each chunk prefixed with an LLM-generated context blurb before embedding — large recall gain), **hybrid dense+BM25 with RRF fusion**, **ColBERT/late-interaction** (per-token embeddings) as an option for high-precision filing search, then **local cross-encoder rerank** → freshness-weighted, deduped, every claim cited to source chunks. Structure-aware chunking (filings by section, transcripts by speaker). Local embeddings + reranker (§8.1); Qdrant (free OSS, local Docker) with rich metadata (ticker, sector, doc-type, event-date, source tier). Ingestion uses only free sources (exchange RSS/public filings); no paid data APIs.
- **Adaptive RAG router**: a cheap classifier routes each query by complexity — simple lookups take the fast single-shot path; multi-hop questions ("what changed for BANKNIFTY constituents since last policy?") take the agentic path. Don't pay 3–10× LLM calls when you don't need to.
- **GraphRAG layer** for the entity-relationship questions that matter in markets: a knowledge graph of tickers ↔ sectors ↔ events ↔ suppliers/peers, so the system can reason over contagion and lead-lag ("who else is exposed to this news?") that flat vector search misses.
- **RAG evaluation harness** (RAGAS/DeepEval, OSS): retrieval faithfulness, context precision/recall, and answer-grounding scored on a fixed question set in CI — so RAG quality is measured, not assumed, exactly like the trading models.
- Structured outputs consumed by the pipeline (not prose): per-ticker sentiment (−1..1), event-risk flags feeding EventRiskGuard's calendar, novelty scores, morning brief / EOD wrap.

### 8.1 Local model plan (LM Studio, 128 GB unified memory — replaces the stub gateway)

All inference is local, free, and private. LM Studio serves an OpenAI-compatible endpoint; the code talks to it through LiteLLM so any model swap is pure config (`LOCAL_LLM_PRIMARY_MODEL` etc. already exist). With 128 GB unified memory you can comfortably run:

| Tier | Model (pick best available in LM Studio at build time) | Approx. footprint | Used for |
|---|---|---|---|
| Deep reasoner | Qwen3-72B / Llama-3.3-70B class, Q4_K_M MLX/GGUF | ~40–45 GB | Regime Analyst (daily), Trade Journalist, weekly pattern mining |
| Fast worker | Qwen3-14B / Gemma-3-12B class, Q4 | ~8–10 GB | Signal Veto Agent, sentiment scoring, Copilot chat |
| Embeddings | nomic-embed-text-v1.5 or bge-m3 | <2 GB | RAG pipeline |
| Reranker | bge-reranker-v2-m3 (sentence-transformers, MPS) | <2 GB | RAG top-k precision |

Rules: keep deep + fast models resident simultaneously (fits easily in 128 GB alongside the trading stack); respect `LOCAL_LLM_MAX_CONCURRENT_CALLS` (local inference is serial-ish — queue, don't burst); raise `LOCAL_LLM_TIMEOUT_SECONDS` for the deep tier (60s) and keep 15s for the fast tier; use LM Studio's structured-output (JSON schema) mode for every agent; benchmark tokens/sec at setup and record it in `ai_capabilities` so latency budgets are honest. Latency consequence: the Veto Agent reviews swing and short-vol entries (seconds-scale is fine) — it must never sit in any tick-latency path.

**LLM agents** (LangGraph, all pointed at the local LM Studio server):
1. **Regime Analyst** — daily+intraday; cross-checked against quantitative regime; disagreement lowers system conviction multiplier.
2. **Signal Veto Agent** — reviews each short-vol/swing entry against RAG context (pending events, fresh news on the underlying). Powers: `approve | veto | downsize`. **Never initiates, never upsizes** — consistent with "advisory ≠ safety".
3. **Trade Journalist** — structured postmortem per closed trade → embedded into Qdrant → weekly pattern mining ("condor losses cluster on expiry Wednesdays with VIX > 16").
4. **Copilot (UI chat)** — explains any decision by tracing signal features + risk checks + agent votes (decision_traces exist — index them); natural-language → backtest config.
5. **Compliance Watcher** — flags new SEBI/exchange circulars affecting retail algo rules.

Agent patterns (2026): agents use **reflection** (critique their own output before returning), **tool use** (retrieve chain data, price series, backtest results as function calls — not just text), and **structured JSON-schema outputs** validated on every call. The Copilot and multi-hop research questions run the agentic loop; single-fact lookups don't. **LangGraph checkpoints** persist agent state so a long research task survives a restart. Every agent is a node with a narrow contract — no free-roaming "do everything" agent near the money path.

Guardrails: JSON-schema-validated outputs, per-agent daily call budget (local compute is free but finite — protect trading-loop latency), prompt-hash + model-version logged in `agent_decisions`, LM Studio outage degrades to pure-quant mode (never blocks risk checks or exits). Update `api/ai_capabilities.py` so the DEGRADED/ADVISORY report stays truthful.

## 9. Goal governance — make the target defensible

Replace `YEARLY_PROFIT_TARGET=₹5cr` with: target expressed as a risk-adjusted return band (e.g. 25–40%/yr at max 10% drawdown — aggressive but not fantasy for premium selling with tight management on ₹10L), tracked as run-rate vs band. PositionScaler scales with equity milestones, never overrides drawdown limits (already true — keep). The UI shows progress honestly, including cost drag.

## 10. Frontend redesign (React/Vite/Zustand — keep stack, consolidate 17 → 8)

Design language: dark-first, information-dense (terminal, not marketing site), every number drill-downable, feed-staleness indicator always visible, kill switch always reachable (including mobile-width), P&L colors with colorblind-safe option, WebSocket topics mirroring the event bus.

**Realtime architecture (latest — this is where naive dashboards fall over):** one shared WebSocket manager (not per-widget connections) validates and dispatches typed messages (`{type, source, timestamp, payload}`) into the Zustand store. **Never `setState` per tick** — buffer messages in a ref and flush on `requestAnimationFrame` (~60fps cap) so a fast feed can't choke React. **Virtualize** every long list/table (positions, OMS events, chain) so 10k rows render like 100. **Isolate high-frequency components** (live P&L, chain, order book) from slow ones (news, journal) so one can't block the other's render. Charts: keep **TradingView Lightweight Charts** (WebGL/Canvas) for price/candles; add **Apache ECharts** for heatmaps/payoff/analytics. Heavy math (Greeks aggregation, VaR) is precomputed server-side and streamed, or offloaded to a **Web Worker** — never on the render thread.

1. **Command Center** (merge Dashboard/Account/Engine): live P&L (realized/unrealized, day/total), equity curve, open positions with per-position Greeks & risk, strategy status grid with mini equity curves, regime badge, margin gauge, reconciliation status, **KILL SWITCH**.
2. **Options Desk** (grow ShortVolPanel): chain with OI/IV heatmap, IV-rank history chart, PCR/max-pain, condor/strangle payoff diagrams with live Greeks, margin preview, one-click preview→enter flow (preview path already exists — surface it).
3. **Strategy Studio** (merge Strategies/Signals/Policies/Tournament): enable/param-edit per strategy (schema-driven forms), promotion-ladder status, live-vs-backtest overlay chart, allocator weights.
4. **Backtest Lab** (grow Backtest): config builder or Copilot chat, run queue with progress, tearsheets (quantstats), parameter-sweep heatmaps, walk-forward visualizations, gate results, promote button (disabled until gates pass).
5. **Risk Console** (grow Risk): limits editor (two-step confirm), VaR & exposure breakdowns, portfolio Greeks totals, risk-event log, blackout calendar, compliance/OTR status.
6. **Intelligence** (merge AICouncil/Intelligence/NeuralLab): morning brief, per-ticker sentiment, agent decisions with citations, Copilot chat, "explain this trade" deep links from any trade row.
7. **Journal & Analytics** (new): calendar P&L heatmap, attribution by strategy/regime/underlying/time-of-day, cost breakdown (brokerage/STT/slippage vs gross), journalist postmortems and mined patterns.
8. **Ops** (merge Execution/Models/TraceReplay/AILab): broker/token/feed health, OMS event search, reconciliation diffs, trace replay, experimental labs behind a flag.

## 11. DevOps & deployment (fixes blocking gap #4) — all local, all free

Everything runs on the Mac (128 GB unified memory is ample: Postgres+Timescale, Redis, Qdrant, observability, and both LLM tiers together use well under half of it):
- Docker Compose profiles `research|paper|live` for Postgres+TimescaleDB, Redis OSS, Qdrant, Prometheus, Grafana, Loki. LM Studio runs natively (not in Docker) for Metal GPU acceleration.
- LIVE + SEBI static-IP requirement, cloud-free: get a fixed-IP add-on from your ISP and whitelist it with Angel One. UPS/auto-restart discipline: `launchd` services with restart-on-crash, machine sleep disabled during market hours, and an alert if the feed dies (a home-hosted live system's biggest risk is silent downtime).
- Secrets via `.env` (gitignored — already the rule); remove `AWS_REGION` vestige.
- Prometheus metrics from every service (tick lag, order latency, rejection rate, reconciliation diffs, LLM latency/queue depth) + Grafana dashboards + Loki logs; Telegram alerts (free bot) tiered INFO (fills) / WARN (reconnects, demotions) / CRITICAL (kill switch, reconciliation mismatch, token failure, daily-loss breach).
- Nightly: DB backup to a second local disk, instrument sync (exists), EOD report, model/feature drift check.
- CI keeps the 640-test suite + contract tests green; add golden-backtest regression (results must not drift unexplained). Respect the existing macOS landmine: plain `GradientBoostingClassifier`, never `HistGradientBoostingClassifier`.

## 12. US market extensibility (design now, build later)

Money as `Decimal` + currency; instrument model carries exchange/currency/tick/lot/session; exchange-calendar lib instead of hardcoded IST checks (`now_ist()` stays for NSE); `BrokerAdapter` interface already exists — add Alpaca (paper-friendly) first, IBKR later; pluggable cost model (India taxes vs US SEC/TAF + PDT guard for <$25k accounts); logical universes ("NIFTY_INDICES", "SP500_LIQUID") resolved per market.

## 13. Build order (each phase keeps tests green and ships)

1. **Weeks 1–2 — Data spine + feed:** `MarketDataAdapter` with **Angel One sharded adapter + Upstox adapter** (free, individual-KYC only), both normalizing to **Tick v2 → Redis Streams tick bus** (§3.0, §3.2); Upstox for full option chain + Greeks + expired-F&O history. AngelOneGateway centralization for broker/account side, tick→bar builder, chain snapshots + IV-rank history, Timescale phase A, **Polars/DuckDB** research layer, **Feast** feature store skeleton, staleness guards. *(TrueData adapter stays flagged off — enable only if you later license a vendor. Everything else depends on this phase.)*
2. **Weeks 3–4 — Execution hardening + TCA:** reconciliation loop, token automation, Algo-ID plumbing, multi-leg hedge-first routing, **TCA loop** measuring every fill. Short-vol keeps trading through it.
3. **Weeks 5–6 — Consolidation + tenancy seams:** retire A as order path, demote B to context nodes, Strategy framework port, allocator v1, runtime decomposition finished, **`tenant_id` on the schema + Postgres RLS + per-tenant `BrokerSessionManager` skeleton** (multi-user paper works end of this phase — §16.4).
4. **Weeks 7–8 — Validation lab + MLOps:** CPCV/DSR/PBO gates, cost model everywhere, **MLflow registry + Evidently drift**, promotion ladder + Backtest Lab screen.
5. **Weeks 9–10 — Short-vol suite expansion + vol science:** strangle/jade-lizard/calendar variants, HAR-RV forecaster, VRP entry signal, SVI surface fitting, Greeks caps + VaR, Options Desk screen.
6. **Weeks 11–13 — Intelligence:** RAG pipeline on `news/` (contextual retrieval + adaptive router + GraphRAG + RAGAS evals), Qdrant, LangGraph agents (veto-only, reflection + tool use), Copilot, Intelligence + Journal screens.
7. **Weeks 14+ — Directional revival attempt + advanced ML:** intraday features, triple-barrier labels, meta-labeling with conformal abstention, foundation-model challengers (Kronos/Chronos-2/TimesFM as RV forecasters), change-point detection + drift monitoring — all under the deployment law; ship only what passes. Then US-market adapter work.

## 14. Acceptance criteria

- One signal pipeline; zero orders originate outside Strategy framework → Risk Service → OMS.
- A strategy can go idea → backtest → gates → paper → live entirely through the UI with every gate enforced and every order carrying Algo-ID + full audit trail.
- Reconciliation mismatch halts entries within 60s and is visible in UI.
- Every closed trade has attribution + cost breakdown + journalist postmortem.
- `api/ai_capabilities.py` startup report accurately describes every AI component's real status.
- Kill switch works from a phone.

## 16. Multi-user / multi-tenant architecture (you are single-user paper today; design for many)

Today the platform runs on one hardcoded set of Angel One credentials in `.env`, paper mode. Going multi-user is not a feature you bolt on later — it changes the data model, the broker layer, and the legal posture. Build the seams now even while only you use it.

### 16.1 The two-plane rule (this is the whole design)
Separate **market data** from **order/portfolio** — they have different limits, pricing, and legal status:
- **Order & portfolio plane — strictly per-user, isolated.** Every user's orders, positions, funds, risk limits, kill switch, and P&L live under *their own broker credentials* and are tagged with *their own* exchange Algo-ID. Nothing is shared. One user's kill switch never touches another's; one user's daily-loss breach halts only that user.
- **Market-data plane — shared where legal, per-user where required (see 16.3).** Ingesting the same NIFTY tick once and reusing it for computation across users is efficient and fine *internally*; **displaying/redistributing** exchange price data to end users is legally gated.

### 16.2 Tenancy in the data model & runtime
- **`tenant_id` (user_id) on every row** — orders, trades, signals, positions, risk_limits, exit_plans, agent_decisions, daily_pnl. Enforce with **Postgres Row-Level Security** so a query can never cross tenants even with a bug. (This is why the SQLite→Postgres migration in §7 is a prerequisite for multi-user — SQLite has no RLS.)
- **Per-tenant broker session**: the current single `LiveTickFeed`/broker client becomes a **`BrokerSessionManager`** keyed by tenant. Each tenant has encrypted credentials in a **secrets vault** (open-source: HashiCorp Vault OSS, or age/SOPS-encrypted per-tenant blobs — never plaintext creds in a shared `.env`), and an independent token/TOTP lifecycle (each Angel One account needs its own 08:45 IST login).
- **`TradingRuntime` becomes per-tenant-scoped** or tenant-aware: the composition root you're already decomposing (§1) should construct services with a tenant context, not global singletons. Shared, non-user-specific components (instrument master, reference data, market-data ingestion, RAG corpus, ML model registry) stay singletons; anything holding money or orders is per-tenant.
- **Isolation guarantees to test**: tenant A cannot see B's positions via REST, WS, or the Copilot; A's risk breach/kill switch is scoped to A; A's Algo-ID never appears on B's orders; A's LLM/agent context never retrieves B's private journal.

### 16.3 The legal constraint that shapes everything (do not skip)
**Exchange market data (NSE/BSE) is the exchange's property; unauthorized redistribution or public display is prohibited and requires a license.** Live prices you pull with *your* broker credentials are licensed to *you*, not to a public app. So for a multi-user product you have exactly three compliant options:
1. **Bring-your-own-account (recommended start):** each user connects *their own* broker account/API key. Their live data is their own entitlement; you never redistribute. This is the clean path and matches SEBI's retail-algo framework (each user's algo runs under their own account, their own Algo-ID, static IP registered). Downside: each user needs a broker account + (for Zerodha) a data subscription.
2. **Licensed market-data vendor** for the shared plane: subscribe to a redistribution-licensed feed (e.g. TrueData) so the platform can legally show one data stream to all users. This is the true-SaaS path; it costs money and needs a data-vendor agreement, so it violates the current "free/local" constraint — flag it as a future paid option, not now.
3. **Paper-only display with delayed/synthetic data** for users who haven't linked an account: show simulated or delayed prices, clearly labeled — never live exchange ticks. Your existing `SimulatedBrokerClient` + synthetic universe already supports this; make it the default for unlinked users.

**Also: a platform that offers algos to third parties must itself be registered/approved by the exchanges under SEBI's 2026 framework** (the obligation that sits on brokers/providers, not individual retail users). If this ever becomes a product for other people rather than a personal tool, that registration is a hard prerequisite — note it in the README and treat "other people trading real money through my platform" as a regulated activity, not a weekend launch.

### 16.4 Multi-user paper trading (your immediate need)
For now, everyone is a paper user. Concretely:
- One shared **market-data ingestion** account (yours) feeds the simulation engine; each paper user gets an **isolated `SimulatedBrokerClient` + PortfolioLedger + RiskEngine** under their `tenant_id`. Same slippage/cost model, independent equity curves, independent kill switches.
- Auth: real user accounts (the app already has `API_AUTH_REQUIRED`/token auth — extend to per-user identities, e.g. OAuth or local user table + JWT). The `/ws/dashboard` snapshot becomes tenant-scoped.
- This lets multiple people paper-trade side by side *today*, and the exact same tenant seams carry over to live-per-account later with zero re-architecture.

### 16.5 Is Angel One the right broker? (evaluation)
Short answer: **Angel One is a good choice to keep as your first adapter, but build the broker layer multi-broker and multi-account from day one** — the `BrokerAdapter` interface already exists, so add others behind it rather than betting on one.

| Broker | Data API cost | Order rate | Token lifecycle | WS limits | Multi-user fit | Verdict |
|---|---|---|---|---|---|---|
| **Angel One SmartAPI** | **Free** (data + orders) | ~10/sec | Daily TOTP re-login | 3 conns × 1000 tokens | Each user = own free account; no data fee per user | **Keep as primary.** Free is decisive for BYO-account multi-user |
| **Zerodha Kite Connect** | ₹500/mo **per user** data | 3/sec | Daily token | Documented, stable | Industry-standard reliability, but ₹500×users adds up | Add as premium adapter; best stability |
| **Dhan** | Free | Good | Longer-lived | Good, automation-focused | Free + native paper trading; strong dev workflow | **Add second** — free + automation-friendly |
| **Upstox** | Free | ~10/sec | **Longer-lived tokens** (less daily-login pain) | Good, low latency | Longer tokens ease multi-account ops | Strong alternative; low latency |

Recommendation: **primary = Angel One** (free, you already have it working, free-per-user is the winning property for bring-your-own-account), **second adapter = Dhan or Upstox** (free + friendlier token lifecycle reduces the per-user 08:45 login burden that becomes painful at N users). Keep Zerodha as an optional premium adapter for users who want its reliability and will pay the data fee. **Do not** try to serve many users off one Angel One account's data — that's the redistribution problem in 16.3. The multi-broker adapter layer also de-risks you against any single broker's API outage or policy change.

### 16.7 Broker & data decision record (read this before writing any adapter)

**Principle: decouple the execution broker from the market-data source.** They have different limits, pricing, quality profiles, and legal status. Bind neither into strategy code — both sit behind adapters.

**Execution (order routing):**
- **Primary: Angel One SmartAPI** — free orders + free data, already integrated, ~10 orders/sec. Free-per-user is what makes bring-your-own-account (§16.3) economically viable.
- **Second: Dhan** (25 orders/sec, webhook-friendly, native paper) or **Upstox** (25 orders/sec, longer-lived tokens → less per-user daily-login burden). Add one for redundancy and to hedge single-broker outage/policy risk.
- **Optional premium: Zerodha Kite Connect** — best-in-class reliability, but ₹500/user/month data fee; offer only to users who want it.

**Market data (the plane you care most about):**
- Broker WebSocket feeds (Angel included) are **throttled, capped at 3×1000 tokens, and drop ticks under load.** Adequate for a handful of instruments; strained by full option chains + Greeks across NIFTY/BANKNIFTY/FINNIFTY/SENSEX simultaneously.
- **Primary data plane: TrueData** (exchange-authorized L1 vendor) — purpose-built for exactly this: clean full option chain, Greeks, tick + 1/5-min bars + deep historical, redistribution-licensed for multi-user, ~₹2,599+GST/month for real-time + historical. Access via `truedata-ws`. Setup steps in `docs/TRUEDATA_SETUP.md`. This is the target from §3.0.
- **Free fallback: Angel One's own feed**, 3-socket sharded + Depth-20 + Tick v2 (§3.2) — usable at zero cost while you set up TrueData, and as a backup adapter if the vendor feed is down. Global Datafeeds is an alternative authorized vendor if you ever want a second.
- A broker feed is **not** legally redistributable to multiple users; the authorized vendor is the only compliant shared multi-user data source (§16.3).
- Keep a `MarketDataAdapter` interface so Angel-feed and TrueData-feed are swappable; strategies never know which is behind it. Same normalization → Redis Streams tick bus (§3.2) regardless of source.

**One-line rule:** broker = execution + *your own* data entitlement; authorized vendor (TrueData) = the *shared* multi-user data plane when you outgrow free broker feeds.

**Split-source execution model (data from vendor, orders to broker — the target design):**
This is fully supported and is the recommended end state. `MarketDataAdapter` and `BrokerAdapter` are independent — the data source and the order destination need not be the same company. Rules that make it safe:
- **Decision data comes from the market-data plane** (vendor or broker feed) → signals, Greeks, IV, regime, backtests. This is where "fetch live data anywhere" applies — and *anywhere* means **any exchange-authorized vendor**, never free/scraped/unofficial sources (delayed, unreliable, and illegal to redistribute — banned by the §0 constraint list).
- **Account data always comes from the executing broker**: funds, margin, order status, fills, positions. A data vendor cannot supply these. So the broker adapter is never optional.
- **Order pricing is validated against the broker's own top-of-book at submit time**, not the vendor's LTP — vendor and broker quotes can differ by a tick, and the fat-finger guard + limit price must reflect the venue you're actually trading on. The reconciliation loop (§6.3) is the backstop: internal ledger is truth-checked against broker positions every 30s regardless of data source.
- **Clock/tick skew is acceptable for this platform** (options premium selling, swing, intraday — not HFT). Do not add cross-source latency arbitrage assumptions; if a strategy's edge depended on data/execution being the same nanosecond source, it would fail validation anyway.
- Practical path: **Phase 1 keep data + execution both on Angel One** (simplest, free, one credential set). **Flip the data plane to TrueData later** by swapping only the `MarketDataAdapter` — zero change to strategy, risk, or execution code. That clean swap is the whole point of the adapter split.

### 16.6 Scaling notes
- Connection budget: at N live users on bring-your-own-account, you run N order sessions + N (or shared) data feeds — the Redis Streams boundary (§3.2) means one ingestion process can serve many internal consumers, but per-user *entitlement* still requires per-user broker sessions. Pool and stagger the 08:45 logins to avoid thundering-herd rate limits.
- Per-tenant resource caps (max symbols, max strategies, LLM call budget) so one user can't starve others of the shared 128 GB box.
- Observability tagged by `tenant_id`; per-tenant Grafana rows; alerts routed per user.

## 15. Honest framing (keep in README)

No architecture guarantees profit. This redesign concentrates on the one edge this codebase has actually validated (selling rich implied volatility with strict management), eliminates the operational failure modes that silently eat returns (stale data, unreconciled positions, unmeasured costs), gates every new idea behind overfitting-resistant validation, and uses LLMs/RAG only where they add measurable value — context, veto, and postmortems — never as an oracle. Costs (STT + slippage) and tail risk are what kill retail short-vol accounts; the cost model, Greeks caps, VaR, and event blackouts exist to make both visible and bounded. Expect the paper-trading gates to feel slow. That is the design working.
