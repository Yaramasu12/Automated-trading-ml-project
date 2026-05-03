import type {
  AccountStatus,
  BacktestResult,
  DBSummary,
  DailyPnl,
  EquityCurvePoint,
  HealthResponse,
  BrokerCapabilityStatus,
  ModelCatalogEntry,
  EventBusSummary,
  ManualApprovalStatus,
  RuntimeState,
  SignalScanResult,
  StrategyEvaluationResult,
  TargetProgress,
  Trade,
  VolatilityForecast,
  WalkForwardResult,
} from './types'

const BASE = import.meta.env.VITE_API_URL ?? '/api'

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

const get = <T>(path: string) => req<T>(path)
const post = <T>(path: string, body?: unknown) =>
  req<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined })

// ─── Health & State ───────────────────────────────────────────────────────────
export const getHealth = () => get<HealthResponse>('/health')
export const getState = () => get<{ execution_mode: string; live_armed: boolean; kill_switch_active: boolean }>('/state')
export const setExecutionMode = (mode: string) => post<RuntimeState>('/execution-mode', { mode })
export const armLive = (armed: boolean) => post<RuntimeState>('/live/arm', { armed })
export const setKillSwitch = (active: boolean) => post<RuntimeState>('/kill-switch', { active })

// ─── Instruments ─────────────────────────────────────────────────────────────
export const getDataStatus = () => get<{ instrument_cache_exists: boolean; current_universe_count: number; angel_one_configured: boolean }>('/data/status')
export const refreshInstruments = () => post('/data/instruments/refresh', {})
export const loadCachedInstruments = () => post('/data/instruments/load-cache', {})

// ─── Strategies ───────────────────────────────────────────────────────────────
export const getStrategyCatalog = () => get<{ count: number; by_family: Record<string, number>; strategies: unknown[] }>('/strategies/catalog')
export const evaluateStrategies = (payload: Record<string, unknown>) =>
  post<StrategyEvaluationResult>('/strategies/evaluate', payload)

// ─── Signals ─────────────────────────────────────────────────────────────────
export const scanSignals = (payload: Record<string, unknown>) =>
  post<SignalScanResult>('/signals/scan', payload)
export const runShadow = (payload: Record<string, unknown>) =>
  post<SignalScanResult>('/shadow/run', payload)

// ─── Backtest ─────────────────────────────────────────────────────────────────
export const runBacktest = (payload: Record<string, unknown>) =>
  post<BacktestResult>('/backtests/run', payload)
export const runWalkForward = (payload: Record<string, unknown>) =>
  post<WalkForwardResult>('/backtests/walk-forward', payload)

// ─── Models ───────────────────────────────────────────────────────────────────
export const getModelCatalog = () => get<{ count: number; models: ModelCatalogEntry[] }>('/models/catalog')
export const getVolatilityForecast = (payload: Record<string, unknown>) =>
  post<{ forecast: VolatilityForecast; interval_evaluation: { coverage: number; healthy: boolean } }>('/models/volatility-forecast', payload)
export const getGarchForecast = (payload: Record<string, unknown>) =>
  post<{ forecast: VolatilityForecast; garch_params: Record<string, number> }>('/models/garch-forecast', payload)
export const classifyRegime = (payload: Record<string, unknown>) =>
  post<{ symbol: string; regime: string; probabilities: Record<string, number>; classifier_trained: boolean }>('/models/regime-classify', payload)
export const getIVSurface = (payload: Record<string, unknown>) =>
  post<{ underlying: string; spot_price: number; atm_iv: number; skew: number; points: unknown[] }>('/derivatives/iv-surface', payload)

// ─── Portfolio ────────────────────────────────────────────────────────────────
export const getTargetProgress = (payload: Record<string, unknown>) =>
  post<TargetProgress>('/portfolio/target-progress', payload)

// ─── Risk ─────────────────────────────────────────────────────────────────────
export const getSupervisorDecision = (payload: Record<string, unknown>) =>
  post<{ action: string; reason: string; max_allocation_multiplier: number }>('/risk/supervisor-decision', payload)

// ─── Account ─────────────────────────────────────────────────────────────────
export const getAccountStatus = () => get<AccountStatus>('/account/status')
export const getAccountSnapshot = () => get<{ broker: string; snapshot: unknown }>('/account/snapshot')

// ─── Monitoring ───────────────────────────────────────────────────────────────
export const getMonitoringMetrics = () => get<{ status: string; total_orders: number; rejection_rate: number; average_latency_ms: number; uptime_seconds: number }>('/monitoring/metrics')
export const getMonitoringEvents = (limit = 30) => get<{ events: unknown[] }>(`/monitoring/events?limit=${limit}`)

// ─── Database ─────────────────────────────────────────────────────────────────
export const getDBSummary = () => get<DBSummary>('/db/summary')
export const getEquityCurve = (mode?: string, limit = 200) =>
  get<{ curve: EquityCurvePoint[] }>(`/db/equity-curve?${new URLSearchParams({
    ...(mode ? { execution_mode: mode } : {}),
    limit: String(limit),
  }).toString()}`)
export const getDailyPnl = (limit = 30) => get<{ history: DailyPnl[] }>(`/db/daily-pnl?limit=${limit}`)
export const getRecentTrades = (limit = 50) => get<{ trades: Trade[] }>(`/db/trades?limit=${limit}`)
export const getRiskEvents = (limit = 30) => get<{ events: unknown[] }>(`/db/risk-events?limit=${limit}`)

// ─── Feed ─────────────────────────────────────────────────────────────────────
export const startFeed = (symbols?: string[]) => post('/feed/start', symbols ? { symbols } : {})
export const stopFeed = () => post('/feed/stop', {})
export const getFeedSnapshot = () => get<{ running: boolean; subscribed_symbols: string[]; tick_count: number }>('/feed/snapshot')

// ─── Orders ───────────────────────────────────────────────────────────────────
export const previewOrder = (payload: Record<string, unknown>) => post('/orders/preview', payload)
export const paperOrder = (payload: Record<string, unknown>) => post('/orders/paper', payload)

// ─── Execution Plane ──────────────────────────────────────────────────────────
export const getSchedulerStats = () => get<Record<string, unknown>>('/execution/scheduler/stats')
export const getOmsEvents = (limit = 50) => get<{ count: number; events: unknown[] }>(`/execution/oms/events?limit=${limit}`)
export const getManualApprovals = () => get<ManualApprovalStatus>('/execution/manual-approvals')
export const approveManualApproval = (requestId: string, approval_reason: string) =>
  post(`/execution/manual-approvals/${requestId}/approve`, { approval_reason })
export const rejectManualApproval = (requestId: string, reason: string) =>
  post(`/execution/manual-approvals/${requestId}/reject`, { reason })
export const getBrokerCapabilities = () => get<BrokerCapabilityStatus>('/execution/broker-capabilities')
export const squareOff = (payload: Record<string, unknown>) => post('/execution/square-off', payload)
export const getEventSummary = () => get<EventBusSummary>('/events/summary')
export const getRecentEvents = (limit = 50, stream?: string) =>
  get<{ count: number; events: unknown[] }>(`/events/recent?limit=${limit}${stream ? `&stream=${stream}` : ''}`)

// ─── Intelligence / Performance ──────────────────────────────────────────────
export const analyzeNews = (payload: Record<string, unknown>) => post('/news/analyze', payload)
export const getNewsEvents = (limit = 30) => get<{ count: number; events: unknown[]; features: Record<string, unknown> }>(`/news/events?limit=${limit}`)
export const getNewsFeatures = () => get<Record<string, unknown>>('/news/features')
export const getCurrentRegime = (symbol = 'NIFTY') => get<Record<string, unknown>>(`/regime/current?symbol=${symbol}`)
export const getPerformanceSummary = (days = 30) => get<Record<string, unknown>>(`/performance/summary?days=${days}`)
